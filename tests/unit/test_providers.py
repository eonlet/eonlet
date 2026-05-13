"""LLM provider unit tests.

Both providers are instantiated with a monkey-patched SDK client — we never
hit the network. The goal is to verify:

  - Message-shape mapping (Anthropic ``tool_result`` block, OpenAI tool_call assembly)
  - Streaming chunk emission (``TextChunk`` per delta, terminal ``DoneChunk``)
  - Tool-call argument reassembly when OpenAI streams chunked JSON

The fake_provider has its own coverage in test_streaming.py; this file
exercises the *real* anthropic_provider.py and openai_provider.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator
from unittest.mock import MagicMock

import anyio
import pytest

from eonlet.llm.protocol import LLMMessage, LLMToolCall


# ── helpers: fake Anthropic SDK ──────────────────────────────────────────────


@dataclass
class _AnthropicTextBlock:
    type: str
    text: str


@dataclass
class _AnthropicToolUseBlock:
    type: str
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _AnthropicFinal:
    content: list
    usage: Any
    stop_reason: str


class _AnthropicStream:
    """Mimics the async context manager + ``text_stream`` + ``get_final_message``."""

    def __init__(self, text_chunks: list[str], final: _AnthropicFinal):
        self._chunks = text_chunks
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    @property
    def text_stream(self):
        async def _gen() -> AsyncIterator[str]:
            for c in self._chunks:
                yield c

        return _gen()

    async def get_final_message(self) -> _AnthropicFinal:
        return self._final


class _FakeAnthropicMessages:
    def __init__(self, text_chunks, final, complete_resp=None):
        self._text_chunks = text_chunks
        self._final = final
        self._complete_resp = complete_resp
        self.create_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _AnthropicStream(self._text_chunks, self._final)

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._complete_resp


class _FakeAnthropicClient:
    def __init__(self, messages: _FakeAnthropicMessages):
        self.messages = messages


# ── Anthropic tests ──────────────────────────────────────────────────────────


def _make_anthropic_provider(monkeypatch: pytest.MonkeyPatch, messages: _FakeAnthropicMessages):
    """Build a real ``AnthropicProvider`` with the SDK client replaced."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    # Stub the anthropic module so ``import anthropic`` succeeds.
    import sys
    import types as _types

    mod = _types.ModuleType("anthropic")
    mod.AsyncAnthropic = lambda **kw: _FakeAnthropicClient(messages)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", mod)

    from eonlet.llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider("claude-test")


