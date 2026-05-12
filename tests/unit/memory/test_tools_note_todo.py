"""`note` and `todo` builtin tools — action dispatch and event emission."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.note import NoteArgs, NoteTool
from eonlet.tools.builtin.todo import TodoArgs, TodoTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path) -> tuple[ToolContext, list[Event]]:
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        # Simulate the runtime stamping an id.
        stamped = ev.model_copy(update={"id": len(captured) + 1})
        captured.append(stamped)
        return stamped

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
        record_event=record,
    )
    return ctx, captured


# ── note ───────────────────────────────────────────────────────────────────


def test_note_add_then_list(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = NoteTool()

    async def go() -> None:
        added = await tool(NoteArgs(action="add", content="hello", title="t"), ctx)
        assert not added.is_error
        listed = await tool(NoteArgs(action="list"), ctx)
        assert "hello" in listed.content

    anyio.run(go)
    kinds = [e.kind for e in captured]
    assert EventKind.MEM_NOTE_ADDED in kinds


def test_note_get_and_update(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = NoteTool()

    async def go() -> Any:
        out = await tool(NoteArgs(action="add", content="orig"), ctx)
        assert not out.is_error and out.structured_output is not None
        nid = out.structured_output["id"]
        got = await tool(NoteArgs(action="get", id=nid), ctx)
        assert "orig" in got.content
        upd = await tool(NoteArgs(action="update", id=nid, content="new"), ctx)
        assert not upd.is_error
        got2 = await tool(NoteArgs(action="get", id=nid), ctx)
        assert "new" in got2.content

    anyio.run(go)
    assert EventKind.MEM_NOTE_UPDATED in [e.kind for e in captured]


def test_note_delete(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = NoteTool()

    async def go() -> Any:
        out = await tool(NoteArgs(action="add", content="x"), ctx)
        nid = out.structured_output["id"]  # type: ignore[index]
        rm = await tool(NoteArgs(action="delete", id=nid), ctx)
        assert not rm.is_error
        miss = await tool(NoteArgs(action="get", id=nid), ctx)
        assert miss.is_error

    anyio.run(go)
    assert EventKind.MEM_NOTE_DELETED in [e.kind for e in captured]


def test_note_missing_id_rejected(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = NoteTool()

    async def go() -> None:
        out = await tool(NoteArgs(action="get"), ctx)
        assert out.is_error and "id" in out.content

    anyio.run(go)


def test_note_add_requires_content(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = NoteTool()

    async def go() -> None:
        out = await tool(NoteArgs(action="add"), ctx)
        assert out.is_error and "content" in out.content

    anyio.run(go)


# ── todo ───────────────────────────────────────────────────────────────────


def test_todo_lifecycle(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = TodoTool()

    async def go() -> Any:
        out = await tool(TodoArgs(action="add", content="do x"), ctx)
        assert not out.is_error and out.structured_output is not None
        tid = out.structured_output["id"]
        listed = await tool(TodoArgs(action="list"), ctx)
        assert "do x" in listed.content
        done = await tool(TodoArgs(action="done", id=tid), ctx)
        assert not done.is_error
        empty = await tool(TodoArgs(action="list", status="pending"), ctx)
        assert "no pending" in empty.content
        done_list = await tool(TodoArgs(action="list", status="done"), ctx)
        assert "do x" in done_list.content
        rm = await tool(TodoArgs(action="delete", id=tid), ctx)
        assert not rm.is_error

    anyio.run(go)
    kinds = [e.kind for e in captured]
    assert EventKind.MEM_TODO_ADDED in kinds
    # mark_done and delete both emit MEM_TODO_UPDATED / DELETED respectively
    assert EventKind.MEM_TODO_UPDATED in kinds
    assert EventKind.MEM_TODO_DELETED in kinds


def test_todo_update_requires_at_least_one_field(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = TodoTool()

    async def go() -> Any:
        out = await tool(TodoArgs(action="add", content="x"), ctx)
        tid = out.structured_output["id"]  # type: ignore[index]
        bad = await tool(TodoArgs(action="update", id=tid), ctx)
        assert bad.is_error and "at least one" in bad.content

    anyio.run(go)


def test_todo_list_unknown_status_invalid_at_schema(tmp_path: Path) -> None:
    # status is a Literal — pydantic rejects unknown values at parse time.
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        TodoArgs(action="list", status="bogus")  # type: ignore[arg-type]


def test_todo_done_missing_id(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = TodoTool()

    async def go() -> None:
        out = await tool(TodoArgs(action="done"), ctx)
        assert out.is_error and "id" in out.content
        out2 = await tool(TodoArgs(action="done", id="nope"), ctx)
        assert out2.is_error and "no such" in out2.content

    anyio.run(go)
