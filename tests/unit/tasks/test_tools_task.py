"""`task` builtin tool — event-only mutation + forest reads (ADR-0007)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from eonlet.runtime.events import Event, EventKind
from eonlet.tasks import TaskForest, reduce_task
from eonlet.tools.builtin.task import TaskArgs, TaskTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path) -> tuple[ToolContext, list[Event], TaskForest]:
    """A context that mimics the runtime: record_event appends + folds into a
    forest, and read_tasks returns it — so the tool's list/transition paths see
    their own writes, exactly as in the live agent loop."""
    captured: list[Event] = []
    forest = TaskForest()

    async def record(ev: Event) -> Event:
        stamped = ev.model_copy(update={"id": len(captured) + 1})
        captured.append(stamped)
        reduce_task(forest, stamped)
        return stamped

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
        tasks_dir=tmp_path / "tasks",
        skills={},
        env={},
        record_event=record,
        read_tasks=lambda: forest,
    )
    return ctx, captured, forest


def test_task_lifecycle(tmp_path: Path) -> None:
    ctx, captured, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        out = await tool(TaskArgs(action="add", content="do x"), ctx)
        assert not out.is_error and out.structured_output is not None
        tid = out.structured_output["id"]
        listed = await tool(TaskArgs(action="list"), ctx)
        assert "do x" in listed.content
        done = await tool(TaskArgs(action="done", id=tid), ctx)
        assert not done.is_error
        empty = await tool(TaskArgs(action="list", status="pending"), ctx)
        assert "no pending" in empty.content
        done_list = await tool(TaskArgs(action="list", status="done"), ctx)
        assert "do x" in done_list.content
        rm = await tool(TaskArgs(action="delete", id=tid), ctx)
        assert not rm.is_error

    anyio.run(go)
    kinds = [e.kind for e in captured]
    assert EventKind.TASK_CREATED in kinds
    assert EventKind.TASK_TRANSITIONED in kinds
    assert EventKind.TASK_DELETED in kinds
    # No JSONL store is written any more.
    assert not (tmp_path / "tasks" / "todos.jsonl").exists()


def test_task_subtask_tree(tmp_path: Path) -> None:
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        root = await tool(TaskArgs(action="add", content="project"), ctx)
        rid = root.structured_output["id"]  # type: ignore[index]
        child = await tool(
            TaskArgs(action="add", content="subtask", parent_id=rid, priority=3), ctx
        )
        assert not child.is_error
        cid = child.structured_output["id"]  # type: ignore[index]
        return rid, cid

    rid, cid = anyio.run(go)
    assert forest.get(cid).parent_id == rid  # type: ignore[union-attr]
    # ADR-0008 §2: a subtask's priority has no scheduling effect, so the tool
    # forces it to 0 (priority schedules only at the root); origin → "agent".
    assert forest.get(cid).priority == 0  # type: ignore[union-attr]
    assert forest.get(cid).origin == "agent"  # type: ignore[union-attr]
    # Parent is not a pending leaf; the child is.
    assert [t.id for t in forest.pending_leaves()] == [cid]


def test_root_origin_from_turn_origin(tmp_path: Path) -> None:
    # ADR-0008 §5: a root created during a user turn is origin="user"; during a
    # cron turn it is origin="trigger". (Subtasks are always "agent" — covered by
    # test_task_subtask_tree.)
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        r1 = await tool(TaskArgs(action="add", content="user root"), ctx)  # default user turn
        ctx.turn_origin = "trigger"
        r2 = await tool(TaskArgs(action="add", content="cron root"), ctx)
        return r1.structured_output["id"], r2.structured_output["id"]  # type: ignore[index]

    uid, tid = anyio.run(go)
    assert forest.get(uid).origin == "user"  # type: ignore[union-attr]
    assert forest.get(tid).origin == "trigger"  # type: ignore[union-attr]


def test_add_enforces_depth_cap(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    ctx.max_task_depth = 2  # at most 2 levels
    tool = TaskTool()

    async def go() -> Any:
        root = await tool(TaskArgs(action="add", content="root"), ctx)
        rid = root.structured_output["id"]  # type: ignore[index]
        child = await tool(TaskArgs(action="add", content="child", parent_id=rid), ctx)
        cid = child.structured_output["id"]  # type: ignore[index]
        # A grandchild would be depth 3 → rejected.
        return await tool(TaskArgs(action="add", content="grandchild", parent_id=cid), ctx)

    out = anyio.run(go)
    assert out.is_error and "depth" in out.content


def test_add_enforces_fanout_cap(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    ctx.max_task_fanout = 1  # one child per node
    tool = TaskTool()

    async def go() -> Any:
        root = await tool(TaskArgs(action="add", content="root"), ctx)
        rid = root.structured_output["id"]  # type: ignore[index]
        await tool(TaskArgs(action="add", content="c1", parent_id=rid), ctx)
        return await tool(TaskArgs(action="add", content="c2", parent_id=rid), ctx)

    out = anyio.run(go)
    assert out.is_error and "subtasks per task" in out.content


def test_task_add_rejects_unknown_parent(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        return await tool(TaskArgs(action="add", content="x", parent_id="nope"), ctx)

    out = anyio.run(go)
    assert out.is_error and "no such parent" in out.content


def test_task_cancel(tmp_path: Path) -> None:
    ctx, captured, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        out = await tool(TaskArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        c = await tool(TaskArgs(action="cancel", id=tid), ctx)
        assert not c.is_error
        cancelled = await tool(TaskArgs(action="list", status="cancelled"), ctx)
        assert tid in cancelled.content

    anyio.run(go)
    assert EventKind.TASK_TRANSITIONED in [e.kind for e in captured]


def test_task_done_twice_is_idempotent(tmp_path: Path) -> None:
    ctx, captured, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> None:
        out = await tool(TaskArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        await tool(TaskArgs(action="done", id=tid), ctx)
        again = await tool(TaskArgs(action="done", id=tid), ctx)
        # Already done: friendly no-op, not an error, and no redundant event.
        assert not again.is_error and "already done" in again.content

    anyio.run(go)
    assert [e.kind for e in captured].count(EventKind.TASK_TRANSITIONED) == 1


def test_task_cancel_after_done_rejected(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> None:
        out = await tool(TaskArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        await tool(TaskArgs(action="done", id=tid), ctx)
        bad = await tool(TaskArgs(action="cancel", id=tid), ctx)
        assert bad.is_error and "cannot" in bad.content  # done is terminal

    anyio.run(go)


def test_task_update_requires_at_least_one_field(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        out = await tool(TaskArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        bad = await tool(TaskArgs(action="update", id=tid), ctx)
        assert bad.is_error and "at least one" in bad.content

    anyio.run(go)


def test_task_list_unknown_status_invalid_at_schema(tmp_path: Path) -> None:
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        TaskArgs(action="list", status="bogus")  # type: ignore[arg-type]


def test_task_done_missing_id(tmp_path: Path) -> None:
    ctx, _, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> None:
        out = await tool(TaskArgs(action="done"), ctx)
        assert out.is_error and "id" in out.content
        out2 = await tool(TaskArgs(action="done", id="nope"), ctx)
        assert out2.is_error and "no such" in out2.content

    anyio.run(go)


def test_done_uses_current_task_id_when_omitted(tmp_path: Path) -> None:
    ctx, captured, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> str:
        out = await tool(TaskArgs(action="add", content="the task"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        # Simulate a task-scoped run: the scheduler sets the current task id.
        ctx.current_task_id = tid
        done = await tool(TaskArgs(action="done", result="all good"), ctx)  # no explicit id
        assert not done.is_error and "done" in done.content
        return tid

    tid = anyio.run(go)
    t = forest.get(tid)
    assert t is not None and t.status == "done"
    tr = next(e for e in captured if e.kind == EventKind.TASK_TRANSITIONED)
    assert tr.payload.get("result") == "all good"


def test_add_defaults_parent_to_current_task(tmp_path: Path) -> None:
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> tuple[str, str]:
        root = await tool(TaskArgs(action="add", content="parent"), ctx)
        rid = root.structured_output["id"]  # type: ignore[index]
        ctx.current_task_id = rid  # inside the parent's run
        child = await tool(TaskArgs(action="add", content="subtask"), ctx)  # no parent_id
        cid = child.structured_output["id"]  # type: ignore[index]
        return rid, cid

    rid, cid = anyio.run(go)
    assert forest.get(cid).parent_id == rid  # type: ignore[union-attr]


def test_task_no_event_sink_errors(tmp_path: Path) -> None:
    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
        skills={},
        env={},
    )
    tool = TaskTool()
    out = anyio.run(lambda: tool(TaskArgs(action="add", content="x"), ctx))
    assert out.is_error and "no event sink" in out.content


def test_done_in_own_scoped_run_requires_result(tmp_path: Path) -> None:
    # ADR-0009 upward flow: the result is the only payload that flows up, so a
    # task-scoped run finishing its own task must supply one.
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> str:
        out = await tool(TaskArgs(action="add", content="the task"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        ctx.current_task_id = tid
        bare = await tool(TaskArgs(action="done"), ctx)
        assert bare.is_error and "result" in bare.content
        ok = await tool(TaskArgs(action="done", result="shipped"), ctx)
        assert not ok.is_error
        return tid

    tid = anyio.run(go)
    assert forest.get(tid).status == "done"  # type: ignore[union-attr]


def test_done_from_chat_scope_result_optional(tmp_path: Path) -> None:
    # Ticking a task off in chat (current_task_id None, or a different task)
    # stays frictionless.
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> str:
        out = await tool(TaskArgs(action="add", content="errand"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        done = await tool(TaskArgs(action="done", id=tid), ctx)
        assert not done.is_error
        return tid

    tid = anyio.run(go)
    assert forest.get(tid).status == "done"  # type: ignore[union-attr]


def test_task_resume_requeues_suspended(tmp_path: Path) -> None:
    from eonlet.runtime.events import task_transitioned

    ctx, _captured, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> str:
        out = await tool(TaskArgs(action="add", content="long job"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        # Simulate the scheduler suspending a yielded task.
        await ctx.record_event(  # type: ignore[misc]
            task_transitioned(id=tid, from_state="pending", to_state="suspended", reason="yielded")
        )
        not_suspended = await tool(TaskArgs(action="resume"), ctx)
        assert not_suspended.is_error  # id required
        resumed = await tool(TaskArgs(action="resume", id=tid), ctx)
        assert not resumed.is_error
        again = await tool(TaskArgs(action="resume", id=tid), ctx)
        assert again.is_error and "not suspended" in again.content
        return tid

    tid = anyio.run(go)
    assert forest.get(tid).status == "pending"  # type: ignore[union-attr]


def test_update_priority_on_subtask_rejected(tmp_path: Path) -> None:
    # Plan §5.7: a subtask's priority has no scheduling effect (root-only,
    # ADR-0008 §2) — storing one would mislead, so the update is refused.
    ctx, _, forest = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> tuple[str, str]:
        root = await tool(TaskArgs(action="add", content="parent"), ctx)
        rid = root.structured_output["id"]  # type: ignore[index]
        child = await tool(TaskArgs(action="add", content="part", parent_id=rid), ctx)
        cid = child.structured_output["id"]  # type: ignore[index]
        res = await tool(TaskArgs(action="update", id=cid, priority=5), ctx)
        assert res.is_error and "priority" in res.content
        ok = await tool(TaskArgs(action="update", id=rid, priority=5), ctx)
        assert not ok.is_error
        return rid, cid

    rid, cid = anyio.run(go)
    assert forest.get(cid).priority == 0  # type: ignore[union-attr]
    assert forest.get(rid).priority == 5  # type: ignore[union-attr]
