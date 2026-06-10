"""End-to-end: AgentRuntime injects memory preamble and respects watermark.

Uses ``FakeProvider`` (``fake-echo``) so no API key is needed. The test
seeds memory documents on disk, runs one turn, and inspects what the
provider received via a side-channel.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel

from eonlet.config import load_agent_config
from eonlet.llm.protocol import (
    DoneChunk,
    LLMMessage,
    LLMResponse,
    LLMToolCall,
    StreamChunk,
    TextChunk,
)
from eonlet.memory.watermark import write_watermark
from eonlet.permissions import PermissionGate
from eonlet.runtime.agent import AgentRuntime
from eonlet.runtime.definition import Definition
from eonlet.runtime.events import EventKind, assistant_message, user_message
from eonlet.runtime.store import EventStore
from eonlet.tools import builtin as _builtin  # noqa: F401  (register builtin tools)
from eonlet.tools.protocol import ToolAnnotations, ToolContext, ToolResult
from eonlet.tools.registry import get_registry


class _DestructiveArgs(BaseModel):
    x: str = "y"


class _FakeDestructiveTool:
    name = "_test_destructive"
    description = "a destructive no-op tool for permission tests"
    input_schema = _DestructiveArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: _DestructiveArgs, ctx: ToolContext) -> ToolResult:
        return ToolResult(content="ran")


def _ensure_destructive_tool() -> None:
    reg = get_registry()
    if not reg.has("_test_destructive"):
        reg.register(_FakeDestructiveTool())


class _FakeBroker:
    """Stand-in decision broker that returns a canned answer."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    async def ask(
        self,
        *,
        kind: str,
        prompt: str,
        options: list[str],
        payload: dict[str, Any] | None = None,
        decline: str = "deny",
    ) -> str:
        self.asked.append((kind, prompt))
        return self.answer


class _Recorder:
    """LLM provider stub that captures the messages it receives."""

    name = "recorder"
    model = "recorder"

    def __init__(self) -> None:
        self.last_system: str = ""
        self.last_messages: list[LLMMessage] = []

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.last_system = system
        self.last_messages = list(messages)
        return LLMResponse(content="ok", tool_calls=[], stop_reason="end_turn")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        self.last_system = system
        self.last_messages = list(messages)
        yield TextChunk(type="text", text="ok")
        yield DoneChunk(
            type="done",
            response=LLMResponse(content="ok", tool_calls=[], stop_reason="end_turn"),
        )


def _build_runtime(tmp_path: Path) -> tuple[AgentRuntime, _Recorder]:
    """Spin up a minimal AgentRuntime against an in-memory definition."""
    workspace = tmp_path / "ws"
    memory = tmp_path / "mem"
    workspace.mkdir()
    memory.mkdir()
    # Seed a minimal agent.yaml + system.md
    defn_dir = tmp_path / "defn" / "assistant"
    defn_dir.mkdir(parents=True)
    (defn_dir / "system.md").write_text("you are a test agent")
    (defn_dir / "agent.yaml").write_text(
        "apiVersion: eonlet/v1\n"
        "kind: Agent\n"
        "metadata:\n"
        "  name: assistant\n"
        "  description: t\n"
        "  version: 0.0.1\n"
        "runtime:\n"
        "  model: fake-echo\n"
        "tools:\n"
        "  builtin:\n"
        "    - sleep\n"
        "memory:\n"
        "  enabled: true\n"
        "  episodic:\n"
        "    working_memory_tokens: 1024\n"
        "    keep_recent_messages_min: 1\n"
    )
    cfg = load_agent_config(defn_dir)
    definition = Definition(
        type="assistant",
        path=defn_dir,
        config=cfg,
        system_prompt="you are a test agent",
        custom_tool_paths=[],
        skills={},
    )
    store = EventStore(tmp_path / "state.db")
    rec = _Recorder()
    runtime = AgentRuntime(
        eonlet_id="t.x",
        definition=definition,
        store=store,
        workspace=workspace,
        memory_dir=memory,
        provider=rec,  # type: ignore[arg-type]
        gate=PermissionGate(mode="ask", extra_deny=[], session_attached=True),
    )
    return runtime, rec


