"""``AgentState`` = fold(events). The working conversation rebuilt from the log.

Per SPEC §7.5, working memory is "last N messages + recent tool results".
We materialize a normalized message list that the LLM provider layer can map
into Anthropic or OpenAI chat format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .events import Event, EventKind

Role = Literal["user", "assistant", "tool"]


@dataclass(slots=True)
class Message:
    """A normalized chat message.

    - ``user``      → plain text content
    - ``assistant`` → text content + optional tool_calls (provider-format-neutral)
    - ``tool``      → response to a tool_call (paired by call_id)
    """

    role: Role
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False
    # Source event id — used by the injection pipeline to filter out
    # messages already represented by short-term memory (id ≤ watermark).
    event_id: int | None = None
    # Chain-of-thought from thinking-mode providers (e.g. DeepSeek).
    # Persisted so it can be echoed back on subsequent turns with tool_calls.
    reasoning_content: str | None = None


@dataclass(slots=True)
class AgentState:
    """Rebuilt state. Immutable from outside the reducer."""

    messages: list[Message] = field(default_factory=list)
    last_event_id: int = 0
    cost_today_usd: float = 0.0
    cost_total_usd: float = 0.0


def reduce(state: AgentState, event: Event) -> AgentState:
    """Apply a single event to the state, returning a new state."""
    if event.id is not None and event.id > state.last_event_id:
        state.last_event_id = event.id

    if event.cost_usd:
        state.cost_total_usd += event.cost_usd
        state.cost_today_usd += event.cost_usd  # MVP: not date-bucketed

    kind = event.kind
    if kind == EventKind.USER_MESSAGE:
        state.messages.append(
            Message(
                role="user",
                content=event.payload.get("content", ""),
                event_id=event.id,
            )
        )
    elif kind == EventKind.ASSISTANT_MESSAGE:
        state.messages.append(
            Message(
                role="assistant",
                content=event.payload.get("content", ""),
                tool_calls=event.payload.get("tool_calls", []) or [],
                reasoning_content=event.payload.get("reasoning_content") or None,
                event_id=event.id,
            )
        )
    elif kind == EventKind.TOOL_RESULT:
        state.messages.append(
            Message(
                role="tool",
                content=event.payload.get("output", ""),
                tool_call_id=event.payload.get("call_id"),
                event_id=event.id,
            )
        )
    elif kind == EventKind.TOOL_ERROR:
        state.messages.append(
            Message(
                role="tool",
                content=event.payload.get("output", ""),
                tool_call_id=event.payload.get("call_id"),
                is_error=True,
                event_id=event.id,
            )
        )
    # All other event kinds are bookkeeping; they don't change conversation state.
    return state


def fold(events: list[Event]) -> AgentState:
    """Replay a list of events into a fresh state."""
    state = AgentState()
    for ev in events:
        state = reduce(state, ev)
    return state


def recent(state: AgentState, n: int) -> list[Message]:
    """Return the last n messages (working context window slice)."""
    if n <= 0 or not state.messages:
        return state.messages[:]
    return state.messages[-n:]
