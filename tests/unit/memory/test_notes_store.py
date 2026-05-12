"""NotesStore round-trip and marker parsing (MEMORY_SPEC §2.3)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from eonlet.memory.notes import NotesStore


def _run(coro):  # type: ignore[no-untyped-def]
    return anyio.run(lambda: coro)


def test_add_then_get_round_trip(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        note = await store.add(id="note-x", content="hello world", title="Hi", tags=["t1"])
        assert note.id == "note-x"
        got = await store.get(id="note-x")
        assert got is not None and got.body == "hello world"
        assert got.tags == ["t1"]
        assert got.title == "Hi"

    anyio.run(go)


def test_add_persists_marker_to_file(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="note-y", content="body", title="T", tags=["a", "b"])

    anyio.run(go)
    raw = (tmp_path / "notes.md").read_text()
    assert "<!-- note id=note-y" in raw
    assert "tags=a,b" in raw
    assert 'title="T"' in raw
    assert "body" in raw


def test_list_filters_by_tag(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="n1", content="a", tags=["x"])
        await store.add(id="n2", content="b", tags=["y"])
        await store.add(id="n3", content="c", tags=["x", "y"])
        only_x = await store.list_notes(tags=["x"])
        only_y = await store.list_notes(tags=["y"])
        assert {n.id for n in only_x} == {"n1", "n3"}
        assert {n.id for n in only_y} == {"n2", "n3"}
        all_ = await store.list_notes()
        assert {n.id for n in all_} == {"n1", "n2", "n3"}

    anyio.run(go)


def test_add_rejects_duplicate_id(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="dup", content="a")
        with pytest.raises(ValueError):
            await store.add(id="dup", content="b")

    anyio.run(go)


def test_update_replaces_body(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="n", content="old", title="T", tags=["t"])
        await store.update(id="n", content="new")
        got = await store.get(id="n")
        assert got is not None
        assert got.body == "new"
        # title/tags should be preserved on update
        assert got.title == "T"
        assert got.tags == ["t"]

    anyio.run(go)


def test_update_missing_raises(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        with pytest.raises(KeyError):
            await store.update(id="nope", content="x")

    anyio.run(go)


def test_delete_round_trip(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="n", content="x")
        assert await store.delete(id="n") is True
        assert await store.delete(id="n") is False
        assert await store.get(id="n") is None

    anyio.run(go)


def test_preamble_preserved_across_writes(tmp_path: Path) -> None:
    # User wrote some markdown by hand before any note was added.
    (tmp_path / "notes.md").write_text(
        "# My personal notes\nstart of day brain dump\n\n", encoding="utf-8"
    )
    store = NotesStore(tmp_path)

    async def go() -> None:
        await store.add(id="n1", content="agent-added")

    anyio.run(go)
    raw = (tmp_path / "notes.md").read_text()
    assert "personal notes" in raw
    assert "start of day brain dump" in raw
    assert "agent-added" in raw


def test_empty_store_returns_empty_list(tmp_path: Path) -> None:
    store = NotesStore(tmp_path)

    async def go() -> None:
        notes = await store.list_notes()
        assert notes == []
        assert await store.get(id="anything") is None
        assert await store.delete(id="anything") is False

    anyio.run(go)
