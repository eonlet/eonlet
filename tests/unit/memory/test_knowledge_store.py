"""KnowledgeStore — CRUD, index sync, path safety, tree fallback (ADR-0005)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from eonlet.errors import KnowledgeError, KnowledgePathError
from eonlet.memory.knowledge import KnowledgeStore, _derive_title, _normalize_rel
from eonlet.memory.paths import knowledge_index_path, knowledge_root


def _store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(tmp_path)


# ── path safety ──────────────────────────────────────────────────────────────


def test_normalize_rel_cleans_dot_segments() -> None:
    assert _normalize_rel("./rules/testing.md") == "rules/testing.md"
    assert _normalize_rel("a//b/c.md") == "a/b/c.md"


@pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "../escape.md", "a/../../b.md"])
def test_normalize_rel_rejects_escapes(bad: str) -> None:
    with pytest.raises(KnowledgePathError):
        _normalize_rel(bad)


def test_derive_title_from_stem() -> None:
    assert _derive_title("rules/testing.md") == "Testing"
    assert _derive_title("auth-rewrite.md") == "Auth Rewrite"
    assert _derive_title("dir/notes_two.md") == "Notes Two"


def test_write_rejects_non_md_and_reserved_index(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        with pytest.raises(KnowledgePathError):
            await store.write(path="rules/testing.txt", content="x")
        with pytest.raises(KnowledgePathError):
            await store.write(path="index.md", content="x")
        with pytest.raises(KnowledgePathError):
            await store.write(path="../outside.md", content="x")

    anyio.run(go)


# ── write / open / index sync ────────────────────────────────────────────────


def test_write_then_open_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        rel = await store.write(
            path="rules/testing.md", content="never mock the DB", index_line="DB test rule"
        )
        assert rel == "rules/testing.md"
        body = await store.open("rules/testing.md")
        assert body is not None and "never mock the DB" in body

    anyio.run(go)
    # File physically lives under knowledge/.
    assert (knowledge_root(tmp_path) / "rules" / "testing.md").exists()


def test_write_syncs_index_with_hook(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="rules/testing.md", content="body", index_line="the DB rule")

    anyio.run(go)
    idx = knowledge_index_path(tmp_path).read_text()
    assert "# Knowledge Index" in idx
    assert "[Testing](rules/testing.md)" in idx
    assert "the DB rule" in idx


def test_rewrite_preserves_existing_hook_when_index_line_omitted(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="a.md", content="v1", index_line="original hook")
        await store.write(path="a.md", content="v2")  # no index_line
        body = await store.open("a.md")
        assert body is not None and "v2" in body

    anyio.run(go)
    idx = knowledge_index_path(tmp_path).read_text()
    assert "original hook" in idx
    # Exactly one entry for a.md (no duplicate appended).
    assert idx.count("(a.md)") == 1


def test_open_missing_returns_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert anyio.run(lambda: store.open("nope.md")) is None


# ── edit ─────────────────────────────────────────────────────────────────────


def test_edit_replaces_unique_string(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> str | None:
        await store.write(path="a.md", content="hello world", index_line="h")
        await store.edit(path="a.md", old_string="world", new_string="there")
        return await store.open("a.md")

    body = anyio.run(go)
    assert body is not None and "hello there" in body


def test_edit_missing_file_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.edit(path="missing.md", old_string="a", new_string="b")

    with pytest.raises(KnowledgeError):
        anyio.run(go)


def test_edit_ambiguous_string_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="a.md", content="x x x", index_line="h")
        await store.edit(path="a.md", old_string="x", new_string="y")

    with pytest.raises(KnowledgeError):
        anyio.run(go)


def test_edit_missing_string_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="a.md", content="abc", index_line="h")
        await store.edit(path="a.md", old_string="zzz", new_string="y")

    with pytest.raises(KnowledgeError):
        anyio.run(go)


# ── delete / move ──────────────────────────────────────────────────────────


def test_delete_removes_file_and_index_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> bool:
        await store.write(path="a.md", content="x", index_line="h")
        return await store.delete(path="a.md")

    existed = anyio.run(go)
    assert existed is True
    assert not (knowledge_root(tmp_path) / "a.md").exists()
    assert "(a.md)" not in knowledge_index_path(tmp_path).read_text()


def test_delete_missing_returns_false(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert anyio.run(lambda: store.delete(path="ghost.md")) is False


def test_move_carries_body_and_index_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="a.md", content="payload", index_line="keep me")
        await store.move(src="a.md", dst="archive/a.md")
        assert await store.open("a.md") is None
        moved = await store.open("archive/a.md")
        assert moved is not None and "payload" in moved

    anyio.run(go)
    idx = knowledge_index_path(tmp_path).read_text()
    assert "(archive/a.md)" in idx
    assert "keep me" in idx  # hook carried over
    assert "(a.md)" not in idx


def test_move_to_existing_destination_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.write(path="a.md", content="1", index_line="h")
        await store.write(path="b.md", content="2", index_line="h")
        await store.move(src="a.md", dst="b.md")

    with pytest.raises(KnowledgeError):
        anyio.run(go)


# ── list / index_text / tree fallback ────────────────────────────────────────


def test_list_returns_curated_entries(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> list[str]:
        await store.write(path="a.md", content="x", index_line="hook a")
        await store.write(path="dir/b.md", content="y", index_line="hook b")
        return [e.path for e in await store.list_entries()]

    paths = anyio.run(go)
    assert set(paths) == {"a.md", "dir/b.md"}


def test_index_text_falls_back_to_tree_walk(tmp_path: Path) -> None:
    # Files on disk but no index.md → regenerate from the tree.
    root = knowledge_root(tmp_path)
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "testing.md").write_text("body")
    (root / "user.md").write_text("body")
    store = _store(tmp_path)

    text = store.index_text()
    assert "(rules/testing.md)" in text
    assert "(user.md)" in text
    # The reserved index name is never listed as a knowledge file.
    assert "(index.md)" not in text


def test_index_text_empty_when_no_tree(tmp_path: Path) -> None:
    assert _store(tmp_path).index_text() == ""
