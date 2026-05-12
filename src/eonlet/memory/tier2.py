"""Tier-2 compaction orchestration (MEMORY_SPEC §4.4).

STM → LTM promotion: when STM exceeds ``short_term_tokens`` budget, call the
compaction LLM to extract durable facts into LTM and replace STM with only
the sections the model flagged as worth keeping.

Sequence per pass:

1. Read current STM; if empty or under budget, return early.
2. Build promotion prompt and call the compaction LLM.
3. Write LTM additions tagged ``src:implicit``.
4. Replace STM with kept sections only.
5. Emit ``mem_ltm_promoted``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ValidationError

from ..llm import LLMMessage, LLMProvider
from ..runtime.events import mem_ltm_promoted
from .config import MemoryConfig
from .ltm import CATEGORIES, LTMStore
from .stm import STMSection, STMStore
from .tier1 import RecordEventFn
from .tokens import estimate

log = logging.getLogger("eonlet.memory.tier2")

_VALID_SECTIONS = frozenset(CATEGORIES)

# ── Response schema ─────────────────────────────────────────────────────────


class _T2Addition(BaseModel):
    section: str
    content: str


class _T2Response(BaseModel):
    ltm_additions: list[_T2Addition]
    stm_keep_section_headers: list[str]


# ── Outcome ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PromotionOutcome:
    ran: bool
    additions: int = 0
    kept_section_count: int = 0
    error: str | None = None


# ── Prompt ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You distill short-term memory summaries into durable long-term knowledge.\n"
    "\n"
    "You receive the current short-term memory (STM) as a list of sections.\n"
    "For each section decide:\n"
    "  1. What knowledge is durable enough to promote to long-term memory (LTM).\n"
    "  2. Whether to keep the section in STM (very recent context) or discard it.\n"
    "\n"
    "Return ONE JSON object with this exact shape and no surrounding text:\n"
    "{\n"
    '  "ltm_additions": [\n'
    '    {"section": "<user|feedback|project|reference|fact|episodic>", "content": "<bullet>"}\n'
    "  ],\n"
    '  "stm_keep_section_headers": ["## [ts – ts] topic", ...]\n'  # noqa: RUF001
    "}\n"
    "\n"
    "Rules:\n"
    "- ltm_additions[].section must be one of: user, feedback, project, reference, fact, episodic.\n"
    "- stm_keep_section_headers must be EXACT copies of the '## [...]' header lines.\n"
    "  Sections NOT listed are dropped from STM (they are represented by LTM now).\n"
    "- Keep sections that contain very recent context. Promote older ones.\n"
    "- Episodic LTM bullets should be dated: '2026-05-22: <one-line summary>'.\n"
    "- Do not invent facts. Be concise.\n"
)


def build_tier2_prompt(sections: list[STMSection]) -> str:
    """Build the user message sent to the tier-2 LLM."""
    lines: list[str] = ["Short-term memory sections to process:\n"]
    for sec in sections:
        # Use en-dash (U+2013) to match STM section header format.
        lines.append(f"## [{sec.ts_start} – {sec.ts_end}] {sec.topic}")  # noqa: RUF001
        if sec.topics:
            lines.append(f"[topics: {', '.join(sec.topics)}]")
        lines.append("")
        lines.append(sec.body)
        lines.append("")
    return "\n".join(lines)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def parse_tier2_response(content: str, sections: list[STMSection]) -> _T2Response:
    """Parse and validate the tier-2 LLM response.

    Raises ``ValueError`` on malformed input; callers should treat this as a
    soft failure (log, skip the pass, leave STM unchanged).
    """
    raw = content.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"tier-2 response is not JSON: {e}") from e
    try:
        resp = _T2Response.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"tier-2 response failed schema: {e}") from e
    for add in resp.ltm_additions:
        if add.section not in _VALID_SECTIONS:
            raise ValueError(f"unknown LTM section {add.section!r}")
    return resp


# ── Lock registry ────────────────────────────────────────────────────────────

_locks: dict[str, anyio.Lock] = {}


def _lock_for(memory_dir: Path) -> anyio.Lock:
    key = str(memory_dir.resolve())
    lock = _locks.get(key)
    if lock is None:
        lock = anyio.Lock()
        _locks[key] = lock
    return lock


# ── Orchestration ────────────────────────────────────────────────────────────


async def run_tier2(
    *,
    memory_dir: Path,
    cfg: MemoryConfig,
    provider: LLMProvider,
    snapshot_id: int = 0,
    record_event: RecordEventFn | None = None,
) -> PromotionOutcome:
    """Run one tier-2 (STM → LTM) promotion pass.

    Returns early with ``ran=False`` if STM is empty or under budget.
    On LLM or parse failure, returns ``ran=False, error=...`` and leaves
    all files unchanged.
    """
    lock = _lock_for(memory_dir)
    async with lock:
        stm_store = STMStore(memory_dir)
        sections = await stm_store.read()
        if not sections:
            return PromotionOutcome(ran=False)

        stm_tokens = estimate(await stm_store.read_raw())
        if stm_tokens < cfg.conversation.short_term_tokens:
            return PromotionOutcome(ran=False)

        prompt = build_tier2_prompt(sections)
        try:
            llm_resp = await provider.complete(
                [LLMMessage(role="user", content=prompt)],
                system=_SYSTEM_PROMPT,
                tools=None,
            )
        except Exception as e:
            err = f"tier-2 LLM call failed: {e}"
            log.warning(err)
            return PromotionOutcome(ran=False, error=err)

        try:
            resp = parse_tier2_response(llm_resp.content, sections)
        except ValueError as e:
            err = f"tier-2 parse failed: {e}"
            log.warning(err)
            return PromotionOutcome(ran=False, error=err)

        # Determine kept STM sections by matching header strings.
        keep_headers = set(resp.stm_keep_section_headers)
        kept_sections: list[STMSection] = []
        for sec in sections:
            header = f"## [{sec.ts_start} – {sec.ts_end}] {sec.topic}"  # noqa: RUF001
            if header in keep_headers:
                kept_sections.append(sec)

        # Write LTM additions tagged src:implicit.
        ltm_store = LTMStore(memory_dir)
        today = datetime.now(UTC).date().isoformat()
        additions_payload: list[dict[str, Any]] = []
        for add in resp.ltm_additions:
            await ltm_store.append_bullet(
                section=add.section,
                content=add.content,
                src="implicit",
                ts=today,
            )
            additions_payload.append(
                {"section": add.section, "content": add.content, "src": "implicit", "ts": today}
            )

        # Replace STM with kept sections.
        await stm_store.replace(kept_sections)

        model_name = getattr(provider, "model", type(provider).__name__)
        if record_event is not None:
            await record_event(
                mem_ltm_promoted(
                    snapshot_id=snapshot_id,
                    additions=additions_payload,
                    kept_section_count=len(kept_sections),
                    model=str(model_name),
                )
            )

        return PromotionOutcome(
            ran=True,
            additions=len(additions_payload),
            kept_section_count=len(kept_sections),
        )


__all__ = [
    "PromotionOutcome",
    "build_tier2_prompt",
    "parse_tier2_response",
    "run_tier2",
]
