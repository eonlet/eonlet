"""In-process fake LLM provider for testing and demos.

Selected by ``build_provider`` when ``runtime.model`` starts with ``fake-``.
The provider never calls a remote service — all responses are deterministic
in-process, suitable for CI subprocesses that have no API key.

Available variants:

- ``fake-echo`` — single-turn. Replies ``"echo: <last user message>"`` in three
  streamed text chunks.
- ``fake-tool-then-text`` — two-turn. First turn requests ``sleep(0)``; second
  turn replies ``"done"``. Useful for exercising tool dispatch.

The variant name is also stored on the instance as ``model``, so it shows up
in event payloads / inspect output exactly like a real model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .protocol import (
    DoneChunk,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    StreamChunk,
    TextChunk,
)


class FakeProvider:
    """Deterministic provider matched on ``fake-*`` model strings."""

    name = "fake"

    def __init__(self, model: str) -> None:
        self.model = model
        if model not in {"fake-echo", "fake-tool-then-text"}:
            raise ValueError(f"unknown fake variant: {model!r}")
        # ``fake-tool-then-text`` is stateful across turns within one run; we
        # count how many ``stream()``s have happened.
        self._turn = 0

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Non-streaming path. Returns one final ``LLMResponse``."""
        chunks: list[StreamChunk] = []
        async for c in self.stream(messages, system=system, tools=tools, max_tokens=max_tokens):
            chunks.append(c)
        # Last chunk is always Done.
        done = chunks[-1]
        return done["response"]  # type: ignore[typeddict-item]

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        if self.model == "fake-echo":
            async for c in self._echo_stream(messages):
                yield c
            return
        if self.model == "fake-tool-then-text":
            async for c in self._tool_then_text_stream():
                yield c
            return
        raise AssertionError(f"unhandled fake variant: {self.model}")  # pragma: no cover

    # ── variants ─────────────────────────────────────────────────────────────

    async def _echo_stream(self, messages: list[LLMMessage]) -> AsyncIterator[StreamChunk]:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        reply = f"echo: {last_user}"
        # Break into 3 roughly-equal chunks so tests can verify multi-delta flow.
        n = len(reply)
        a, b = n // 3, 2 * n // 3
        for piece in (reply[:a], reply[a:b], reply[b:]):
            if piece:
                yield TextChunk(type="text", text=piece)
        yield DoneChunk(
            type="done",
            response=LLMResponse(
                content=reply,
                tool_calls=[],
                tokens_in=len(last_user.split()),
                tokens_out=len(reply.split()),
                stop_reason="end_turn",
            ),
        )

    async def _tool_then_text_stream(self) -> AsyncIterator[StreamChunk]:
        self._turn += 1
        if self._turn == 1:
            # First turn: emit a single tool_use, no text.
            yield DoneChunk(
                type="done",
                response=LLMResponse(
                    content="",
                    tool_calls=[LLMToolCall(id="call_1", name="sleep", arguments={"seconds": 0})],
                    stop_reason="tool_use",
                ),
            )
            return
        # Second turn: text only.
        for piece in ("do", "ne"):
            yield TextChunk(type="text", text=piece)
        yield DoneChunk(
            type="done",
            response=LLMResponse(content="done", tool_calls=[], stop_reason="end_turn"),
        )
