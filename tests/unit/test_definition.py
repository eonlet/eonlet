"""Definition loader — agent.yaml + system.md parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from eonlet.errors import ConfigError
from eonlet.runtime.definition import load_definition


def _write_min_definition(root: Path, name: str = "assistant") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"""
apiVersion: eonlet/v1
kind: Agent
metadata:
  name: {name}
  description: test
  version: 0.0.1
runtime:
  model: claude-sonnet-4-6
tools:
  builtin: [file_read]
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# Identity\nYou are a test.\n", encoding="utf-8")
    return d


def test_load_minimal_definition(tmp_path: Path) -> None:
    p = _write_min_definition(tmp_path)
    defn = load_definition(p)
    assert defn.type == "assistant"
    assert defn.config.runtime.model == "claude-sonnet-4-6"
    assert defn.config.tools.builtin == ["file_read"]
    assert "Identity" in defn.system_prompt


def test_directory_name_must_match_metadata_name(tmp_path: Path) -> None:
    p = _write_min_definition(tmp_path, name="x-digest")
    # Rename folder to mismatch.
    bad = p.parent / "other-name"
    p.rename(bad)
    with pytest.raises(ConfigError, match="must match directory name"):
        load_definition(bad)


def test_skills_discovered_with_description(tmp_path: Path) -> None:
    p = _write_min_definition(tmp_path)
    (p / "skills").mkdir()
    (p / "skills" / "demo.md").write_text(
        "# Demo\n\nDo a thing carefully.\n\n## Steps\n", encoding="utf-8"
    )
    defn = load_definition(p)
    assert "demo" in defn.skills
    assert defn.skills["demo"].description.startswith("Do a thing")
