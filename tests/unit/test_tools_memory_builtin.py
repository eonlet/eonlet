"""Tests for tools/builtin/memory.py — show, compact/pause/resume actions."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio

from eonlet.tools import builtin as _builtin  # noqa: F401 — register builtins
from eonlet.tools.builtin.memory import MemoryArgs, MemoryTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path, *, extra: dict[str, Any] | None = None) -> ToolContext:
    ws = tmp_path / "ws"
    mem = tmp_path / "mem"
    ws.mkdir(exist_ok=True)
    mem.mkdir(exist_ok=True)
    return ToolContext(
        eonlet_id="t.x",
        workspace=ws,
        memory_dir=mem,
        skills={},
        env={},
        extra=extra or {},
    )


# ── show ─────────────────────────────────────────────────────────────────────


def test_show_all_empty(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="show"), ctx)
    assert not result.is_error
    assert "short_term" in result.content
    assert "long_term" in result.content
    assert "notes" in result.content
    assert "todos" in result.content


def test_show_stm_only_with_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.memory_dir / "short_term.md").write_text("## working notes\nstuff\n", encoding="utf-8")
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="show", store="stm"), ctx)
    assert not result.is_error
    assert "short_term" in result.content
    assert "stuff" in result.content


def test_show_ltm_only_with_file(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.memory_dir / "long_term.md").write_text("<!-- ltm -->\n- bullet\n", encoding="utf-8")
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="show", store="ltm"), ctx)
    assert not result.is_error
    assert "long_term" in result.content
    assert "bullet" in result.content


def test_show_notes_with_entries(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    note_content = "---\nid: n1\ntitle: My Note\ntags: []\ncreated_at: 2026-05-22T00:00:00+00:00\n---\n\nbody text\n\n"
    (ctx.memory_dir / "notes.md").write_text(note_content, encoding="utf-8")
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="show", store="notes"), ctx)
    assert not result.is_error
    assert "notes" in result.content


def test_show_todos_with_entries(tmp_path: Path) -> None:
    import json

    ctx = _ctx(tmp_path)
    lines = [
        json.dumps(
            {
                "id": "t1",
                "content": "task one",
                "status": "pending",
                "created_at": "2026-05-22T00:00:00+00:00",
            }
        ),
    ]
    (ctx.memory_dir / "todos.jsonl").write_text("\n".join(lines), encoding="utf-8")
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="show", store="todos"), ctx)
    assert not result.is_error
    assert "todos" in result.content


# ── compact — no runtime ──────────────────────────────────────────────────────


def test_compact_no_runtime(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="compact"), ctx)
    assert result.is_error
    assert "no live runtime" in result.content


def test_compact_ltm_no_runtime(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="compact_ltm"), ctx)
    assert result.is_error
    assert "no live runtime" in result.content


def test_compact_runtime_disabled(tmp_path: Path) -> None:
    """If memory is disabled in agent config, compact should error."""
    from eonlet.memory.config import MemoryConfig

    mock_runtime = MagicMock()
    mock_runtime.definition.config.memory = MemoryConfig.model_validate({"enabled": False})
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="compact"), ctx)
    assert result.is_error
    assert "disabled" in result.content


def test_compact_ltm_runtime_disabled(tmp_path: Path) -> None:
    from eonlet.memory.config import MemoryConfig

    mock_runtime = MagicMock()
    mock_runtime.definition.config.memory = MemoryConfig.model_validate({"enabled": False})
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="compact_ltm"), ctx)
    assert result.is_error
    assert "disabled" in result.content


# ── pause / resume ────────────────────────────────────────────────────────────


def test_pause_no_runtime(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="pause"), ctx)
    assert result.is_error
    assert "no live runtime" in result.content


def test_resume_no_runtime(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="resume"), ctx)
    assert result.is_error
    assert "no live runtime" in result.content


def test_pause_with_runtime(tmp_path: Path) -> None:
    mock_runtime = MagicMock()
    mock_runtime.auto_compact_enabled = True
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="pause"), ctx)
    assert not result.is_error
    assert "paused" in result.content
    assert mock_runtime.auto_compact_enabled is False


def test_resume_with_runtime(tmp_path: Path) -> None:
    mock_runtime = MagicMock()
    mock_runtime.auto_compact_enabled = False
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    result = anyio.run(MemoryTool().__call__, MemoryArgs(action="resume"), ctx)
    assert not result.is_error
    assert "resumed" in result.content
    assert mock_runtime.auto_compact_enabled is True


def test_pause_emits_event(tmp_path: Path) -> None:
    events_recorded: list[Any] = []

    async def record(ev: Any) -> None:
        events_recorded.append(ev)

    mock_runtime = MagicMock()
    mock_runtime.auto_compact_enabled = True
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    ctx.record_event = record

    anyio.run(MemoryTool().__call__, MemoryArgs(action="pause"), ctx)
    assert len(events_recorded) == 1
    assert "paused" in str(events_recorded[0].kind)


def test_resume_emits_event(tmp_path: Path) -> None:
    events_recorded: list[Any] = []

    async def record(ev: Any) -> None:
        events_recorded.append(ev)

    mock_runtime = MagicMock()
    mock_runtime.auto_compact_enabled = False
    ctx = _ctx(tmp_path, extra={"runtime": mock_runtime})
    ctx.record_event = record

    anyio.run(MemoryTool().__call__, MemoryArgs(action="resume"), ctx)
    assert len(events_recorded) == 1
    assert "resumed" in str(events_recorded[0].kind)
