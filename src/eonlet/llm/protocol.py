"""Provider-neutral LLM types."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypedDict

Role = Literal["system", "user", "assistant", "tool"]


# ── Streaming chunk types ────────────────────────────────────────────────────


class TextChunk(TypedDict):
    """Incremental assistant text emitted by ``LLMProvider.stream``."""

    type: Literal["text"]
    text: str


class DoneChunk(TypedDict):
    """Terminal event from ``LLMProvider.stream`` — carries the full response."""

    type: Literal["done"]
    response: LLMResponse


StreamChunk = TextChunk | DoneChunk


@dataclass(slots=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class LLMMessage:
    """Provider-neutral message. The provider module maps it to its own schema."""

    role: Role
    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False


@dataclass(slots=True)
class LLMResponse:
    """One assistant turn."""

    content: str
    tool_calls: list[LLMToolCall]
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    stop_reason: str = "end_turn"
    raw: Any = None  # provider-specific payload for debugging


class LLMProvider(Protocol):
    """Minimal interface every provider exposes."""

    name: str
    model: str

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Single non-streaming completion. Kept for tests + non-interactive paths."""
        ...

    def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """Yield ``TextChunk``s as they arrive, then a single terminal ``DoneChunk``.

        Default streaming is what powers ``eonlet attach`` token-by-token. The
        terminal chunk carries the same fields (tool_calls, usage, stop_reason)
        as ``complete()`` so the agent loop can finalize without a second call.
        """
        ...
