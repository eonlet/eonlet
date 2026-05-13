"""OpenAI (and OpenAI-compatible: Ollama, vLLM) provider via Chat Completions."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from ..errors import LLMError
from .protocol import DoneChunk, LLMMessage, LLMResponse, LLMToolCall, StreamChunk, TextChunk


class OpenAIProvider:
    name = "openai"

    def __init__(self, model: str, api_key: str | None = None, base_url: str | None = None) -> None:
        self.model = model
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._base_url = base_url or os.environ.get("OPENAI_BASE_URL")
        if not self._api_key:
            raise LLMError("OPENAI_API_KEY not set")
        try:
            import openai  # type: ignore[import-not-found]
        except ImportError as e:
            raise LLMError("openai SDK not installed") from e
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = openai.AsyncOpenAI(**kwargs)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        api_messages = [{"role": "system", "content": system}, *_to_openai_messages(messages)]
        api_tools = [_to_openai_tool(t) for t in (tools or [])]
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=api_tools or None,
                max_tokens=max_tokens,
            )
        except Exception as e:
            raise LLMError(f"OpenAI call failed: {e}") from e

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_calls: list[LLMToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            tool_calls.append(LLMToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content=text,
            tool_calls=tool_calls,
            tokens_in=getattr(usage, "prompt_tokens", None) if usage else None,
            tokens_out=getattr(usage, "completion_tokens", None) if usage else None,
            stop_reason=choice.finish_reason or "stop",
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
        """Stream via ``stream=True``.

        We accumulate text + (possibly chunked) tool-call JSON arguments.
        OpenAI's streaming API emits tool_calls in pieces — index, then bits
        of the function.arguments string — so we re-assemble them by index.
        """
        api_messages = [{"role": "system", "content": system}, *_to_openai_messages(messages)]
        api_tools = [_to_openai_tool(t) for t in (tools or [])]
        try:
            stream_resp = await self._client.chat.completions.create(
                model=self.model,
                messages=api_messages,
                tools=api_tools or None,
                max_tokens=max_tokens,
                stream=True,
                stream_options={"include_usage": True},
            )
        except Exception as e:
            raise LLMError(f"OpenAI stream failed: {e}") from e

        content_buf: list[str] = []
        tool_buf: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"
        usage_tokens_in: int | None = None
        usage_tokens_out: int | None = None
        last_raw: Any = None

        async for chunk in stream_resp:
            last_raw = chunk
            if getattr(chunk, "usage", None):
                usage_tokens_in = getattr(chunk.usage, "prompt_tokens", None)
                usage_tokens_out = getattr(chunk.usage, "completion_tokens", None)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                content_buf.append(delta.content)
                yield TextChunk(type="text", text=delta.content)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    slot = tool_buf.setdefault(tc.index, {"id": "", "name": "", "args_json": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["args_json"] += tc.function.arguments
            if choice.finish_reason:
                finish_reason = choice.finish_reason

        tool_calls: list[LLMToolCall] = []
        for slot in tool_buf.values():
            try:
                args = json.loads(slot["args_json"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": slot["args_json"]}
            tool_calls.append(LLMToolCall(id=slot["id"], name=slot["name"], arguments=args))

        response = LLMResponse(
            content="".join(content_buf),
            tool_calls=tool_calls,
            tokens_in=usage_tokens_in,
            tokens_out=usage_tokens_out,
            stop_reason=finish_reason,
            raw=last_raw,
        )
        yield DoneChunk(type="done", response=response)


def _to_openai_messages(messages: list[LLMMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ]
            out.append(entry)
        elif m.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id,
                    "content": m.content,
                }
            )
    return out


def _to_openai_tool(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
        },
    }
