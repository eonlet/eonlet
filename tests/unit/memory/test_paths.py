"""Per-eonlet memory file paths (MEMORY_SPEC §2)."""

from __future__ import annotations

import os
from pathlib import Path

from eonlet.memory.paths import (
    index_db_path,
    knowledge_index_path,
    knowledge_root,
    long_term_path,
    memory_root,
    short_term_path,
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
    assert knowledge_root(tmp_path) == tmp_path / "knowledge"
    assert knowledge_index_path(tmp_path) == tmp_path / "knowledge" / "index.md"
    assert index_db_path(tmp_path) == tmp_path / "index.sqlite"
    assert watermark_path(tmp_path) == tmp_path / "watermark"
