"""Event types and the canonical Event record.

Per SPEC §7.4 and AGENT_CONFIG_SPEC appendix: every state change is an immutable
event; `state = fold(events)`. Payloads are stored as msgpack BLOBs but exposed
to Python as ordinary dicts.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventKind(StrEnum):
    """Enumeration from AGENT_CONFIG_SPEC appendix."""

    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    ASSISTANT_TOKEN_DELTA = "assistant_token_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    TOOL_ERROR = "tool_error"
    PERMISSION_REQUESTED = "permission_requested"
    PERMISSION_GRANTED = "permission_granted"
    PERMISSION_DENIED = "permission_denied"
    MEMORY_WRITE = "memory_write"
    MEMORY_READ = "memory_read"
    TRIGGER_FIRED = "trigger_fired"
    TRIGGER_COMPLETED = "trigger_completed"
    TRIGGER_FAILED = "trigger_failed"
    TRIGGER_SKIPPED = "trigger_skipped"
    TRIGGER_MISSED = "trigger_missed"
    BUDGET_WARNING = "budget_warning"
    BUDGET_EXCEEDED = "budget_exceeded"
    SESSION_STARTED = "session_started"
    SESSION_ENDED = "session_ended"
    ERROR = "error"
    LOG = "log"
    # ── Memory subsystem (MEMORY_SPEC §7) ────────────────────────────────────
    MEM_COMPACTED = "mem_compacted"  # tier-1 working → STM
    MEM_LTM_PROMOTED = "mem_ltm_promoted"  # tier-2 STM → LTM
    MEM_LTM_FORGOTTEN = "mem_ltm_forgotten"  # tier-3 LTM compaction or forget tool
    MEM_NOTE_ADDED = "mem_note_added"
    MEM_NOTE_UPDATED = "mem_note_updated"
    MEM_NOTE_DELETED = "mem_note_deleted"
    MEM_TODO_ADDED = "mem_todo_added"
    MEM_TODO_UPDATED = "mem_todo_updated"
    MEM_TODO_DELETED = "mem_todo_deleted"
    MEM_REMEMBER = "mem_remember"  # explicit LTM write via `remember` tool
    MEM_RECALL_INVOKED = "mem_recall_invoked"
    MEM_PAUSED = "mem_paused"  # /compact off
    MEM_RESUMED = "mem_resumed"  # /compact on


def now_us() -> int:
    """Current time in unix microseconds (event timestamp unit)."""
    return int(time.time() * 1_000_000)


class Event(BaseModel):
    """One row in the event log. ``id`` is assigned by the store on append."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    ts: int = Field(default_factory=now_us)
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    parent_id: int | None = None
    trigger_id: str | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    def summary(self) -> str:
        """One-line debug rendering."""
        return f"#{self.id or '?'} [{self.kind}] {self.payload!r}"


# ── Message helpers ──────────────────────────────────────────────────────────


def user_message(content: str) -> Event:
    return Event(kind=EventKind.USER_MESSAGE, payload={"content": content})


def assistant_message(
    content: str,
    tool_calls: list[dict[str, Any]] | None = None,
    *,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    reasoning_content: str | None = None,
) -> Event:
    payload: dict[str, Any] = {"content": content, "tool_calls": tool_calls or []}
    if reasoning_content:
        payload["reasoning_content"] = reasoning_content
    return Event(
        kind=EventKind.ASSISTANT_MESSAGE,
        payload=payload,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
    )


def tool_call(call_id: str, tool_name: str, args: dict[str, Any]) -> Event:
    return Event(
        kind=EventKind.TOOL_CALL,
        payload={"call_id": call_id, "tool_name": tool_name, "args": args},
    )


def tool_result(call_id: str, tool_name: str, output: str, *, is_error: bool = False) -> Event:
    return Event(
        kind=EventKind.TOOL_ERROR if is_error else EventKind.TOOL_RESULT,
        payload={"call_id": call_id, "tool_name": tool_name, "output": output},
    )


# ── Memory helpers (MEMORY_SPEC §7) ──────────────────────────────────────────


