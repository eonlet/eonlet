"""Tier-3 compaction / forgetting (MEMORY_SPEC §4.5).

LTM → LTM: when LTM exceeds ``long_term_tokens`` budget, call the compaction
LLM to prune or merge bullets and rewrite the document within budget.

Sequence per pass:

1. Read current LTM; if empty or under budget, return early.
2. Build prompt and call the compaction LLM.
3. Validate response; on failure leave all files unchanged.
4. Rewrite LTM from the ``kept_bullets`` list.
5. Emit ``mem_ltm_forgotten``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import anyio
from pydantic import BaseModel, ValidationError

from ..llm import LLMMessage, LLMProvider
from ..runtime.events import mem_ltm_forgotten
from .config import MemoryConfig
from .ltm import LTMBullet, LTMStore
from .tier1 import RecordEventFn
from .tokens import estimate

log = logging.getLogger("eonlet.memory.tier3")

# ── Response schema ─────────────────────────────────────────────────────────


class _T3KeptBullet(BaseModel):
    section: str
    content: str
    src: str
    ts: str
    merged_from: list[str] = []


class _T3DroppedBullet(BaseModel):
    section: str
    preview: str
    reason: str


class _T3Response(BaseModel):
    kept_bullets: list[_T3KeptBullet]
    dropped_bullets: list[_T3DroppedBullet]


# ── Outcome ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ForgettingOutcome:
    ran: bool
    kept_count: int = 0
    dropped_count: int = 0
    error: str | None = None


# ── Prompt ──────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You compact a long-term EPISODIC timeline by dropping or merging stale entries.\n"
    "\n"
    "Long-term memory is a single uniform population: dated episodic summaries of\n"
    "what happened, when. It naturally decays — older, low-salience entries are\n"
    "less useful than recent ones. There is no protected class of bullet; every\n"
    "entry is eligible for forgetting by recency and salience.\n"
    "\n"
    "Return ONE JSON object:\n"
    "{\n"
    '  "kept_bullets": [\n'
    '    {"section": "episodic", "content": "...", "src": "...", "ts": "...", "merged_from": ["..."]}\n'
    "  ],\n"
    '  "dropped_bullets": [\n'
    '    {"section": "episodic", "preview": "first 80 chars", "reason": "duplicate|stale|low-salience"}\n'
    "  ]\n"
    "}\n"
    "\n"
    "Rules:\n"
    "- Prefer keeping recent and high-salience summaries; drop old, redundant, or\n"
    "  low-salience ones to fit the budget.\n"
    "- When merging adjacent or duplicate summaries, preserve the earliest 'ts'.\n"
    "- Minimise information loss. When in doubt about a recent entry, keep it.\n"
    "- Every kept bullet MUST have: section, content, src, ts.\n"
    "- 'merged_from' lists the original content strings that were combined.\n"
)


def build_tier3_prompt(ltm_raw: str) -> str:
    """Build the user message sent to the tier-3 LLM."""
    return f"Current long-term memory to compact:\n\n{ltm_raw}"


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def parse_tier3_response(content: str) -> _T3Response:
    """Parse and validate the tier-3 LLM response.

    Raises ``ValueError`` on malformed input.
    """
    raw = content.strip()
    m = _FENCE_RE.search(raw)
    if m:
        raw = m.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"tier-3 response is not JSON: {e}") from e
    try:
        return _T3Response.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"tier-3 response failed schema: {e}") from e


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


async def run_tier3(
    *,
    memory_dir: Path,
    cfg: MemoryConfig,
    provider: LLMProvider,
    record_event: RecordEventFn | None = None,
) -> ForgettingOutcome:
    """Run one tier-3 (LTM forgetting) pass.

    Returns early with ``ran=False`` if LTM is empty or under budget.
    On LLM or parse failure, returns ``ran=False, error=...`` and leaves
    LTM unchanged (M-I2 / do-not-change-persistent-state-on-failure).
    """
    lock = _lock_for(memory_dir)
    async with lock:
        ltm_store = LTMStore(memory_dir)
        ltm_raw = ltm_store.read_raw()
        if not ltm_raw:
            return ForgettingOutcome(ran=False)

        ltm_tokens = estimate(ltm_raw)
        if ltm_tokens < cfg.episodic.long_term_tokens:
            return ForgettingOutcome(ran=False)

        prompt = build_tier3_prompt(ltm_raw)
        try:
            llm_resp = await provider.complete(
                [LLMMessage(role="user", content=prompt)],
                system=_SYSTEM_PROMPT,
                tools=None,
            )
        except Exception as e:
            err = f"tier-3 LLM call failed: {e}"
            log.warning(err)
            return ForgettingOutcome(ran=False, error=err)

        try:
            resp = parse_tier3_response(llm_resp.content)
        except ValueError as e:
            err = f"tier-3 parse failed: {e}"
            log.warning(err)
            return ForgettingOutcome(ran=False, error=err)

        kept_bullets = [
            LTMBullet(
                section=b.section,
                content=b.content,
                src=b.src,
                ts=b.ts,
                raw=f"- {b.content} [src:{b.src}, ts:{b.ts}]",
            )
            for b in resp.kept_bullets
        ]
        await ltm_store.rewrite(kept_bullets)

        dropped_digest = [
            {"section": d.section, "preview": d.preview, "reason": d.reason}
            for d in resp.dropped_bullets
        ]

        model_name = getattr(provider, "model", type(provider).__name__)
        if record_event is not None:
            await record_event(
                mem_ltm_forgotten(
                    kept_count=len(kept_bullets),
                    dropped_count=len(resp.dropped_bullets),
                    dropped_digest=dropped_digest,
                    cause="tier3",
                    model=str(model_name),
                )
            )

        return ForgettingOutcome(
            ran=True,
            kept_count=len(kept_bullets),
            dropped_count=len(resp.dropped_bullets),
        )


__all__ = [
    "ForgettingOutcome",
    "build_tier3_prompt",
    "parse_tier3_response",
    "run_tier3",
]
