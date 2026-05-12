"""Permission gate — SPEC §12 invariant I3 (hardcoded deny cannot be bypassed)."""

from __future__ import annotations

from pydantic import BaseModel

from eonlet.permissions import PermissionGate
from eonlet.tools.protocol import ToolAnnotations


class _BashArgs(BaseModel):
    command: str


class _BashLike:
    name = "bash"
    description = "fake"
    input_schema = _BashArgs
    annotations = ToolAnnotations(destructive=True)


class _FileWriteArgs(BaseModel):
    path: str
    content: str = ""


class _FileWriteLike:
    name = "file_write"
    description = "fake"
    input_schema = _FileWriteArgs
    annotations = ToolAnnotations(destructive=True)


class _NotesArgs(BaseModel):
    file: str = ""


class _NotesReadLike:
    name = "notes_read"
    description = "fake"
    input_schema = _NotesArgs
    annotations = ToolAnnotations(read_only=True)


def test_hardcoded_deny_blocks_rm_rf_in_yolo() -> None:
    gate = PermissionGate("yolo", extra_deny=[])
    d = gate.evaluate(_BashLike(), _BashArgs(command="rm -rf /"))
    assert not d.allowed and d.rule == "hardcoded_deny"


def test_hardcoded_deny_blocks_sudo() -> None:
    gate = PermissionGate("yolo", extra_deny=[])
    d = gate.evaluate(_BashLike(), _BashArgs(command="sudo apt install"))
    assert not d.allowed and d.rule == "hardcoded_deny"


def test_hardcoded_deny_blocks_ssh_writes() -> None:
    import os

    gate = PermissionGate("yolo", extra_deny=[])
    p = os.path.expanduser("~/.ssh/authorized_keys")
    d = gate.evaluate(_FileWriteLike(), _FileWriteArgs(path=p))
    assert not d.allowed and d.rule == "hardcoded_deny"


def test_extra_deny_pattern_blocks() -> None:
    gate = PermissionGate("yolo", extra_deny=["Bash(npm publish*)"])
    d = gate.evaluate(_BashLike(), _BashArgs(command="npm publish --tag latest"))
    assert not d.allowed and d.rule == "extra_deny"


def test_yolo_allows_safe_command() -> None:
    gate = PermissionGate("yolo", extra_deny=[])
    d = gate.evaluate(_BashLike(), _BashArgs(command="echo hi"))
    assert d.allowed and d.rule == "yolo"


def test_ask_mode_blocks_destructive_without_session() -> None:
    gate = PermissionGate("ask", extra_deny=[], session_attached=False)
    d = gate.evaluate(_BashLike(), _BashArgs(command="echo hi"))
    assert not d.allowed and d.rule == "ask_no_session"


def test_ask_mode_allows_read_only_without_session() -> None:
    gate = PermissionGate("ask", extra_deny=[], session_attached=False)
    d = gate.evaluate(_NotesReadLike(), _NotesArgs(file="notes.md"))
    assert d.allowed and d.rule == "ask_non_destructive"
