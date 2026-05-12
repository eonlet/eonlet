"""Atomic write helper and per-file locks (MEMORY_SPEC §2 I-S3/I-S4)."""

from __future__ import annotations

import os
from pathlib import Path

import anyio
import pytest

from eonlet.memory.storage import atomic_write_bytes, atomic_write_text, file_lock


def test_atomic_write_text_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_atomic_write_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text() == "two"


def test_atomic_write_cleans_temp_on_success(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    atomic_write_text(target, "x")
    # No stray .tmp left behind
    assert not (tmp_path / "a.md.tmp").exists()


def test_atomic_write_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "deep" / "nested" / "x.md"
    atomic_write_text(target, "ok")
    assert target.read_text() == "ok"


def test_atomic_write_bytes_preserves_content(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    payload = bytes(range(256))
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_file_lock_serializes_writers(tmp_path: Path) -> None:
    """Two concurrent writers under the same lock must not interleave."""
    target = tmp_path / "race.md"
    target.write_text("")
    observations: list[str] = []

    async def writer(label: str, wait_first: bool) -> None:
        async with file_lock(target):
            if wait_first:
                await anyio.sleep(0.02)
            observations.append(f"{label}-enter")
            # Simulate a slow write under the lock
            await anyio.sleep(0.02)
            observations.append(f"{label}-exit")

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(writer, "A", False)
            tg.start_soon(writer, "B", True)

    anyio.run(main)

    # We expect strict alternation (enter→exit→enter→exit), never interleaved.
    assert observations[0].endswith("-enter")
    assert observations[1].endswith("-exit")
    assert observations[2].endswith("-enter")
    assert observations[3].endswith("-exit")
    # Same label for adjacent enter/exit
    assert observations[0][0] == observations[1][0]
    assert observations[2][0] == observations[3][0]


def test_file_lock_distinct_paths_run_concurrently(tmp_path: Path) -> None:
    """Two distinct paths use distinct locks → can overlap."""
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    overlap = {"count": 0, "in_a": False, "in_b": False, "max": 0}

    async def hold(path: Path, key: str) -> None:
        async with file_lock(path):
            overlap[key] = True  # type: ignore[assignment]
            overlap["count"] = int(overlap["in_a"]) + int(overlap["in_b"])
            overlap["max"] = max(int(overlap["max"]), int(overlap["count"]))
            await anyio.sleep(0.05)
            overlap[key] = False  # type: ignore[assignment]

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(hold, a, "in_a")
            tg.start_soon(hold, b, "in_b")

    anyio.run(main)
    assert overlap["max"] == 2  # both held concurrently at some point


def test_atomic_write_leaves_no_temp_on_failure(tmp_path: Path) -> None:
    """If the rename target cannot be created (e.g. parent is a file, not dir),
    no .tmp should remain visible."""
    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("i am a file")
    target = not_a_dir / "child.md"  # writing under a non-dir parent

    with pytest.raises((NotADirectoryError, FileNotFoundError, OSError)):
        atomic_write_text(target, "x")
    # The temp file path under that broken parent must not exist as a stray.
    # (The error is raised before any file is created, but verify regardless.)
    assert not (tmp_path / "blocker.tmp").exists()


def test_atomic_write_is_durable_after_fsync(tmp_path: Path) -> None:
    """Smoke test: after atomic_write returns, opening the file by name yields
    the new bytes (rules out a rename → read race)."""
    target = tmp_path / "x.md"
    for i in range(50):
        atomic_write_text(target, f"iter-{i}")
        # Re-read via a fresh handle (no caching of fd)
        with open(target) as f:
            assert f.read() == f"iter-{i}"
    # Sanity: temp suffix path doesn't linger
    assert not any(p.suffix == ".tmp" for p in tmp_path.iterdir() if p.is_file())
    # Avoid unused import nag
    _ = os.fspath(target)
