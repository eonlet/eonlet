"""Cron scheduler — message templating, missed-fire grace, backoff."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import pytest

from eonlet.config import CronTrigger
from eonlet.runtime.events import EventKind
from eonlet.runtime.store import EventStore
from eonlet.triggers.scheduler import CronScheduler, build_trigger_message


# ── helpers ──────────────────────────────────────────────────────────────────


def _trig(**over) -> CronTrigger:
    defaults = dict(
        id="daily",
        schedule="0 8 * * *",
        timezone="UTC",
        message="do the thing",
        grace_period="1h",
    )
    defaults.update(over)
    return CronTrigger(**defaults)


# ── message templating ──────────────────────────────────────────────────────


def test_build_trigger_message_substitutes_vars() -> None:
    t = _trig(message="run for {{fired_at_date}} on {{eonlet_id}}")
    tz = ZoneInfo("UTC")
    fired = datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)
    body = build_trigger_message(
        t,
        tz=tz,
        fired_at=fired,
        last_success_at=None,
        eonlet_id="x-digest.morning",
        catchup=False,
        override_message=None,
    )
    assert "run for 2026-05-12 on x-digest.morning" in body
    assert '<trigger kind="cron" id="daily"' in body
    assert "Previous successful run: never" in body


def test_catchup_note_present() -> None:
    body = build_trigger_message(
        _trig(),
        tz=ZoneInfo("UTC"),
        fired_at=datetime.now(timezone.utc),
        last_success_at=None,
        eonlet_id="x.y",
        catchup=True,
        override_message=None,
    )
    assert "catching up after downtime" in body


def test_override_message_wins() -> None:
    body = build_trigger_message(
        _trig(),
        tz=ZoneInfo("UTC"),
        fired_at=datetime.now(timezone.utc),
        last_success_at=None,
        eonlet_id="x.y",
        catchup=False,
        override_message="hand-fired body",
    )
    assert "hand-fired body" in body
    assert "do the thing" not in body


# ── catch_up_missed ──────────────────────────────────────────────────────────


def test_missed_fire_within_grace_enqueues_catchup(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    send, recv = anyio.create_memory_object_stream(16)
    sched = CronScheduler([_trig(grace_period="24h")], store, send, "test.id")

    async def go() -> bool:
        await sched.catch_up_missed()
        try:
            item = recv.receive_nowait()
        except anyio.WouldBlock:
            return False
        return item.kind == "cron" and item.trigger_id == "daily"

    assert anyio.run(go) is True


def test_missed_fire_beyond_grace_skipped(tmp_path: Path) -> None:
    """If grace is tiny we should skip silently and emit trigger_skipped."""
    store = EventStore(tmp_path / "state.db")
    send, recv = anyio.create_memory_object_stream(16)
    # 0s grace = anything missed is skipped.
    sched = CronScheduler([_trig(grace_period="0s")], store, send, "test.id")

    async def go() -> bool:
        await sched.catch_up_missed()
        try:
            recv.receive_nowait()
            return False
        except anyio.WouldBlock:
            return True

    assert anyio.run(go) is True
    # And a trigger_skipped event was emitted.
    kinds = [e.kind for e in store.read()]
    assert EventKind.TRIGGER_SKIPPED in kinds


def test_already_fired_no_catchup(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    # Pretend we already fired during the most-recent slot.
    huge = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
    store.update_trigger_state("daily", last_fired_at=huge)
    send, recv = anyio.create_memory_object_stream(16)
    sched = CronScheduler([_trig()], store, send, "x")

    async def go() -> bool:
        await sched.catch_up_missed()
        try:
            recv.receive_nowait()
            return False
        except anyio.WouldBlock:
            return True

    assert anyio.run(go) is True


# ── failure backoff ──────────────────────────────────────────────────────────


def test_record_outcome_triggers_backoff_after_3_failures(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    send, _ = anyio.create_memory_object_stream(16)
    sched = CronScheduler([_trig()], store, send, "x")
    for _ in range(3):
        sched.record_outcome("daily", success=False)
    state = store.get_trigger_state("daily")
    assert state["consecutive_failures"] == 3
    # The Scheduled item should have skip_remaining = failures - 2 = 1
    s = sched._items[0]  # noqa: SLF001
    assert s.skip_remaining == 1


def test_success_resets_failures(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    send, _ = anyio.create_memory_object_stream(16)
    sched = CronScheduler([_trig()], store, send, "x")
    sched.record_outcome("daily", success=False)
    sched.record_outcome("daily", success=False)
    sched.record_outcome("daily", success=True)
    state = store.get_trigger_state("daily")
    assert state["consecutive_failures"] == 0
    assert state["total_successes"] == 1
