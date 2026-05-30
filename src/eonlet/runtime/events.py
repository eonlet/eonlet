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
    MEM_LTM_PROMOTED = "mem_ltm_promoted"  # tier-2 STM → LTM (episodic)
    MEM_LTM_FORGOTTEN = "mem_ltm_forgotten"  # tier-3 episodic forgetting
    MEM_RECALL_INVOKED = "mem_recall_invoked"
    MEM_PAUSED = "mem_paused"  # /compact off
    MEM_RESUMED = "mem_resumed"  # /compact on
    # Agent-proposed semantic compaction (ADR-0006, M3).
    MEM_COMPACT_PROPOSED = "mem_compact_proposed"
    MEM_COMPACT_APPROVED = "mem_compact_approved"
    MEM_COMPACT_DECLINED = "mem_compact_declined"
    # ── Web subsystem (ADR-0004) ─────────────────────────────────────────────
    WEB_SEARCH_PERFORMED = "web_search_performed"
    WEB_FETCH_PERFORMED = "web_fetch_performed"
    # ── Knowledge axis (ADR-0005) ────────────────────────────────────────────
    KB_WRITTEN = "kb_written"  # knowledge.write / knowledge.edit
    KB_DELETED = "kb_deleted"  # knowledge.delete
    KB_MOVED = "kb_moved"  # knowledge.move
    # ── Tasks (ADR-0005 — moved out of memory) ───────────────────────────────
    TASK_ADDED = "task_added"
    TASK_UPDATED = "task_updated"  # done / cancel / edit
    TASK_DELETED = "task_deleted"


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


# ── Session helpers ──────────────────────────────────────────────────────────


def session_started(*, reason: str | None = None) -> Event:
    """Mark the start of a conversation episode.

    Emitted (with ``reason="compact"``) right after a user-forced full
    compaction empties the working window (ADR-0006), so the next user message
    begins a fresh episode carrying only the injected memory preamble.
    """
    payload: dict[str, Any] = {}
    if reason is not None:
        payload["reason"] = reason
    return Event(kind=EventKind.SESSION_STARTED, payload=payload)


def session_ended(*, reason: str | None = None) -> Event:
    """Mark the end of a conversation episode (ADR-0006)."""
    payload: dict[str, Any] = {}
    if reason is not None:
        payload["reason"] = reason
    return Event(kind=EventKind.SESSION_ENDED, payload=payload)


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
    cause: str = "tier3",
    snapshot_id: int | None = None,
    model: str | None = None,
) -> Event:
    """Tier-3 episodic-LTM forgetting success.

    ``cause`` is always ``"tier3"`` as of ADR-0005 — the explicit ``forget``
    tool (and its ``cause="forget"`` variant) was retired when LTM narrowed to
    a single uniformly-forgettable episodic population.
    """
    if cause != "tier3":
        raise ValueError(f"mem_ltm_forgotten cause must be 'tier3', got {cause!r}")
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


def task_added(
    *, id: str, content: str, due: str | None = None, tags: list[str] | None = None
) -> Event:
    return Event(
        kind=EventKind.TASK_ADDED,
        payload={"id": id, "content": content, "due": due, "tags": tags or []},
    )


def task_updated(*, id: str, status: str, done_at: str | None = None) -> Event:
    return Event(
        kind=EventKind.TASK_UPDATED,
        payload={"id": id, "status": status, "done_at": done_at},
    )


def task_deleted(*, id: str) -> Event:
    return Event(kind=EventKind.TASK_DELETED, payload={"id": id})


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


def mem_compact_proposed(*, boundary_event_id: int, reason: str, working_tokens: int) -> Event:
    """Agent proposed folding away context older than ``boundary_event_id`` (ADR-0006)."""
    return Event(
        kind=EventKind.MEM_COMPACT_PROPOSED,
        payload={
            "boundary_event_id": boundary_event_id,
            "reason": reason,
            "working_tokens": working_tokens,
        },
    )


def mem_compact_approved(*, boundary_event_id: int, rule: str = "user") -> Event:
    """A proposed compaction was approved. ``rule`` is ``"user"`` or ``"yolo"``."""
    return Event(
        kind=EventKind.MEM_COMPACT_APPROVED,
        payload={"boundary_event_id": boundary_event_id, "rule": rule},
    )


def mem_compact_declined(*, boundary_event_id: int, rule: str = "user") -> Event:
    """A proposed compaction was declined (``"user"`` or ``"no_listener"``)."""
    return Event(
        kind=EventKind.MEM_COMPACT_DECLINED,
        payload={"boundary_event_id": boundary_event_id, "rule": rule},
    )


# ── Knowledge-axis helpers (ADR-0005) ────────────────────────────────────────


def kb_written(*, path: str, size: int, action: str = "write") -> Event:
    """A knowledge file was created or edited.

    Summary-only: the event records the path and resulting size, not the body
    (the body lives on disk; the event is just a pointer to it).
    ``action`` is ``"write"`` (full-body replace) or ``"edit"`` (string-replace).
    """
    return Event(
        kind=EventKind.KB_WRITTEN,
        payload={"path": path, "size": size, "action": action},
    )


def kb_deleted(*, path: str) -> Event:
    return Event(kind=EventKind.KB_DELETED, payload={"path": path})


def kb_moved(*, src: str, dst: str) -> Event:
    return Event(kind=EventKind.KB_MOVED, payload={"src": src, "dst": dst})


# ── Web helpers (ADR-0004) ───────────────────────────────────────────────────


def web_search_performed(
    *,
    provider: str,
    query: str,
    max_results: int,
    hit_count: int,
    error: str | None = None,
) -> Event:
    """Summary-only record of a ``web_search`` call.

    Full hit list lives in the corresponding ``TOOL_RESULT`` event; this
    one exists so ``eonlet replay`` can surface fragile-fallback usage and
    long-tail provider failures at a glance.
    """
    payload: dict[str, Any] = {
        "provider": provider,
        "query": query,
        "max_results": max_results,
        "hit_count": hit_count,
    }
    if error is not None:
        payload["error"] = error
    return Event(kind=EventKind.WEB_SEARCH_PERFORMED, payload=payload)


def web_fetch_performed(
    *,
    url: str,
    content_type: str,
    bytes_in: int,
    offset_tokens: int,
    total_tokens: int,
    truncated: bool,
    error: str | None = None,
) -> Event:
    """Summary-only record of a ``web_fetch`` call.

    Full body lives in the corresponding ``TOOL_RESULT`` event. The extra
    summary fields make truncation/pagination debugging tractable without
    pulling the body out of msgpack.
    """
    payload: dict[str, Any] = {
        "url": url,
        "content_type": content_type,
        "bytes_in": bytes_in,
        "offset_tokens": offset_tokens,
        "total_tokens": total_tokens,
        "truncated": truncated,
    }
    if error is not None:
        payload["error"] = error
    return Event(kind=EventKind.WEB_FETCH_PERFORMED, payload=payload)
