"""Cover the second fake-provider variant (tool-then-text)."""
from __future__ import annotations

import anyio
import pytest

from eonlet.llm.factory import build_provider
from eonlet.llm.protocol import LLMMessage


def test_fake_tool_then_text_two_turn_dialog() -> None:
    p = build_provider("fake-tool-then-text")

    async def turn1() -> tuple[str, list]:
        last = None
        async for c in p.stream([LLMMessage(role="user", content="x")], system=""):
            if c["type"] == "done":
                last = c["response"]
        return last.content, last.tool_calls

    async def turn2() -> str:
        last = None
        async for c in p.stream([LLMMessage(role="user", content="x")], system=""):
            if c["type"] == "done":
                last = c["response"]
        return last.content

    c1, tc1 = anyio.run(turn1)
    assert c1 == ""
    assert len(tc1) == 1 and tc1[0].name == "sleep"

    c2 = anyio.run(turn2)
    assert c2 == "done"


def test_fake_echo_complete_path() -> None:
    """Cover the non-streaming complete() shortcut on FakeProvider."""
    p = build_provider("fake-echo")

    async def go():
        return await p.complete(
            [LLMMessage(role="user", content="world")], system="be terse"
        )

    resp = anyio.run(go)
    assert resp.content == "echo: world"


def test_fake_provider_unknown_variant_raises() -> None:
    from eonlet.llm.fake_provider import FakeProvider

    with pytest.raises(ValueError, match="unknown fake variant"):
        FakeProvider("fake-doesnt-exist")