def test_system_prompt_contains_memory_preamble(tmp_path: Path) -> None:
    runtime, rec = _build_runtime(tmp_path)
    # Seed an LTM doc — should appear inside <long_term>
    (runtime.memory_dir / "long_term.md").write_text("## user\n- LTM-MARKER")

    async def go() -> None:
        async for _ in runtime.handle_user_message("hello"):
            pass

    anyio.run(go)
    assert "<memory>" in rec.last_system
    assert "LTM-MARKER" in rec.last_system


def test_user_turn_timestamped_by_default(tmp_path: Path) -> None:
    runtime, rec = _build_runtime(tmp_path)

    async def go() -> None:
        async for _ in runtime.handle_user_message("hello world"):
            pass

    anyio.run(go)
    users = [m for m in rec.last_messages if m.role == "user"]
    assert users
    assert users[-1].content.endswith(" hello world")
    assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{2}:\d{2}\] ", users[-1].content)


def test_user_turn_not_timestamped_when_disabled(tmp_path: Path) -> None:
    runtime, rec = _build_runtime(tmp_path)
    runtime.definition.config.memory.inject_turn_timestamps = False

    async def go() -> None:
        async for _ in runtime.handle_user_message("hello world"):
            pass

    anyio.run(go)
    users = [m for m in rec.last_messages if m.role == "user"]
    assert users
    assert users[-1].content == "hello world"


def test_no_preamble_when_subsystem_disabled(tmp_path: Path) -> None:
    # Manually load and tweak config to disable memory.
    workspace = tmp_path / "ws"
    memory = tmp_path / "mem"
    workspace.mkdir()
    memory.mkdir()
    defn_dir = tmp_path / "defn" / "assistant"
    defn_dir.mkdir(parents=True)
    (defn_dir / "system.md").write_text("x")
    (defn_dir / "agent.yaml").write_text(
        "apiVersion: eonlet/v1\n"
        "kind: Agent\n"
        "metadata:\n"
        "  name: assistant\n"
        "  description: t\n"
        "  version: 0.0.1\n"
        "runtime:\n"
        "  model: fake-echo\n"
        "tools:\n"
        "  builtin: []\n"
        "memory:\n"
        "  enabled: false\n"
    )
    cfg = load_agent_config(defn_dir)
    defn = Definition(
        type="assistant",
        path=defn_dir,
        config=cfg,
        system_prompt="x",
        custom_tool_paths=[],
        skills={},
    )
    store = EventStore(tmp_path / "state.db")
    rec = _Recorder()
    runtime = AgentRuntime(
        eonlet_id="t.y",
        definition=defn,
        store=store,
        workspace=workspace,
        memory_dir=memory,
        provider=rec,  # type: ignore[arg-type]
        gate=PermissionGate(mode="ask", extra_deny=[], session_attached=True),
    )
    # Even with a file present on disk:
    (memory / "long_term.md").write_text("HIDDEN")

    async def go() -> None:
        async for _ in runtime.handle_user_message("hi"):
            pass

    anyio.run(go)
    assert "<memory>" not in rec.last_system
    assert "HIDDEN" not in rec.last_system


def test_recent_window_filters_out_messages_below_watermark(tmp_path: Path) -> None:
    runtime, rec = _build_runtime(tmp_path)

    async def go() -> None:
        # Three turns to build up history.
        for _ in range(3):
            async for _ in runtime.handle_user_message("ping"):
                pass

    anyio.run(go)
    pre_count = len(rec.last_messages)
    assert pre_count > 0

    # Advance watermark past everything, then do another turn.
    write_watermark(runtime.memory_dir, runtime.store.latest_id())

    async def go2() -> None:
        async for _ in runtime.handle_user_message("new-pivot"):
            pass

    anyio.run(go2)

    # Window should now contain ONLY the latest user_message (assistant reply
    # comes after, but the recorder captures pre-response).
    user_msgs = [m for m in rec.last_messages if m.role == "user"]
    assert all("ping" not in m.content for m in user_msgs)
    assert any("new-pivot" in m.content for m in user_msgs)


# ── interactive permission confirm (ADR-0006 M2) ────────────────────────────


