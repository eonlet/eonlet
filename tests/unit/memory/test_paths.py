"""Per-eonlet memory file paths (MEMORY_SPEC §2)."""

from __future__ import annotations

import os
from pathlib import Path

from eonlet.memory.paths import (
    index_db_path,
    long_term_path,
    memory_root,
    notes_path,
    short_term_path,
    todos_archive_path,
    todos_path,
    watermark_path,
)


def test_memory_root_honors_eonlet_home(tmp_path: Path) -> None:
    old = os.environ.get("EONLET_HOME")
    os.environ["EONLET_HOME"] = str(tmp_path)
    try:
        root = memory_root("e.1234")
        assert root == tmp_path / "eonlets" / "e.1234" / "memory"
    finally:
        if old is None:
            os.environ.pop("EONLET_HOME", None)
        else:
            os.environ["EONLET_HOME"] = old


def test_relative_paths_resolve_under_dir(tmp_path: Path) -> None:
    assert short_term_path(tmp_path) == tmp_path / "short_term.md"
    assert long_term_path(tmp_path) == tmp_path / "long_term.md"
    assert notes_path(tmp_path) == tmp_path / "notes.md"
    assert todos_path(tmp_path) == tmp_path / "todos.jsonl"
    assert todos_archive_path(tmp_path) == tmp_path / "todos.archive.jsonl"
    assert index_db_path(tmp_path) == tmp_path / "index.sqlite"
    assert watermark_path(tmp_path) == tmp_path / "watermark"
