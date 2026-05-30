"""`task` builtin tool — action dispatch and event emission (ADR-0005)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.task import TaskArgs, TaskTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path) -> tuple[ToolContext, list[Event]]:
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        stamped = ev.model_copy(update={"id": len(captured) + 1})
        captured.append(stamped)
        return stamped

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path / "memory",
        tasks_dir=tmp_path / "tasks",
        skills={},
        env={},
        record_event=record,
    )
    return ctx, captured


def test_task_lifecycle(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
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
    assert EventKind.TASK_ADDED in kinds
    assert EventKind.TASK_UPDATED in kinds
    assert EventKind.TASK_DELETED in kinds


def test_task_cancel(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> Any:
        out = await tool(TaskArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        c = await tool(TaskArgs(action="cancel", id=tid), ctx)
        assert not c.is_error
        cancelled = await tool(TaskArgs(action="list", status="cancelled"), ctx)
        assert tid in cancelled.content

    anyio.run(go)
    assert EventKind.TASK_UPDATED in [e.kind for e in captured]


def test_task_writes_under_tasks_dir(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = TaskTool()
    anyio.run(lambda: tool(TaskArgs(action="add", content="persist me"), ctx))
    # File lands in tasks/, NOT memory/.
    assert (tmp_path / "tasks" / "todos.jsonl").exists()
    assert not (tmp_path / "memory" / "todos.jsonl").exists()


def test_task_update_requires_at_least_one_field(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
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
    ctx, _ = _ctx(tmp_path)
    tool = TaskTool()

    async def go() -> None:
        out = await tool(TaskArgs(action="done"), ctx)
        assert out.is_error and "id" in out.content
        out2 = await tool(TaskArgs(action="done", id="nope"), ctx)
        assert out2.is_error and "no such" in out2.content

    anyio.run(go)


def test_task_fallback_tasks_dir_from_memory_sibling(tmp_path: Path) -> None:
    # When tasks_dir is None, the tool falls back to memory_dir.parent / "tasks".
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path / "eonlet" / "memory",
        skills={},
        env={},
        record_event=record,
    )
    tool = TaskTool()
    anyio.run(lambda: tool(TaskArgs(action="add", content="x"), ctx))
    assert (tmp_path / "eonlet" / "tasks" / "todos.jsonl").exists()