def test_destructive_tool_prompts_then_approves(tmp_path: Path) -> None:
    _ensure_destructive_tool()
    runtime, _ = _build_runtime(tmp_path)  # gate is ask-mode + session_attached
    broker = _FakeBroker("approve")
    runtime.decision_broker = broker
    call = LLMToolCall(id="c1", name="_test_destructive", arguments={"x": "y"})

    events: list[Any] = []

    async def go() -> None:
        async for ev in runtime._execute_tool_call(call):
            events.append(ev)

    anyio.run(go)
    assert broker.asked and broker.asked[0][0] == "permission"
    # Permission events are recorded (not yielded) — inspect the store.
    stored = [e.kind for e in runtime.store.read()]
    assert EventKind.PERMISSION_GRANTED in stored
    # Approved → the tool ran (its TOOL_RESULT is yielded).
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert results and results[-1].payload.get("output") == "ran"


def test_destructive_tool_prompts_then_denies(tmp_path: Path) -> None:
    _ensure_destructive_tool()
    runtime, _ = _build_runtime(tmp_path)
    broker = _FakeBroker("deny")
    runtime.decision_broker = broker
    call = LLMToolCall(id="c1", name="_test_destructive", arguments={"x": "y"})

    events: list[Any] = []

    async def go() -> None:
        async for ev in runtime._execute_tool_call(call):
            events.append(ev)

    anyio.run(go)
    assert broker.asked  # user was still asked
    stored = [e.kind for e in runtime.store.read()]
    assert EventKind.PERMISSION_DENIED in stored
    # Declined → the tool did NOT run; the yielded result is the denial error.
    errs = [e for e in events if e.kind == EventKind.TOOL_ERROR]
    assert errs and "permission denied" in (errs[-1].payload.get("output") or "")


def test_destructive_tool_denied_without_broker(tmp_path: Path) -> None:
    # No broker (headless/test) → a needs_prompt decision falls back to deny.
    _ensure_destructive_tool()
    runtime, _ = _build_runtime(tmp_path)
    runtime.decision_broker = None
    call = LLMToolCall(id="c1", name="_test_destructive", arguments={"x": "y"})

    events: list[Any] = []

    async def go() -> None:
        async for ev in runtime._execute_tool_call(call):
            events.append(ev)

    anyio.run(go)
    assert EventKind.PERMISSION_DENIED in [e.kind for e in runtime.store.read()]
    errs = [e for e in events if e.kind == EventKind.TOOL_ERROR]
    assert errs and "permission denied" in (errs[-1].payload.get("output") or "")


def test_record_stamps_task_scope(tmp_path: Path) -> None:
    # ADR-0009 §2: _record stamps the task scope onto conversation events from
    # current_task_id; chat turns stay None.
    runtime, _ = _build_runtime(tmp_path)

    async def go() -> None:
        await runtime._record(user_message("chat"))  # current_task_id None
        runtime.current_task_id = "task-a"
        await runtime._record(user_message("task framing"))
        await runtime._record(assistant_message("working on it"))
        runtime.current_task_id = None

    anyio.run(go)
    events = runtime.store.read()
    by_content = {e.payload.get("content"): e.task_id for e in events}
    assert by_content["chat"] is None
    assert by_content["task framing"] == "task-a"
    assert by_content["working on it"] == "task-a"


def test_llm_window_is_scoped(tmp_path: Path) -> None:
    # ADR-0009 §2: the LLM window is the current scope only — chat and task
    # internals never bleed into each other.
    runtime, _ = _build_runtime(tmp_path)

    async def seed() -> None:
        await runtime._record(user_message("CHAT-ONE"))
        await runtime._record(assistant_message("chat-reply"))
        runtime.current_task_id = "task-a"
        await runtime._record(user_message("TASK-FRAMING"))
        await runtime._record(assistant_message("task-reply"))
        runtime.current_task_id = None

    anyio.run(seed)

    # Chat scope: sees chat turns, not task internals.
    runtime.current_task_id = None
    chat_window = " ".join(m.content for m in runtime._build_llm_messages())
    assert "CHAT-ONE" in chat_window
    assert "TASK-FRAMING" not in chat_window

    # Task scope: sees the task's own turns, not chat.
    runtime.current_task_id = "task-a"
    task_window = " ".join(m.content for m in runtime._build_llm_messages())
    assert "TASK-FRAMING" in task_window
    assert "CHAT-ONE" not in task_window
    runtime.current_task_id = None


