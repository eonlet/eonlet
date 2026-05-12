"""Shared CLI helpers: id resolution, status detection, console output."""

from __future__ import annotations

import sys
from typing import NoReturn

from rich.console import Console

from .. import paths
from ..errors import EonletNotFoundError
from ..worker.lifecycle import process_alive, read_pid, read_status

console = Console()
err_console = Console(stderr=True, style="red")


def fail(message: str, code: int = 1) -> NoReturn:
    err_console.print(message)
    sys.exit(code)


def resolve_eonlet_id(id_or_name: str) -> str:
    """Accept ``type.name`` (preferred) or a bare name if unambiguous."""
    if "." in id_or_name:
        if not paths.eonlet_dir(id_or_name).exists():
            raise EonletNotFoundError(f"no eonlet directory: {id_or_name}")
        return id_or_name
    # Search by trailing name.
    root = paths.eonlets_dir()
    if not root.exists():
        raise EonletNotFoundError("no eonlets created yet")
    matches = [p.name for p in root.iterdir() if p.is_dir() and p.name.endswith("." + id_or_name)]
    if not matches:
        raise EonletNotFoundError(f"no eonlet named {id_or_name!r}")
    if len(matches) > 1:
        raise EonletNotFoundError(
            f"ambiguous name {id_or_name!r}, matches: {', '.join(matches)}; use <type>.<name>"
        )
    return matches[0]


def effective_status(eonlet_id: str) -> str:
    """Combine recorded status with live PID check (lazy cleanup per SPEC Q2)."""
    declared = read_status(eonlet_id)
    pid = read_pid(eonlet_id)
    if declared in {"running", "paused"} and not process_alive(pid):
        return "dead"
    return declared
