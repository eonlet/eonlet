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
    assert forest.get(cid).priority == 3  # type: ignore[union-attr]
    # Parent is not a pending leaf; the child is.
    assert [t.id for t in forest.pending_leaves()] == [cid]


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
