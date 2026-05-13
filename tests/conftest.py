"""Shared fixtures.

Critically, every test gets an isolated ``EONLET_HOME`` so we never touch the
real ~/.eonlet/. This is enforced via an autouse fixture.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Force EONLET_HOME to a per-test temp dir."""
    home = tmp_path / "eonlet-home"
    home.mkdir()
    monkeypatch.setenv("EONLET_HOME", str(home))
    return home


@pytest.fixture
def fresh_registry():
    """Reset the global tool registry, then re-import builtins."""
    from eonlet.tools import registry

    registry.reset_registry()
    # Re-register builtins by re-importing the package (idempotent).
    import importlib

    from eonlet.tools import builtin

    for sub in ["bash", "files", "notes", "sleep_tool", "skill_tool"]:
        importlib.reload(importlib.import_module(f"eonlet.tools.builtin.{sub}"))
    yield registry.get_registry()
