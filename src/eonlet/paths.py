"""Path helpers. Everything Eonlet writes lives under ~/.eonlet/.

The home root is overridable via ``EONLET_HOME`` (for tests and dev sandboxes).
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Return the Eonlet home directory.

    Honors ``$EONLET_HOME`` so tests can isolate without touching the real home.
    """
    override = os.environ.get("EONLET_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".eonlet"


def config_path() -> Path:
    return home() / "config.yaml"


def agents_dir() -> Path:
    return home() / "agents"


def eonlets_dir() -> Path:
    return home() / "eonlets"


def global_logs_dir() -> Path:
    return home() / "logs"


def agent_definition_dir(agent_type: str) -> Path:
    return agents_dir() / agent_type


def eonlet_dir(eonlet_id: str) -> Path:
    return eonlets_dir() / eonlet_id


def state_db(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "state.db"


def runtime_sock(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "runtime.sock"


def pid_file(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "pid"


def status_file(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "status"


def heartbeat_file(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "heartbeat"


def meta_file(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "meta.json"


def memory_dir(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "memory"


def tasks_dir(eonlet_id: str) -> Path:
    """Workflow state (todos) — sibling of memory/, not part of it (ADR-0005)."""
    return eonlet_dir(eonlet_id) / "tasks"


def trace_dir(eonlet_id: str) -> Path:
    """LLM context-trace records — observability, deletable at any time (ADR-0010)."""
    return eonlet_dir(eonlet_id) / "trace"


def workspace_dir(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "workspace"


def logs_dir(eonlet_id: str) -> Path:
    return eonlet_dir(eonlet_id) / "logs"


def current_log(eonlet_id: str) -> Path:
    return logs_dir(eonlet_id) / "current.log"


def ensure_home() -> Path:
    """Idempotently create ``~/.eonlet/`` and required subdirectories."""
    root = home()
    for p in (root, agents_dir(), eonlets_dir(), global_logs_dir()):
        p.mkdir(parents=True, exist_ok=True)
    return root