def test_task_turns_excluded_from_episodic_compaction(tmp_path: Path) -> None:
    # ADR-0009 §5: tier-1 source is chat scope only — task turns never enter STM.
    from eonlet.memory.injection import chat_scope_only

    runtime, _ = _build_runtime(tmp_path)

    async def seed() -> None:
        await runtime._record(user_message("chat-a"))
        runtime.current_task_id = "task-a"
        await runtime._record(user_message("task-a-turn"))
        runtime.current_task_id = None
        await runtime._record(user_message("chat-b"))

    anyio.run(seed)
    chat = chat_scope_only(runtime.store.read())
    contents = [e.payload.get("content") for e in chat]
    assert "chat-a" in contents and "chat-b" in contents
    assert "task-a-turn" not in contents


def test_ensure_framing_stores_down_tree_trace(tmp_path: Path) -> None:
    # ADR-0009 M3: a child's first dispatch computes & stores a decision trace
    # compressed from the parent's scope (here the _Recorder returns "ok").
    from eonlet.runtime.events import task_created
    from eonlet.worker.main import _ensure_framing

    runtime, _ = _build_runtime(tmp_path)
    runtime.definition.config.memory.compaction_model = "fake-echo"  # reuse the recorder

    async def go() -> Any:
        await runtime._record(
            task_created(id="root", content="build", goal="build it", origin="user")
        )
        runtime.current_task_id = "root"
        await runtime._record(user_message("framing"))
        await runtime._record(assistant_message("decided to use Postgres"))
        runtime.current_task_id = None
        await runtime._record(
            task_created(id="kid", content="schema", goal="design schema", parent_id="root")
        )
        kid = runtime.task_forest.get("kid")
        await _ensure_framing(runtime, kid)
        return kid

    kid = anyio.run(go)
    assert kid is not None and kid.framing  # non-empty trace was stored


def test_ensure_framing_skips_trigger_root(tmp_path: Path) -> None:
    # A trigger-origin root has nothing that decomposed it → no framing.
    from eonlet.runtime.events import task_created
    from eonlet.worker.main import _ensure_framing

    runtime, _ = _build_runtime(tmp_path)
    runtime.definition.config.memory.compaction_model = "fake-echo"

    async def go() -> Any:
        await runtime._record(
            task_created(id="r", content="cron job", goal="hourly", origin="trigger")
        )
        t = runtime.task_forest.get("r")
        await _ensure_framing(runtime, t)
        return t

    t = anyio.run(go)
    assert t is not None and t.framing == ""


def test_task_scope_compaction_folds_and_prunes(tmp_path: Path) -> None:
    # ADR-0009 M4: when a task's own-scope window exceeds budget, _maybe_compact_task
    # folds older turns into the brief, advances the brief watermark (pruning them
    # from the window), and the brief surfaces in the system prompt.
    from eonlet.runtime.events import task_created
    from eonlet.worker.main import _maybe_compact_task

    runtime, _ = _build_runtime(tmp_path)
    runtime.definition.config.memory.compaction_model = "fake-echo"  # reuse the recorder
    runtime.definition.config.memory.episodic.working_memory_tokens = 5  # tiny → trips easily

    async def go() -> Any:
        await runtime._record(task_created(id="t", content="job", goal="do the job"))
        runtime.current_task_id = "t"
        await runtime._record(user_message("task framing"))
        for i in range(6):
            await runtime._record(assistant_message(f"step {i} did some real work here"))
        await _maybe_compact_task(runtime, "t")
        return runtime.task_forest.get("t")

    t = anyio.run(go)
    assert t is not None
    assert t.brief_watermark > 0  # watermark advanced → older turns folded
    assert t.progress_summary  # cumulative brief written

    # The window for the task scope now excludes the folded older turns (the
    # framing + early steps), keeping only the recent tail…
    runtime.current_task_id = "t"
    window_text = " ".join(m.content for m in runtime._build_llm_messages())
    assert "task framing" not in window_text
    assert "step 0 did some real work" not in window_text
    # …and the brief is carried in the system prompt instead.
    assert "<task_progress>" in runtime._build_system_prompt()
    runtime.current_task_id = None