def test_anthropic_stream_yields_text_chunks_and_final(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _AnthropicFinal(
        content=[_AnthropicTextBlock(type="text", text="Hello world")],
        usage=MagicMock(input_tokens=4, output_tokens=2),
        stop_reason="end_turn",
    )
    msgs = _FakeAnthropicMessages(text_chunks=["Hel", "lo ", "world"], final=final)
    provider = _make_anthropic_provider(monkeypatch, msgs)

    async def go() -> tuple[list[str], Any]:
        out: list[str] = []
        last = None
        async for c in provider.stream([LLMMessage(role="user", content="hi")], system="be terse"):
            if c["type"] == "text":
                out.append(c["text"])
            elif c["type"] == "done":
                last = c["response"]
        return out, last

    deltas, response = anyio.run(go)
    assert deltas == ["Hel", "lo ", "world"]
    assert response.content == "Hello world"
    assert response.tokens_in == 4 and response.tokens_out == 2
    assert response.stop_reason == "end_turn"
    # System prompt was passed through.
    assert msgs.stream_calls[0]["system"] == "be terse"


def test_anthropic_stream_collects_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    final = _AnthropicFinal(
        content=[
            _AnthropicTextBlock(type="text", text=""),
            _AnthropicToolUseBlock(
                type="tool_use", id="call_a", name="lookup", input={"key": "x"}
            ),
        ],
        usage=None,
        stop_reason="tool_use",
    )
    msgs = _FakeAnthropicMessages(text_chunks=[], final=final)
    provider = _make_anthropic_provider(monkeypatch, msgs)

    async def go():
        last = None
        async for c in provider.stream([LLMMessage(role="user", content="x")], system=""):
            if c["type"] == "done":
                last = c["response"]
        return last

    resp = anyio.run(go)
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_a" and tc.name == "lookup" and tc.arguments == {"key": "x"}


def test_anthropic_complete_maps_tool_result_as_user_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An LLMMessage(role='tool', ...) must serialize into Anthropic's
    ``user`` role with a ``tool_result`` content block."""
    complete_resp = MagicMock()
    complete_resp.content = [_AnthropicTextBlock(type="text", text="ok")]
    complete_resp.usage = None
    complete_resp.stop_reason = "end_turn"
    msgs = _FakeAnthropicMessages(text_chunks=[], final=None, complete_resp=complete_resp)
    provider = _make_anthropic_provider(monkeypatch, msgs)

    async def go() -> None:
        await provider.complete(
            [
                LLMMessage(role="user", content="do it"),
                LLMMessage(
                    role="assistant",
                    content="",
                    tool_calls=[LLMToolCall(id="t1", name="lookup", arguments={})],
                ),
                LLMMessage(role="tool", content="answer", tool_call_id="t1"),
            ],
            system="",
        )

    anyio.run(go)
    sent_messages = msgs.create_calls[0]["messages"]
    # The tool-result message should be a user-role with a tool_result block.
    tool_msg = sent_messages[-1]
    assert tool_msg["role"] == "user"
    assert tool_msg["content"][0]["type"] == "tool_result"
    assert tool_msg["content"][0]["tool_use_id"] == "t1"


# ── helpers: fake OpenAI SDK ─────────────────────────────────────────────────


@dataclass
class _OAIDeltaToolFn:
    name: str | None
    arguments: str | None


@dataclass
class _OAIDeltaToolCall:
    index: int
    id: str | None
    function: _OAIDeltaToolFn | None


@dataclass
class _OAIDelta:
    content: str | None = None
    tool_calls: list[_OAIDeltaToolCall] | None = None


@dataclass
class _OAIChoice:
    delta: _OAIDelta
    finish_reason: str | None = None


@dataclass
class _OAIChunk:
    choices: list[_OAIChoice]
    usage: Any = None


class _OAIAsyncIter:
    def __init__(self, chunks: list[_OAIChunk]):
        self._iter = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeOAICompletions:
    def __init__(self, chunks: list[_OAIChunk]):
        self._chunks = chunks
        self.create_calls: list[dict] = []

    async def create(self, **kwargs):
        self.create_calls.append(kwargs)
        return _OAIAsyncIter(self._chunks)


class _FakeOAIChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOAIClient:
    def __init__(self, completions):
        self.chat = _FakeOAIChat(completions)


def _make_openai_provider(monkeypatch: pytest.MonkeyPatch, completions: _FakeOAICompletions):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    import sys
    import types as _types

    mod = _types.ModuleType("openai")
    mod.AsyncOpenAI = lambda **kw: _FakeOAIClient(completions)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", mod)

    from eonlet.llm.openai_provider import OpenAIProvider

    return OpenAIProvider("gpt-test")


# ── OpenAI tests ─────────────────────────────────────────────────────────────


def test_openai_stream_yields_text_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        _OAIChunk(choices=[_OAIChoice(delta=_OAIDelta(content="He"))]),
        _OAIChunk(choices=[_OAIChoice(delta=_OAIDelta(content="llo"))]),
        _OAIChunk(
            choices=[_OAIChoice(delta=_OAIDelta(content=None), finish_reason="stop")],
            usage=MagicMock(prompt_tokens=3, completion_tokens=2),
        ),
    ]
    completions = _FakeOAICompletions(chunks)
    provider = _make_openai_provider(monkeypatch, completions)

    async def go():
        deltas: list[str] = []
        last = None
        async for c in provider.stream([LLMMessage(role="user", content="hi")], system="s"):
            if c["type"] == "text":
                deltas.append(c["text"])
            elif c["type"] == "done":
                last = c["response"]
        return deltas, last

    deltas, response = anyio.run(go)
    assert deltas == ["He", "llo"]
    assert response.content == "Hello"
    assert response.stop_reason == "stop"
    assert response.tokens_in == 3 and response.tokens_out == 2


def test_openai_stream_reassembles_chunked_tool_call_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI streams `function.arguments` as broken-up JSON; the provider must
    concatenate by ``index`` before json.loads."""
    chunks = [
        _OAIChunk(
            choices=[
                _OAIChoice(
                    delta=_OAIDelta(
                        tool_calls=[
                            _OAIDeltaToolCall(
                                index=0,
                                id="call_42",
                                function=_OAIDeltaToolFn(name="search", arguments='{"q":'),
                            )
                        ]
                    )
                )
            ]
        ),
        _OAIChunk(
            choices=[
                _OAIChoice(
                    delta=_OAIDelta(
                        tool_calls=[
                            _OAIDeltaToolCall(
                                index=0,
                                id=None,
                                function=_OAIDeltaToolFn(name=None, arguments=' "py"}'),
                            )
                        ]
                    )
                )
            ]
        ),
        _OAIChunk(
            choices=[_OAIChoice(delta=_OAIDelta(), finish_reason="tool_calls")]
        ),
    ]
    completions = _FakeOAICompletions(chunks)
    provider = _make_openai_provider(monkeypatch, completions)

    async def go():
        last = None
        async for c in provider.stream([LLMMessage(role="user", content="x")], system=""):
            if c["type"] == "done":
                last = c["response"]
        return last

    resp = anyio.run(go)
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_42" and tc.name == "search"
    assert tc.arguments == {"q": "py"}
    assert resp.stop_reason == "tool_calls"
