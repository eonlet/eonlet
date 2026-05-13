"""Streaming agent loop — fake provider drives ``on_delta``.

Verifies that:
  - ``AgentRuntime.handle_user_message`` consumes ``provider.stream``.
  - Each TextChunk is forwarded to ``on_delta`` (the IPC bridge).
  - A single final ``assistant_message`` event is appended carrying the full
    concatenated content (no per-token events).
  - Multi-turn tool-calling still works under streaming: 1st turn yields a
    tool_use, 2nd turn yields plain text.
"""
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Iterable

import anyio
import pytest

from eonlet.config import AgentConfig
from eonlet.llm.protocol import LLMMessage, LLMResponse, LLMToolCall, StreamChunk
from eonlet.permissions import PermissionGate
from eonlet.runtime.agent import AgentRuntime
from eonlet.runtime.definition import Definition
from eonlet.runtime.events import EventKind
from eonlet.runtime.store import EventStore
from eonlet.tools import builtin as _builtin  # noqa: F401 — register builtins


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, turns: Iterable[list[StreamChunk]]):
        # Each turn is a flat list of TextChunk + a final DoneChunk.
        self._turns = list(turns)
        self.calls = 0

    async def complete(self, *a, **k) -> LLMResponse:  # pragma: no cover — unused
        raise NotImplementedError

    async def stream(self, *args, **kwargs) -> AsyncIterator[StreamChunk]:
        chunks = self._turns[self.calls]
        self.calls += 1
        for c in chunks:
            yield c


def _make_definition(tmp_path: Path) -> Definition:
    d = tmp_path / "fakebot"
    d.mkdir()
    (d / "agent.yaml").write_text(
        "apiVersion: eonlet/v1\nkind: Agent\n"
        "metadata:\n  name: fakebot\n  description: t\n  version: 0.0.1\n"
        "runtime:\n  model: claude-fake\n  max_steps_per_run: 5\n"
        "tools:\n  builtin: [sleep]\n",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# fakebot\nbe terse.\n", encoding="utf-8")
    from eonlet.runtime.definition import load_definition

    return load_definition(d)


def _build_runtime(tmp_path: Path, provider: _FakeProvider) -> AgentRuntime:
    defn = _make_definition(tmp_path)
    store = EventStore(tmp_path / "state.db")
    return AgentRuntime(
        eonlet_id="fakebot.test",
        definition=defn,
        store=store,
        workspace=tmp_path / "ws",
        memory_dir=tmp_path / "mem",
        provider=provider,
        gate=PermissionGate(mode=defn.config.permissions.mode, extra_deny=[]),
    )


# ── tests ────────────────────────────────────────────────────────────────────


def test_text_only_run_forwards_deltas_and_stores_one_assistant_event(
    tmp_path: Path,
) -> None:
    chunks: list[StreamChunk] = [
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": " world"},
        {"type": "text", "text": "!"},
        {
            "type": "done",
            "response": LLMResponse(
                content="Hello world!",
                tool_calls=[],
                tokens_in=10,
                tokens_out=3,
                stop_reason="end_turn",
            ),
        },
    ]
    provider = _FakeProvider(turns=[chunks])
    runtime = _build_runtime(tmp_path, provider)
    deltas: list[str] = []

    async def on_delta(t: str) -> None:
        deltas.append(t)

    runtime.on_delta = on_delta

    async def run() -> list:
        events = []
        async for ev in runtime.handle_user_message("hi"):
            events.append(ev)
        return events

    events = anyio.run(run)
    # 3 text deltas arrived.
    assert deltas == ["Hello", " world", "!"]
    # Exactly one persisted assistant_message with the concatenated content.
    assist = [e for e in events if e.kind == EventKind.ASSISTANT_MESSAGE]
    assert len(assist) == 1
    assert assist[0].payload["content"] == "Hello world!"
    # No token-delta events stored.
    assert not any(e.kind == EventKind.ASSISTANT_TOKEN_DELTA for e in events)


def test_tool_use_then_text_turn(tmp_path: Path) -> None:
    """Multi-turn: first turn calls a tool, second turn produces text."""
    turn1: list[StreamChunk] = [
        {
            "type": "done",
            "response": LLMResponse(
                content="",
                tool_calls=[LLMToolCall(id="c1", name="sleep", arguments={"seconds": 0})],
                stop_reason="tool_use",
            ),
        },
    ]
    turn2: list[StreamChunk] = [
        {"type": "text", "text": "done"},
        {
            "type": "done",
            "response": LLMResponse(content="done", tool_calls=[], stop_reason="end_turn"),
        },
    ]
    provider = _FakeProvider(turns=[turn1, turn2])
    runtime = _build_runtime(tmp_path, provider)

    async def run() -> list:
        return [ev async for ev in runtime.handle_user_message("call it")]

    events = anyio.run(run)
    kinds = [str(e.kind).split(".")[-1] for e in events]
    # Expected order: user_message → assistant_message(tool_use) → tool_call →
    #                 permission_granted → tool_result → assistant_message(text)
    assert "user_message" in kinds
    assert kinds.count("assistant_message") == 2
    assert "tool_call" in kinds and "tool_result" in kinds
    assert provider.calls == 2
