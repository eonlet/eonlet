"""TaskStore round-trip + JSONL persistence (ADR-0005; was TodosStore)."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from eonlet.tasks import TaskStore


def test_add_then_list_pending(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="task-1", content="do a thing")
        pending = await store.list_tasks(status="pending")
        assert [t.id for t in pending] == ["task-1"]
        assert pending[0].status == "pending"
        assert pending[0].created_at  # ISO timestamp present

    anyio.run(go)


def test_done_transitions_status_and_records_time(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="t", content="x")
        done = await store.mark_done(id="t")
        assert done.status == "done"
        assert done.done_at is not None and "T" in done.done_at  # ISO-ish

    anyio.run(go)


def test_cancel_transitions_status(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="t", content="x")
        cancelled = await store.mark_cancelled(id="t")
        assert cancelled.status == "cancelled"

    anyio.run(go)


def test_list_status_filter(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="a", content="A")
        await store.add(id="b", content="B")
        await store.mark_done(id="b")
        assert {t.id for t in await store.list_tasks(status="pending")} == {"a"}
        assert {t.id for t in await store.list_tasks(status="done")} == {"b"}
        assert {t.id for t in await store.list_tasks(status="all")} == {"a", "b"}

    anyio.run(go)


def test_update_fields(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="t", content="old", tags=["a"])
        updated = await store.update(
            id="t", content="new", due="2026-06-01T12:00:00+08:00", tags=["b", "c"]
        )
        assert updated.content == "new"
        assert updated.due == "2026-06-01T12:00:00+08:00"
        assert updated.tags == ["b", "c"]

    anyio.run(go)


def test_delete(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="t", content="x")
        assert await store.delete(id="t") is True
        assert await store.delete(id="t") is False

    anyio.run(go)


def test_jsonl_format_on_disk(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="t1", content="a")
        await store.add(id="t2", content="b", due="2026-06-01T00:00:00+00:00", tags=["x"])

    anyio.run(go)
    raw = (tmp_path / "todos.jsonl").read_text()
    lines = [line for line in raw.splitlines() if line.strip()]
    assert len(lines) == 2
    objs = [json.loads(line) for line in lines]
    assert {o["id"] for o in objs} == {"t1", "t2"}
    by_id = {o["id"]: o for o in objs}
    assert by_id["t2"]["due"] == "2026-06-01T00:00:00+00:00"
    assert by_id["t2"]["tags"] == ["x"]


def test_corrupt_lines_skipped_on_read(tmp_path: Path) -> None:
    (tmp_path / "todos.jsonl").write_text(
        '{"id":"good","content":"x","status":"pending"}\n'
        "not-json garbage\n"
        '{"id":"good2","content":"y","status":"pending"}\n',
        encoding="utf-8",
    )
    store = TaskStore(tmp_path)

    async def go() -> None:
        tasks = await store.list_tasks(status="all")
        assert {t.id for t in tasks} == {"good", "good2"}

    anyio.run(go)


def test_add_rejects_duplicate_id(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        await store.add(id="dup", content="x")
        with pytest.raises(ValueError):
            await store.add(id="dup", content="y")

    anyio.run(go)


def test_mark_done_missing_raises(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)

    async def go() -> None:
        with pytest.raises(KeyError):
            await store.mark_done(id="nope")

    anyio.run(go)


def test_unknown_status_in_file_falls_back_to_pending(tmp_path: Path) -> None:
    (tmp_path / "todos.jsonl").write_text(
        '{"id":"t","content":"x","status":"???"}\n', encoding="utf-8"
    )
    store = TaskStore(tmp_path)

    async def go() -> None:
        tasks = await store.list_tasks(status="pending")
        assert [t.id for t in tasks] == ["t"]

    anyio.run(go)
