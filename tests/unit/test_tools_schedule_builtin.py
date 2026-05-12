"""Tests for tools/builtin/schedule.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anyio

from eonlet.tools import builtin as _builtin  # noqa: F401
from eonlet.tools.builtin.schedule import ScheduleArgs, ScheduleTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path, *, scheduler: Any = None) -> ToolContext:
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
        scheduler=scheduler,
    )


def _mock_scheduler(triggers: list[dict[str, Any]] | None = None) -> MagicMock:
    sched = MagicMock()
    sched.serializable.return_value = triggers or []
    sched.add_dynamic = AsyncMock()
    sched.add_once_dynamic = AsyncMock()
    sched.remove_dynamic = AsyncMock(return_value=True)
    sched.set_enabled = AsyncMock(return_value=True)
    sched.clear_dynamic = AsyncMock(return_value=2)
    return sched


# ── no scheduler attached ─────────────────────────────────────────────────────


def test_no_scheduler_returns_error(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    result = anyio.run(ScheduleTool().__call__, ScheduleArgs(action="list"), ctx)
    assert result.is_error
    assert "no scheduler" in result.content


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_empty(tmp_path: Path) -> None:
    sched = _mock_scheduler([])
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(ScheduleTool().__call__, ScheduleArgs(action="list"), ctx)
    assert not result.is_error
    assert result.structured_output == {"triggers": []}


def test_list_with_triggers(tmp_path: Path) -> None:
    triggers = [{"id": "dyn-2026-05-22-aa01", "schedule": "0 9 * * *", "timezone": "UTC"}]
    sched = _mock_scheduler(triggers)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(ScheduleTool().__call__, ScheduleArgs(action="list"), ctx)
    assert not result.is_error
    assert "dyn-2026-05-22-aa01" in result.content


# ── add ───────────────────────────────────────────────────────────────────────


def test_add_missing_fields(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="add", schedule="0 9 * * *"),
        ctx,
    )
    assert result.is_error
    assert "required" in result.content


def test_add_message_reserved_prefix(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(
            action="add",
            schedule="0 9 * * *",
            timezone="UTC",
            message="<trigger foo>",
        ),
        ctx,
    )
    assert result.is_error
    assert "reserved" in result.content


def test_add_success(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    # add_dynamic returns a mock record
    from unittest.mock import MagicMock

    rec = MagicMock()
    rec.trig.id = "dyn-2026-05-22-aa03"
    rec.trig.schedule = "0 9 * * *"
    rec.trig.timezone = "UTC"
    sched.add_dynamic = AsyncMock(return_value=rec)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="add", schedule="0 9 * * *", timezone="UTC", message="good morning"),
        ctx,
    )
    assert not result.is_error
    assert "dyn-2026-05-22-aa03" in result.content


# ── add_once ─────────────────────────────────────────────────────────────────


def test_add_once_missing_fields(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="add_once"),
        ctx,
    )
    assert result.is_error
    assert "required" in result.content


def test_add_once_both_fire_at_and_in(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    args = ScheduleArgs.model_validate(
        {
            "action": "add_once",
            "timezone": "UTC",
            "message": "hi",
            "fire_at": "2026-06-01T09:00:00+00:00",
            "in": "30m",
        }
    )
    result = anyio.run(ScheduleTool().__call__, args, ctx)
    assert result.is_error
    assert "exactly one" in result.content


def test_add_once_neither_fire_at_nor_in(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="add_once", timezone="UTC", message="hi"),
        ctx,
    )
    assert result.is_error
    assert "exactly one" in result.content


def test_add_once_with_fire_at(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    rec = MagicMock()
    rec.trig.id = "dyn-2026-05-22-oo01"
    rec.trig.fire_at = "2026-06-01T09:00:00+00:00"
    sched.add_once_dynamic = AsyncMock(return_value=rec)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(
            action="add_once",
            timezone="UTC",
            message="fire once",
            fire_at="2026-06-01T09:00:00+00:00",
        ),
        ctx,
    )
    assert not result.is_error
    assert "dyn-2026-05-22-oo01" in result.content


def test_add_once_with_in_duration(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    rec = MagicMock()
    rec.trig.id = "dyn-2026-05-22-oo02"
    rec.trig.fire_at = "2026-06-01T09:30:00+00:00"
    sched.add_once_dynamic = AsyncMock(return_value=rec)
    ctx = _ctx(tmp_path, scheduler=sched)
    args = ScheduleArgs.model_validate(
        {"action": "add_once", "timezone": "UTC", "message": "later", "in": "30m"}
    )
    result = anyio.run(ScheduleTool().__call__, args, ctx)
    assert not result.is_error
    assert "dyn-2026-05-22-oo02" in result.content


def test_add_once_bad_duration(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    args = ScheduleArgs.model_validate(
        {"action": "add_once", "timezone": "UTC", "message": "later", "in": "bad_duration"}
    )
    result = anyio.run(ScheduleTool().__call__, args, ctx)
    assert result.is_error


def test_add_once_reserved_message_prefix(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(
            action="add_once",
            timezone="UTC",
            message="<trigger foo>",
            fire_at="2026-06-01T09:00:00+00:00",
        ),
        ctx,
    )
    assert result.is_error
    assert "reserved" in result.content


# ── remove ────────────────────────────────────────────────────────────────────


def test_remove_missing_trigger_id(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(ScheduleTool().__call__, ScheduleArgs(action="remove"), ctx)
    assert result.is_error
    assert "required" in result.content


def test_remove_not_found(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.remove_dynamic = AsyncMock(return_value=False)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="remove", trigger_id="dyn-missing"),
        ctx,
    )
    assert result.is_error
    assert "no such" in result.content


def test_remove_success(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.remove_dynamic = AsyncMock(return_value=True)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="remove", trigger_id="dyn-2026-05-22-rr01"),
        ctx,
    )
    assert not result.is_error
    assert "removed" in result.content


# ── set_enabled ───────────────────────────────────────────────────────────────


def test_set_enabled_missing_fields(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(ScheduleTool().__call__, ScheduleArgs(action="set_enabled"), ctx)
    assert result.is_error
    assert "required" in result.content


def test_set_enabled_not_found(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.set_enabled = AsyncMock(return_value=False)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="set_enabled", trigger_id="dyn-ghost", enabled=True),
        ctx,
    )
    assert result.is_error
    assert "no such" in result.content


def test_set_enabled_true(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.set_enabled = AsyncMock(return_value=True)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="set_enabled", trigger_id="dyn-2026-05-22-se01", enabled=True),
        ctx,
    )
    assert not result.is_error
    assert "enabled" in result.content


def test_set_enabled_false(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.set_enabled = AsyncMock(return_value=True)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="set_enabled", trigger_id="dyn-2026-05-22-se02", enabled=False),
        ctx,
    )
    assert not result.is_error
    assert "disabled" in result.content


# ── clear ────────────────────────────────────────────────────────────────────


def test_clear_without_confirm(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="clear", confirm=False),
        ctx,
    )
    assert result.is_error
    assert "confirm=true" in result.content


def test_clear_with_confirm(tmp_path: Path) -> None:
    sched = _mock_scheduler()
    sched.clear_dynamic = AsyncMock(return_value=3)
    ctx = _ctx(tmp_path, scheduler=sched)
    result = anyio.run(
        ScheduleTool().__call__,
        ScheduleArgs(action="clear", confirm=True),
        ctx,
    )
    assert not result.is_error
    assert "3" in result.content
