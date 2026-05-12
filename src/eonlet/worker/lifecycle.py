"""Filesystem lifecycle files: pid, status, heartbeat, meta.json."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from .. import paths


def write_pid(eonlet_id: str) -> None:
    paths.pid_file(eonlet_id).write_text(str(os.getpid()), encoding="utf-8")


def write_status(eonlet_id: str, status: str) -> None:
    paths.status_file(eonlet_id).write_text(status, encoding="utf-8")


def write_heartbeat(eonlet_id: str) -> None:
    paths.heartbeat_file(eonlet_id).write_text(str(int(time.time())), encoding="utf-8")


def read_status(eonlet_id: str) -> str:
    p = paths.status_file(eonlet_id)
    return p.read_text(encoding="utf-8").strip() if p.exists() else "unknown"


def read_pid(eonlet_id: str) -> int | None:
    p = paths.pid_file(eonlet_id)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def write_meta(eonlet_id: str, *, type_: str, name: str, definition: Path, version: str) -> None:
    payload = {
        "uuid": eonlet_id,
        "name": name,
        "type": type_,
        "definition_version": version,
        "definition_path": str(definition),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spec_version": "eonlet/v1",
    }
    paths.meta_file(eonlet_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def read_meta(eonlet_id: str) -> dict[str, Any] | None:
    p = paths.meta_file(eonlet_id)
    if not p.exists():
        return None
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return data


def cleanup(eonlet_id: str) -> None:
    """Remove transient runtime files; keep state.db, memory/, meta.json."""
    for p in (
        paths.runtime_sock(eonlet_id),
        paths.pid_file(eonlet_id),
        paths.heartbeat_file(eonlet_id),
    ):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()
