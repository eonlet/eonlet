"""Load an agent definition directory: agent.yaml + system.md + tools/ + skills/."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AgentConfig, load_agent_config
from ..errors import ConfigError, DefinitionNotFoundError


@dataclass(slots=True)
class Skill:
    name: str  # filename without .md
    description: str  # first non-blank line, used in system prompt listing
    body: str  # full markdown body


@dataclass(slots=True)
class Definition:
    """An immutable view of an agent definition on disk."""

    type: str  # directory name == metadata.name
    path: Path  # absolute definition directory
    config: AgentConfig
    system_prompt: str
    skills: dict[str, Skill] = field(default_factory=dict)
    custom_tool_paths: list[Path] = field(default_factory=list)


def load_definition(path: Path | str) -> Definition:
    """Load a definition from its directory."""
    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_dir():
        raise DefinitionNotFoundError(f"Definition directory not found: {p}")

    cfg = load_agent_config(p)

    system_md = p / "system.md"
    if not system_md.exists():
        raise ConfigError(f"system.md missing in {p}")
    system_prompt = system_md.read_text(encoding="utf-8")

    skills: dict[str, Skill] = {}
    skills_dir = p / "skills"
    if skills_dir.is_dir():
        for f in sorted(skills_dir.glob("*.md")):
            body = f.read_text(encoding="utf-8")
            desc = _first_description_line(body)
            skills[f.stem] = Skill(name=f.stem, description=desc, body=body)

    custom_paths: list[Path] = []
    for rel in cfg.tools.custom:
        tp = (p / rel).resolve()
        if not tp.exists():
            raise ConfigError(f"custom tool not found: {tp} (declared in agent.yaml)")
        custom_paths.append(tp)

    return Definition(
        type=cfg.metadata.name,
        path=p,
        config=cfg,
        system_prompt=system_prompt,
        skills=skills,
        custom_tool_paths=custom_paths,
    )


def _first_description_line(markdown: str) -> str:
    """Heuristic: first non-blank, non-heading line is the description."""
    for line in markdown.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s
    return ""


def import_custom_tool_module(file_path: Path) -> None:
    """Import a tools/*.py file so its ``@tool``-decorated classes register.

    Module name is namespaced under ``eonlet._custom_tools.<stem>`` to avoid
    polluting the global module table and to allow re-imports across eonlets.
    """
    mod_name = f"eonlet._custom_tools.{file_path.parent.parent.name}.{file_path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise ConfigError(f"Cannot import custom tool: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
