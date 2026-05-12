"""Anthropic Messages API provider."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

from ..errors import LLMError
from .protocol import DoneChunk, LLMMessage, LLMResponse, LLMToolCall, StreamChunk, TextChunk


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self._api_key:
            raise LLMError("ANTHROPIC_API_KEY not set")
        # Imported lazily so the package can be imported without the SDK.
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:
            raise LLMError("anthropic SDK not installed") from e
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.AsyncAnthropic(**kwargs)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        api_messages = _to_anthropic_messages(messages)
        api_tools = [_to_anthropic_tool(t) for t in (tools or [])]
        try:
            resp = await self._client.messages.create(
                model=self.model,
                system=system,
                messages=api_messages,
                tools=api_tools or None,
                max_tokens=max_tokens,
            )
        except Exception as e:
            raise LLMError(f"Anthropic call failed: {e}") from e

        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    LLMToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
            stop_reason=getattr(resp, "stop_reason", "end_turn") or "end_turn",
            raw=resp,
        )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        """Stream via ``client.messages.stream``.

        Anthropic's SDK provides ``stream.text_stream`` and a final
        ``get_final_message()`` we use to harvest tool_use blocks + usage.
        We pass text deltas through unchanged and emit a single terminal
        DoneChunk carrying the full LLMResponse.
        """
        api_messages = _to_anthropic_messages(messages)
        api_tools = [_to_anthropic_tool(t) for t in (tools or [])]
        try:
            async with self._client.messages.stream(
                model=self.model,
                system=system,
                messages=api_messages,
                tools=api_tools or None,
                max_tokens=max_tokens,
            ) as stream:
                async for text in stream.text_stream:
                    if text:
                        yield TextChunk(type="text", text=text)
                final = await stream.get_final_message()
        except Exception as e:
            raise LLMError(f"Anthropic stream failed: {e}") from e

        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for block in final.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(
                    LLMToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=dict(block.input or {}),
                    )
                )
        usage = getattr(final, "usage", None)
        response = LLMResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            tokens_in=getattr(usage, "input_tokens", None) if usage else None,
            tokens_out=getattr(usage, "output_tokens", None) if usage else None,
            stop_reason=getattr(final, "stop_reason", "end_turn") or "end_turn",
            raw=final,
        )
        yield DoneChunk(type="done", response=response)


def _to_anthropic_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    """Map provider-neutral messages to Anthropic's content-block schema."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks or m.content or ""})
        elif m.role == "tool":
            # Anthropic represents tool results as a user-role tool_result block.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.tool_call_id,
                            "content": m.content,
                            "is_error": m.is_error,
                        }
                    ],
                }
            )
        # ``system`` role is hoisted out by the caller (Anthropic API takes
        # `system` as a top-level param), so we just skip if it appears here.
    return out


def _to_anthropic_tool(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": t["name"],
        "description": t["description"],
        "input_schema": t["input_schema"],
    }


# Cost-estimate hook left for v0.0.2 (need price table).
def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    return None
