"""Worker task-run guards: crash recovery, cancel-aware pause, budget, cheap
user-input checkpoints (design-review fixes P2/P10/P11/P8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio

from eonlet.config import load_agent_config
from eonlet.llm.protocol import DoneChunk, LLMMessage, LLMResponse, StreamChunk, TextChunk
from eonlet.permissions import PermissionGate
from eonlet.runtime.agent import AgentRuntime
from eonlet.runtime.definition import Definition
from eonlet.runtime.events import assistant_message, task_created, task_transitioned, user_message
from eonlet.runtime.store import EventStore
from eonlet.worker.main import _checkpoint_summary, _make_pause_check, _recover_stale_tasks


class _Provider:
    """Stub provider; would be the compaction provider if the LLM path ran."""

    name = "fake"
    model = "fake-echo"
    called = False

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        _Provider.called = True
        return LLMResponse(content="LLM-BRIEF", tool_calls=[], stop_reason="end_turn")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        yield TextChunk(type="text", text="ok")
        yield DoneChunk(
            type="done",
            response=LLMResponse(content="ok", tool_calls=[], stop_reason="end_turn"),
        )


def _build_runtime(tmp_path: Path) -> AgentRuntime:
    workspace = tmp_path / "ws"
    memory = tmp_path / "mem"
    workspace.mkdir()
    memory.mkdir()
    defn_dir = tmp_path / "defn" / "assistant"
    defn_dir.mkdir(parents=True)
    (defn_dir / "system.md").write_text("test agent")
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
        # compaction_model == runtime.model so _resolve_compaction_provider
        # returns runtime.provider (our stub) — proving structural_only skips
        # an *available* LLM, not just a missing one.
        "  compaction_model: fake-echo\n"
    )
    cfg = load_agent_config(defn_dir)
    definition = Definition(
        type="assistant",
        path=defn_dir,
        config=cfg,
        system_prompt="test agent",
        custom_tool_paths=[],
        skills={},
    )
    return AgentRuntime(
        eonlet_id="t.x",
        definition=definition,
        store=EventStore(tmp_path / "state.db"),
        workspace=workspace,
        memory_dir=memory,
        provider=_Provider(),  # type: ignore[arg-type]
        gate=PermissionGate(mode="ask", extra_deny=[], session_attached=True),
    )


def _sched(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "preempt": "off",
        "preempt_cooldown": "0s",
        "per_task_budget_tokens": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


async def _mk_active_task(runtime: AgentRuntime, tid: str = "task-1") -> Any:
    await runtime._record(task_created(id=tid, content="do the thing", goal="the goal"))
    await runtime._record(
        task_transitioned(id=tid, from_state="pending", to_state="active", reason="scheduled")
    )
    return runtime.task_forest.get(tid)


# ── P2: crash recovery ───────────────────────────────────────────────────────


def test_recover_stale_tasks_requeues_active(tmp_path: Path) -> None:
    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        await _mk_active_task(runtime)
        await _recover_stale_tasks(runtime)
        t = runtime.task_forest.get("task-1")
        assert t is not None and t.status == "pending"

    anyio.run(go)


def test_recover_stale_tasks_noop_without_active(tmp_path: Path) -> None:
    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        await runtime._record(task_created(id="task-1", content="x"))
        before = runtime.store.latest_id()
        await _recover_stale_tasks(runtime)
        assert runtime.store.latest_id() == before  # no events appended

    anyio.run(go)


# ── P10: cancel/delete a running task ends the run ───────────────────────────


def test_pause_check_yields_when_task_cancelled(tmp_path: Path) -> None:
    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        task = await _mk_active_task(runtime)
        preempt_to: dict[str, str] = {}
        check = _make_pause_check(runtime, task, _sched(), preempt_to)
        assert await check() is False  # running normally
        await runtime._record(
            task_transitioned(
                id="task-1", from_state="active", to_state="cancelled", reason="cli:cancel"
            )
        )
        assert await check() is True
        assert preempt_to.get("gone") == "1"

    anyio.run(go)


def test_pause_check_yields_when_task_deleted(tmp_path: Path) -> None:
    from eonlet.runtime.events import task_deleted

    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        task = await _mk_active_task(runtime)
        preempt_to: dict[str, str] = {}
        check = _make_pause_check(runtime, task, _sched(), preempt_to)
        await runtime._record(task_deleted(id="task-1"))
        assert await check() is True
        assert preempt_to.get("gone") == "1"

    anyio.run(go)


# ── P11: budget accumulates incrementally across checks ──────────────────────


def test_pause_check_budget_accumulates(tmp_path: Path) -> None:
    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        task = await _mk_active_task(runtime)
        preempt_to: dict[str, str] = {}
        check = _make_pause_check(runtime, task, _sched(per_task_budget_tokens=10), preempt_to)
        await runtime._record(assistant_message("step 1", tokens_in=3, tokens_out=3))
        assert await check() is False  # 6 < 10
        await runtime._record(assistant_message("step 2", tokens_in=3, tokens_out=3))
        assert await check() is True  # 12 ≥ 10 — earlier spend was remembered
        assert not preempt_to  # budget end is a yield, not a preemption

    anyio.run(go)


# ── P8: user-input pause takes the cheap structural checkpoint ───────────────


def test_structural_only_checkpoint_skips_llm(tmp_path: Path) -> None:
    async def go() -> None:
        runtime = _build_runtime(tmp_path)
        await _mk_active_task(runtime)
        # Give the task some own-scope turns so the LLM path *would* run.
        runtime.current_task_id = "task-1"
        await runtime._record(user_message("go"))
        await runtime._record(assistant_message("worked on it"))
        runtime.current_task_id = None

        _Provider.called = False
        brief, boundary = await _checkpoint_summary(runtime, "task-1", structural_only=True)
        assert _Provider.called is False
        assert boundary is None  # never advance the watermark from a lossy brief
        assert "the goal" in brief and "worked on it" in brief

        # And the LLM path still runs when not structural_only.
        brief2, boundary2 = await _checkpoint_summary(runtime, "task-1")
        assert _Provider.called is True
        assert brief2 == "LLM-BRIEF" and boundary2 is not None

    anyio.run(go)
