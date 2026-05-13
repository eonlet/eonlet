"""Cron trigger scheduler.

Per SPEC §7.3 + TRIGGER_SPEC. Runs inside the worker's TaskGroup. For each
declared cron trigger we maintain a (croniter, next_fire) pair and sleep until
the soonest fire. When a trigger fires we:

1. Record a ``trigger_fired`` event.
2. Update ``trigger_state.last_fired_at``.
3. Build the trigger message (template substitution per TRIGGER_SPEC §2.3).
4. Enqueue a ``TriggerItem`` on the worker's send channel.

Edge cases honored (TRIGGER_SPEC §4):
- **Missed fires**: on startup, replay missed schedules; fire once with
  ``catchup=true`` if within ``grace_period``, otherwise emit
  ``trigger_skipped`` and skip.
- **Repeated failures**: ``consecutive_failures >= 3`` triggers an
  exponential-ish backoff — skip the next ``consecutive_failures - 2``
  fires. Worker reports success/failure back via ``record_trigger_outcome``.
- **Per-trigger run-rate floor**: refuse schedules that fire faster than once
  per minute (TRIGGER_SPEC §9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from anyio.streams.memory import MemoryObjectSendStream
from croniter import croniter

from ..config import CronTrigger
from ..errors import ConfigError
from ..runtime.events import Event, EventKind, now_us

if TYPE_CHECKING:
    from ..runtime.store import EventStore

log = logging.getLogger("eonlet.triggers")


# ── Queue items ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TriggerItem:
    """One unit of work for the main loop.

    Two flavors:
    - ``kind="interactive"`` — a user message from IPC.
    - ``kind="cron"``        — a scheduled/manual cron fire. ``trigger_id`` set.
    """

    kind: str  # "interactive" | "cron"
    content: str
    trigger_id: str | None = None


# ── Scheduler ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Scheduled:
    trig: CronTrigger
    tz: ZoneInfo
    next_fire: datetime
    skip_remaining: int = 0  # backoff counter


class CronScheduler:
    """Owns the cron firing loop for one eonlet."""

    def __init__(
        self,
        triggers: list[CronTrigger],
        store: EventStore,
        send: MemoryObjectSendStream[TriggerItem],
        eonlet_id: str,
    ) -> None:
        self._eonlet_id = eonlet_id
        self._store = store
        self._send = send
        self._items: list[_Scheduled] = []
        now = datetime.now(UTC)
        for t in triggers:
            if not t.enabled:
                continue
            tz = _resolve_tz(t.timezone)
            cron = _make_cron(t, now.astimezone(tz))
            self._items.append(_Scheduled(trig=t, tz=tz, next_fire=cron))

    def trigger_ids(self) -> list[str]:
        return [s.trig.id for s in self._items]

    def configured_triggers(self) -> list[CronTrigger]:
        """Snapshot of configured triggers — used by IPC ``triggers.list``."""
        return [s.trig for s in self._items]

    def serializable(self) -> list[dict[str, Any]]:
        """JSON-friendly trigger info for the CLI: next fire + last status."""
        out: list[dict[str, Any]] = []
        for s in self._items:
            state = self._store.get_trigger_state(s.trig.id)
            out.append(
                {
                    "id": s.trig.id,
                    "schedule": s.trig.schedule,
                    "timezone": s.trig.timezone,
                    "enabled": s.trig.enabled,
                    "next_fire_at": s.next_fire.isoformat(),
                    "last_fired_at": state["last_fired_at"],
                    "last_success_at": state["last_success_at"],
                    "consecutive_failures": state["consecutive_failures"],
                    "skip_remaining": s.skip_remaining,
                }
            )
        return out

    def get(self, trigger_id: str) -> CronTrigger | None:
        for s in self._items:
            if s.trig.id == trigger_id:
                return s.trig
        return None

    # ── runtime entry points ────────────────────────────────────────────────

    async def catch_up_missed(self) -> None:
        """Handle missed fires on worker startup (TRIGGER_SPEC §4.1)."""
        now = datetime.now(UTC)
        for s in self._items:
            state = self._store.get_trigger_state(s.trig.id)
            last = state["last_fired_at"]
            # Compute the most recent scheduled fire that should have happened.
            base = now.astimezone(s.tz)
            it = croniter(s.trig.schedule, base)
            prev_fire = it.get_prev(datetime)
            prev_fire_utc = prev_fire.astimezone(UTC)
            prev_us = int(prev_fire_utc.timestamp() * 1_000_000)
            if last is not None and last >= prev_us:
                # Already fired for this slot.
                continue
            # Missed. Within grace?
            missed_by = now - prev_fire_utc
            if missed_by.total_seconds() <= s.trig.grace_period_seconds:
                await self._fire(s, catchup=True)
            else:
                self._store.append(
                    Event(
                        kind=EventKind.TRIGGER_SKIPPED,
                        trigger_id=s.trig.id,
                        payload={
                            "reason": "missed_beyond_grace",
                            "scheduled_for": prev_fire_utc.isoformat(),
                            "missed_by_seconds": int(missed_by.total_seconds()),
                        },
                    )
                )

    async def run(self) -> None:
        """Main scheduler loop. Sleeps until the soonest fire, then dispatches."""
        if not self._items:
            # Nothing to schedule. Sleep forever (the worker's cancel_scope ends us).
            await anyio.sleep_forever()
            return
        while True:
            next_item = min(self._items, key=lambda s: s.next_fire)
            wait = (next_item.next_fire - datetime.now(UTC)).total_seconds()
            if wait > 0:
                await anyio.sleep(wait)
            if next_item.skip_remaining > 0:
                # Backoff: skip this fire, advance schedule, emit event.
                next_item.skip_remaining -= 1
                self._store.append(
                    Event(
                        kind=EventKind.TRIGGER_SKIPPED,
                        trigger_id=next_item.trig.id,
                        payload={"reason": "backoff_after_failures"},
                    )
                )
                self._advance(next_item)
                continue
            await self._fire(next_item, catchup=False)
            self._advance(next_item)

    def _advance(self, s: _Scheduled) -> None:
        it = croniter(s.trig.schedule, datetime.now(s.tz))
        s.next_fire = it.get_next(datetime).astimezone(UTC)

    async def _fire(self, s: _Scheduled, *, catchup: bool) -> None:
        fired_at = datetime.now(UTC)
        state = self._store.get_trigger_state(s.trig.id)
        # Persist fire-side state up front.
        self._store.update_trigger_state(
            s.trig.id,
            last_fired_at=int(fired_at.timestamp() * 1_000_000),
            total_fires=state["total_fires"] + 1,
        )
        self._store.append(
            Event(
                kind=EventKind.TRIGGER_FIRED,
                trigger_id=s.trig.id,
                payload={
                    "fired_at": fired_at.isoformat(),
                    "catchup": catchup,
                    "consecutive_failures_before": state["consecutive_failures"],
                },
            )
        )
        message = build_trigger_message(
            s.trig,
            tz=s.tz,
            fired_at=fired_at,
            last_success_at=state["last_success_at"],
            eonlet_id=self._eonlet_id,
            catchup=catchup,
            override_message=None,
        )
        try:
            self._send.send_nowait(TriggerItem(kind="cron", content=message, trigger_id=s.trig.id))
        except anyio.WouldBlock:
            # Queue full → drop with an explicit event (TRIGGER_SPEC §4.2).
            self._store.append(
                Event(
                    kind=EventKind.TRIGGER_SKIPPED,
                    trigger_id=s.trig.id,
                    payload={"reason": "queue_full"},
                )
            )

    # ── outcomes (called by the worker after a run completes) ───────────────

    def record_outcome(self, trigger_id: str, *, success: bool) -> None:
        s = next((x for x in self._items if x.trig.id == trigger_id), None)
        if s is None:
            return
        state = self._store.get_trigger_state(trigger_id)
        if success:
            self._store.update_trigger_state(
                trigger_id,
                last_success_at=now_us(),
                consecutive_failures=0,
                total_successes=state["total_successes"] + 1,
            )
            s.skip_remaining = 0
            self._store.append(
                Event(
                    kind=EventKind.TRIGGER_COMPLETED,
                    trigger_id=trigger_id,
                    payload={"success": True},
                )
            )
        else:
            failures = state["consecutive_failures"] + 1
            self._store.update_trigger_state(
                trigger_id,
                last_failure_at=now_us(),
                consecutive_failures=failures,
            )
            self._store.append(
                Event(
                    kind=EventKind.TRIGGER_FAILED,
                    trigger_id=trigger_id,
                    payload={"consecutive_failures": failures},
                )
            )
            # Backoff per TRIGGER_SPEC §4.5: skip the next (failures - 2) fires.
            if failures >= 3:
                s.skip_remaining = failures - 2


# ── helpers ──────────────────────────────────────────────────────────────────


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ConfigError(f"unknown timezone: {name}") from e


def _make_cron(trig: CronTrigger, now_local: datetime) -> datetime:
    """Compute the next fire time in UTC. Refuses ``* * * * *`` (TRIGGER_SPEC §9)."""
    if trig.schedule.strip() in {"* * * * *"}:
        raise ConfigError(
            f"trigger {trig.id!r}: schedule {trig.schedule!r} fires more than once per minute"
        )
    try:
        it = croniter(trig.schedule, now_local)
    except Exception as e:
        raise ConfigError(f"trigger {trig.id!r}: invalid cron {trig.schedule!r}: {e}") from e
    next_dt: datetime = it.get_next(datetime)
    return next_dt.astimezone(UTC)


def build_trigger_message(
    trig: CronTrigger,
    *,
    tz: ZoneInfo,
    fired_at: datetime,
    last_success_at: int | None,
    eonlet_id: str,
    catchup: bool,
    override_message: str | None,
) -> str:
    """Render the ``<trigger>`` envelope per TRIGGER_SPEC §2.3."""
    fired_local = fired_at.astimezone(tz)
    if last_success_at:
        last_dt = datetime.fromtimestamp(last_success_at / 1_000_000, tz=tz)
        last_str = last_dt.isoformat()
        since = _humanize(fired_at - datetime.fromtimestamp(last_success_at / 1_000_000, tz=UTC))
    else:
        last_str = "never"
        since = "never"

    body_template = override_message if override_message is not None else trig.message
    subs = {
        "{{fired_at}}": fired_local.isoformat(),
        "{{fired_at_date}}": fired_local.strftime("%Y-%m-%d"),
        "{{fired_at_time}}": fired_local.strftime("%H:%M"),
        "{{last_success_at}}": last_str,
        "{{since_last_run}}": since,
        "{{trigger_id}}": trig.id,
        "{{eonlet_id}}": eonlet_id,
    }
    body = body_template
    for k, v in subs.items():
        body = body.replace(k, v)

    catchup_note = "\n  (catching up after downtime)" if catchup else ""
    return (
        f'<trigger kind="cron" id="{trig.id}" fired_at="{fired_local.isoformat()}">\n'
        f"  Previous successful run: {last_str}\n"
        f"  Time since last run: {since}{catchup_note}\n\n"
        f"  {body.strip()}\n"
        f"</trigger>"
    )


def _humanize(delta: timedelta) -> str:
    s = int(abs(delta.total_seconds()))
    if s < 60:
        return f"{s}s"
    m, s2 = divmod(s, 60)
    if m < 60:
        return f"{m}m {s2}s"
    h, m2 = divmod(m, 60)
    if h < 24:
        return f"{h}h {m2}m"
    d, h2 = divmod(h, 24)
    return f"{d}d {h2}h"