def mem_compacted(
    *,
    snapshot_id: int,
    boundary_event_id: int,
    sections_added: int,
    tokens_before: int,
    tokens_after: int,
    model: str,
) -> Event:
    """Tier-1 (working → STM) compaction success."""
    return Event(
        kind=EventKind.MEM_COMPACTED,
        payload={
            "tier": 1,
            "snapshot_id": snapshot_id,
            "boundary_event_id": boundary_event_id,
            "sections_added": sections_added,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "model": model,
        },
    )


def mem_ltm_promoted(
    *,
    snapshot_id: int,
    additions: list[dict[str, Any]],
    kept_section_count: int,
    model: str,
) -> Event:
    """Tier-2 (STM → LTM) promotion success."""
    return Event(
        kind=EventKind.MEM_LTM_PROMOTED,
        payload={
            "snapshot_id": snapshot_id,
            "additions": additions,
            "kept_section_count": kept_section_count,
            "model": model,
        },
    )


def mem_ltm_forgotten(
    *,
    kept_count: int,
    dropped_count: int,
    dropped_digest: list[dict[str, Any]],
    cause: str,
    snapshot_id: int | None = None,
    model: str | None = None,
) -> Event:
    """Tier-3 LTM compaction success, or user/agent ``forget`` action.

    ``cause`` is ``"tier3"`` or ``"forget"``. ``model`` is set on tier-3
    runs and omitted for ``forget``.
    """
    if cause not in ("tier3", "forget"):
        raise ValueError(f"mem_ltm_forgotten cause must be tier3|forget, got {cause!r}")
    payload: dict[str, Any] = {
        "cause": cause,
        "kept_count": kept_count,
        "dropped_count": dropped_count,
        "dropped_digest": dropped_digest,
    }
    if snapshot_id is not None:
        payload["snapshot_id"] = snapshot_id
    if model is not None:
        payload["model"] = model
    return Event(kind=EventKind.MEM_LTM_FORGOTTEN, payload=payload)


def mem_note_added(*, id: str, title: str | None, tags: list[str]) -> Event:
    return Event(
        kind=EventKind.MEM_NOTE_ADDED,
        payload={"id": id, "title": title, "tags": tags},
    )


def mem_note_updated(*, id: str) -> Event:
    return Event(kind=EventKind.MEM_NOTE_UPDATED, payload={"id": id})


def mem_note_deleted(*, id: str) -> Event:
    return Event(kind=EventKind.MEM_NOTE_DELETED, payload={"id": id})


def mem_todo_added(
    *, id: str, content: str, due: str | None = None, tags: list[str] | None = None
) -> Event:
    return Event(
        kind=EventKind.MEM_TODO_ADDED,
        payload={"id": id, "content": content, "due": due, "tags": tags or []},
    )


def mem_todo_updated(*, id: str, status: str, done_at: str | None = None) -> Event:
    return Event(
        kind=EventKind.MEM_TODO_UPDATED,
        payload={"id": id, "status": status, "done_at": done_at},
    )


def mem_todo_deleted(*, id: str) -> Event:
    return Event(kind=EventKind.MEM_TODO_DELETED, payload={"id": id})


def mem_remember(*, section: str, content_preview: str, ts: str) -> Event:
    """Explicit LTM write (``remember`` tool / ``/remember``).

    ``content_preview`` is the first ~120 chars of the bullet, not the full
    content — full content is on disk, the event is a pointer.
    """
    return Event(
        kind=EventKind.MEM_REMEMBER,
        payload={"section": section, "src": "explicit", "ts": ts, "preview": content_preview},
    )


def mem_recall_invoked(
    *,
    mode: str,
    hits: int,
    query: str | None = None,
    date: str | None = None,
) -> Event:
    payload: dict[str, Any] = {"mode": mode, "hits": hits}
    if query is not None:
        payload["query"] = query
    if date is not None:
        payload["date"] = date
    return Event(kind=EventKind.MEM_RECALL_INVOKED, payload=payload)


def mem_paused() -> Event:
    return Event(kind=EventKind.MEM_PAUSED, payload={})


def mem_resumed() -> Event:
    return Event(kind=EventKind.MEM_RESUMED, payload={})
