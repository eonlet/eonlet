"""Filesystem layout for per-eonlet memory (MEMORY_SPEC §2).

All helpers take an explicit ``memory_dir`` rather than an ``eonlet_id`` so
tests can use ``tmp_path`` directly without touching ``EONLET_HOME``.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths as _paths


def memory_root(eonlet_id: str) -> Path:
    """The ``memory/`` directory for one eonlet (under ``EONLET_HOME``)."""
    return _paths.memory_dir(eonlet_id)


def short_term_path(memory_dir: Path) -> Path:
    return memory_dir / "short_term.md"


def long_term_path(memory_dir: Path) -> Path:
    return memory_dir / "long_term.md"


def notes_path(memory_dir: Path) -> Path:
    return memory_dir / "notes.md"


def todos_path(memory_dir: Path) -> Path:
    return memory_dir / "todos.jsonl"


def todos_archive_path(memory_dir: Path) -> Path:
    return memory_dir / "todos.archive.jsonl"


def index_db_path(memory_dir: Path) -> Path:
    return memory_dir / "index.sqlite"


def watermark_path(memory_dir: Path) -> Path:
    return memory_dir / "watermark"
