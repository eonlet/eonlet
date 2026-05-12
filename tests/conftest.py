"""Shared fixtures.

Critically, every test gets an isolated ``EONLET_HOME`` so we never touch the
real ~/.eonlet/. This is enforced via an autouse fixture.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _short_tmpdir_root() -> str:
    """Pick a short-path tmp root so AF_UNIX bind() never blows the 104/108-byte
    cap on macOS/Linux. On macOS ``$TMPDIR`` defaults to ``/var/folders/...``
    (~50 chars) which already eats most of the budget; pytest's ``tmp_path``
    adds ~50 more for the test-name suffix. ``/tmp`` exists on both platforms
    and is the shortest stable option (on macOS it's a symlink to
    ``/private/tmp`` — ~12 chars resolved, still fine).
    """
    return "/tmp"


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Force EONLET_HOME to a short-path per-test temp dir.

    Some tests bind Unix sockets under EONLET_HOME (``eonlets/<id>/runtime.sock``)
    and AF_UNIX paths are capped at 108 bytes on Linux / 104 on macOS. Pytest's
    default ``tmp_path`` plus our nested directory layout overruns that budget
    on CI runners with long test names. We use ``tempfile.mkdtemp(dir=/tmp)``
    to keep the absolute path to ~15 bytes before we add our own structure.
    """
    home = Path(tempfile.mkdtemp(prefix="el-", dir=_short_tmpdir_root()))
    monkeypatch.setenv("EONLET_HOME", str(home))
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)


@pytest.fixture
def short_tmp_path() -> Iterator[Path]:
    """Short-path counterpart of pytest's ``tmp_path`` for tests that bind
    Unix sockets *outside* EONLET_HOME (e.g. an ad-hoc IPC fixture). Same
    rationale as ``isolated_home``: AF_UNIX path length budget.
    """
    d = Path(tempfile.mkdtemp(prefix="el-", dir=_short_tmpdir_root()))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fresh_registry():
    """Reset the global tool registry, then re-import builtins."""
    from eonlet.tools import registry

    registry.reset_registry()
    # Re-register builtins by re-importing the package (idempotent).
    import importlib

    for sub in ["bash", "files", "notes", "sleep_tool", "skill_tool"]:
        importlib.reload(importlib.import_module(f"eonlet.tools.builtin.{sub}"))
    yield registry.get_registry()
