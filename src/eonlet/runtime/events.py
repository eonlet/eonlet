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
) -> Event:
    return Event(
        kind=EventKind.ASSISTANT_MESSAGE,
        payload={"content": content, "tool_calls": tool_calls or []},
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
