"""Compaction LLM contract (MEMORY_SPEC §4.1).

The compactor takes a list of events and a suggested boundary, asks a
compaction-purpose LLM to summarize the older portion into STM sections,
and returns a structured result. Boundary safety is enforced here: the
model may move the boundary BACKWARDS (compress less), never forwards.

Production: ``LLMCompactor`` wraps an ``LLMProvider``.
Tests: pass any callable matching the ``Compactor`` protocol.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ValidationError

from ..llm import LLMMessage, LLMProvider
from ..runtime.events import Event, EventKind
from .stm import STMSection

log = logging.getLogger("eonlet.memory.compactor")


# ── Data classes ───────────────────────────────────────────────────────────


class _SectionModel(BaseModel):
    ts_start: str
    ts_end: str
    topic: str
    topics: list[str] = []
    body: str


class _CompactionResponseModel(BaseModel):
    sections: list[_SectionModel]
    boundary_event_id: int


class CompactionResult(BaseModel):
    """Output of a successful tier-1 compaction call."""

    sections: list[STMSection]
    boundary_event_id: int

    model_config = {"arbitrary_types_allowed": True}


class Compactor(Protocol):
    async def summarize(self, events: list[Event], suggested_boundary: int) -> CompactionResult: ...


# ── Prompt assembly ────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You compress long conversation history into short, faithful summaries.\n"
    "\n"
    "You will receive a chronological list of conversation events between a "
    "user and an AI agent. Group them into one or more topical sections.\n"
    "\n"
    "Return ONE JSON object with this exact shape and no surrounding text:\n"
    "{\n"
    '  "sections": [\n'
    "    {\n"
    '      "ts_start": "<ISO-8601 timestamp of first event in this section>",\n'
    '      "ts_end":   "<ISO-8601 timestamp of last event in this section>",\n'
    '      "topic":    "<5-10 word topic phrase>",\n'
    '      "topics":   ["keyword", "..."],\n'
    '      "body":     "<short paragraph describing what happened>"\n'
    "    }\n"
    "  ],\n"
    '  "boundary_event_id": <integer>\n'
    "}\n"
    "\n"
    "Rules:\n"
    "- Preserve user intent and final decisions; tool I/O can be aggregated.\n"
    '- "boundary_event_id" is the LAST event id you compressed; events with '
    "id > boundary_event_id remain raw in the conversation history.\n"
    "- You MAY choose a smaller boundary than the suggested one to preserve "
    "topic coherence near the boundary. You MUST NOT choose a larger one.\n"
    "- Do not invent facts. If a turn is empty or noisy, omit it.\n"
)


def _ts_of(event: Event) -> str:
    if event.ts is None:
        return ""
    return datetime.fromtimestamp(event.ts / 1_000_000, tz=UTC).isoformat()


def _render_event_for_prompt(event: Event) -> str:
    """One-line representation suitable for the compaction prompt."""
    eid = event.id or 0
    ts = _ts_of(event)
    payload = event.payload
    if event.kind == EventKind.USER_MESSAGE:
        return f"#{eid} {ts} user: {payload.get('content', '')}"
    if event.kind == EventKind.ASSISTANT_MESSAGE:
        body = str(payload.get("content") or "")
        calls = payload.get("tool_calls") or []
        if calls:
            names = ", ".join(c.get("name", "?") for c in calls)
            return f"#{eid} {ts} assistant: {body}  [calls: {names}]"
        return f"#{eid} {ts} assistant: {body}"
    if event.kind == EventKind.TOOL_CALL:
        name = payload.get("tool_name") or "?"
        args = payload.get("args") or {}
        return f"#{eid} {ts} tool_call {name}({args})"
    if event.kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR):
        tag = "tool_error" if event.kind == EventKind.TOOL_ERROR else "tool_result"
        return f"#{eid} {ts} {tag}: {payload.get('output', '')}"
    # bookkeeping events not surfaced to the compactor
    return f"#{eid} {ts} {event.kind}"


def build_compaction_prompt(events: list[Event], suggested_boundary: int) -> str:
    """User message body sent to the compactor LLM."""
    lines = [_render_event_for_prompt(e) for e in events]
    return (
        f"Suggested boundary_event_id: {suggested_boundary}\n"
        f"You may choose a boundary <= {suggested_boundary} but never larger.\n\n"
        "Events to compress:\n" + "\n".join(lines)
    )


# ── Response parsing ───────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _extract_json(content: str) -> str:
    """Strip optional markdown fences around a JSON object."""
    m = _FENCE_RE.search(content)
    if m:
        return m.group(1).strip()
    return content.strip()


def parse_compaction_response(
    content: str,
    *,
    valid_event_ids: set[int],
    suggested_boundary: int,
) -> CompactionResult:
    """Validate and parse the compactor's JSON output.

    Raises ``ValueError`` on malformed input. Callers are expected to fall
    back to the suggested boundary on failure (MEMORY_SPEC §4.1 step 3).
    """
    raw = _extract_json(content)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"compactor response is not JSON: {e}") from e
    try:
        model = _CompactionResponseModel.model_validate(data)
    except ValidationError as e:
        raise ValueError(f"compactor response failed schema validation: {e}") from e

    boundary = model.boundary_event_id
    if boundary > suggested_boundary:
        raise ValueError(
            f"compactor boundary {boundary} exceeds suggested {suggested_boundary} "
            "(model must compress no farther than suggested)"
        )
    if boundary not in valid_event_ids:
        raise ValueError(f"compactor boundary {boundary} is not a known event id")
    if not model.sections:
        raise ValueError("compactor returned zero sections")

    return CompactionResult(
        sections=[
            STMSection(
                ts_start=s.ts_start,
                ts_end=s.ts_end,
                topic=s.topic,
                topics=list(s.topics),
                body=s.body,
            )
            for s in model.sections
        ],
        boundary_event_id=boundary,
    )


# ── LLM-backed compactor ───────────────────────────────────────────────────


class LLMCompactor:
    """Production ``Compactor`` backed by an ``LLMProvider``."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    async def summarize(self, events: list[Event], suggested_boundary: int) -> CompactionResult:
        prompt = build_compaction_prompt(events, suggested_boundary)
        msg = LLMMessage(role="user", content=prompt)
        resp = await self._provider.complete([msg], system=_SYSTEM_PROMPT, tools=None)
        valid_ids = {e.id for e in events if e.id is not None}
        return parse_compaction_response(
            resp.content,
            valid_event_ids=valid_ids,
            suggested_boundary=suggested_boundary,
        )


# ── Test helpers ───────────────────────────────────────────────────────────


CompactionFn = Callable[[list[Event], int], Awaitable[CompactionResult]]


class StaticCompactor:
    """Test-only compactor that returns a pre-canned result.

    Useful when an integration test wants tier-1 to advance the watermark
    without spinning up a real (or fake) LLM provider.
    """

    def __init__(self, sections: list[STMSection], boundary_event_id: int) -> None:
        self._sections = sections
        self._boundary = boundary_event_id
        self.calls = 0

    async def summarize(self, events: list[Event], suggested_boundary: int) -> CompactionResult:
        self.calls += 1
        return CompactionResult(
            sections=list(self._sections),
            boundary_event_id=self._boundary,
        )


__all__ = [
    "CompactionFn",
    "CompactionResult",
    "Compactor",
    "LLMCompactor",
    "StaticCompactor",
    "build_compaction_prompt",
    "parse_compaction_response",
]
