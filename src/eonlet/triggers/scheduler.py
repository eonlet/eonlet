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

from ..config import CronTrigger, OnceTrigger
from ..errors import ConfigError
from ..runtime.events import Event, EventKind, now_us
from .dynamic_store import (
    DYNAMIC_ID_PREFIX,
    MAX_DYNAMIC_TRIGGERS,
    DynamicOnceRecord,
    DynamicTriggerRecord,
    DynamicTriggerStore,
    is_dynamic_id,
)

if TYPE_CHECKING:
    from pathlib import Path

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
    trig: CronTrigger | OnceTrigger
    tz: ZoneInfo
    next_fire: datetime
    skip_remaining: int = 0  # backoff counter (cron only)
    once: bool = False  # if True, remove from _items + store after firing


class CronScheduler:
    """Owns the cron firing loop for one eonlet."""

    def __init__(
        self,
        triggers: list[CronTrigger],
        store: EventStore,
        send: MemoryObjectSendStream[TriggerItem],
        eonlet_id: str,
        eonlet_dir: Path | None = None,
    ) -> None:
        self._eonlet_id = eonlet_id
        self._store = store
        self._send = send
        self._items: list[_Scheduled] = []
        # Static-trigger IDs are reserved: dynamic ones must not collide and
        # static ones can never be removed via the dynamic API.
        self._static_ids: set[str] = set()
        # Wakeup signal — set by mutations so `run()` reschedules its sleep.
        self._wake = anyio.Event()
        now = datetime.now(UTC)
        for t in triggers:
            if is_dynamic_id(t.id):
                raise ConfigError(
                    f"static trigger {t.id!r} must not use the {DYNAMIC_ID_PREFIX!r} prefix"
                )
            self._static_ids.add(t.id)
            if not t.enabled:
                continue
            tz = _resolve_tz(t.timezone)
            cron = _make_cron(t, now.astimezone(tz))
            self._items.append(_Scheduled(trig=t, tz=tz, next_fire=cron))
        # Dynamic-trigger store (per ADR-0002). Lazily loaded on first call to
        # ``load_dynamic`` so tests that construct the scheduler without an
        # ``eonlet_dir`` keep working.
        self._dyn_store: DynamicTriggerStore | None = (
            DynamicTriggerStore(eonlet_dir) if eonlet_dir is not None else None
        )

    def trigger_ids(self) -> list[str]:
        return [s.trig.id for s in self._items]

    def configured_triggers(self) -> list[CronTrigger | OnceTrigger]:
        """Snapshot of configured triggers — used by IPC ``triggers.list``."""
        return [s.trig for s in self._items]

    def serializable(self) -> list[dict[str, Any]]:
        """JSON-friendly trigger info for the CLI: next fire + last status."""
        out: list[dict[str, Any]] = []
        for s in self._items:
            state = self._store.get_trigger_state(s.trig.id)
            if s.once:
                schedule_display = f"once@{s.next_fire.isoformat()}"
            else:
                assert isinstance(s.trig, CronTrigger)
                schedule_display = s.trig.schedule
            out.append(
                {
                    "id": s.trig.id,
                    "kind": "dynamic" if is_dynamic_id(s.trig.id) else "static",
                    "mode": "once" if s.once else "cron",
                    "schedule": schedule_display,
                    "timezone": s.trig.timezone,
                    "message": s.trig.message,
                    "enabled": s.trig.enabled,
                    "next_fire_at": s.next_fire.isoformat(),
                    "last_fired_at": state["last_fired_at"],
                    "last_success_at": state["last_success_at"],
                    "consecutive_failures": state["consecutive_failures"],
                    "skip_remaining": s.skip_remaining,
                }
            )
        # Disabled dynamic triggers don't have a `_Scheduled` (we only schedule
        # enabled ones). Surface them too so `list` shows what's there.
        if self._dyn_store is not None:
            scheduled_ids = {s.trig.id for s in self._items}
            for rec in self._dyn_store.all():
                if rec.trig.id in scheduled_ids:
                    continue
                state = self._store.get_trigger_state(rec.trig.id)
                out.append(
                    {
                        "id": rec.trig.id,
                        "kind": "dynamic",
                        "mode": "cron",
                        "schedule": rec.trig.schedule,
                        "timezone": rec.trig.timezone,
                        "message": rec.trig.message,
                        "enabled": rec.trig.enabled,
                        "next_fire_at": None,
                        "last_fired_at": state["last_fired_at"],
                        "last_success_at": state["last_success_at"],
                        "consecutive_failures": state["consecutive_failures"],
                        "skip_remaining": 0,
                    }
                )
            for orec in self._dyn_store.all_once():
                if orec.trig.id in scheduled_ids:
                    continue
                state = self._store.get_trigger_state(orec.trig.id)
                out.append(
                    {
                        "id": orec.trig.id,
                        "kind": "dynamic",
                        "mode": "once",
                        "schedule": f"once@{orec.trig.fire_at}",
                        "timezone": orec.trig.timezone,
                        "message": orec.trig.message,
                        "enabled": orec.trig.enabled,
                        "next_fire_at": None,
                        "last_fired_at": state["last_fired_at"],
                        "last_success_at": state["last_success_at"],
                        "consecutive_failures": state["consecutive_failures"],
                        "skip_remaining": 0,
                    }
                )
        return out

    def get(self, trigger_id: str) -> CronTrigger | OnceTrigger | None:
        for s in self._items:
            if s.trig.id == trigger_id:
                return s.trig
        return None

    # ── runtime entry points ────────────────────────────────────────────────

    async def catch_up_missed(self) -> None:
        """Handle missed fires on worker startup (TRIGGER_SPEC §4.1).

        For one-shot triggers: if ``fire_at`` is in the past and within
        ``grace_period``, fire once and remove. If past the grace window, emit
        ``trigger_skipped`` and remove (the slot is gone — no future fire).
        """
        now = datetime.now(UTC)
        # Snapshot — we may mutate _items as one-shots resolve.
        for s in list(self._items):
            if s.once:
                fire_at = s.next_fire
                if fire_at > now:
                    continue
                missed_by = now - fire_at
                if missed_by.total_seconds() <= s.trig.grace_period_seconds:
                    await self._fire(s, catchup=True)
                else:
                    self._store.append(
                        Event(
                            kind=EventKind.TRIGGER_SKIPPED,
                            trigger_id=s.trig.id,
                            payload={
                                "reason": "missed_beyond_grace",
                                "scheduled_for": fire_at.isoformat(),
                                "missed_by_seconds": int(missed_by.total_seconds()),
                            },
                        )
                    )
                await self._consume_once(s)
                continue
            # cron path (unchanged)
            assert isinstance(s.trig, CronTrigger)
            state = self._store.get_trigger_state(s.trig.id)
            last = state["last_fired_at"]
            base = now.astimezone(s.tz)
            it = croniter(s.trig.schedule, base)
            prev_fire = it.get_prev(datetime)
            prev_fire_utc = prev_fire.astimezone(UTC)
            prev_us = int(prev_fire_utc.timestamp() * 1_000_000)
            if last is not None and last >= prev_us:
                continue
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
        """Main scheduler loop. Sleeps until the soonest fire, then dispatches.

        Interruptible: dynamic-trigger mutations call ``_kick`` to wake the
        loop so a newly-added trigger can fire sooner than the previously
        scheduled one.
        """
        while True:
            if not self._items:
                # Nothing scheduled right now — wait for a mutation to kick us.
                await self._wake.wait()
                self._wake = anyio.Event()
                continue
            next_item = min(self._items, key=lambda s: s.next_fire)
            wait = (next_item.next_fire - datetime.now(UTC)).total_seconds()
            if wait > 0:
                # Sleep, but bail early if a mutation kicks us.
                with anyio.move_on_after(wait):
                    await self._wake.wait()
                    self._wake = anyio.Event()
                    continue
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
            if next_item.once:
                await self._consume_once(next_item)
            else:
                self._advance(next_item)

    def _advance(self, s: _Scheduled) -> None:
        assert isinstance(s.trig, CronTrigger)
        it = croniter(s.trig.schedule, datetime.now(s.tz))
        s.next_fire = it.get_next(datetime).astimezone(UTC)

    async def _consume_once(self, s: _Scheduled) -> None:
        """Remove a one-shot from in-memory and persistent state after firing."""
        self._items = [x for x in self._items if x.trig.id != s.trig.id]
        if self._dyn_store is not None:
            await self._dyn_store.remove(s.trig.id)

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

    # ── dynamic triggers (ADR-0002) ─────────────────────────────────────────

    def load_dynamic(self) -> int:
        """Read the dynamic-trigger JSON file and install enabled records.

        Loads both recurring (cron) and one-shot triggers. Returns the count.
        """
        if self._dyn_store is None:
            return 0
        cron_records, once_records = self._dyn_store.load()
        now = datetime.now(UTC)
        installed = 0
        for rec in cron_records:
            if not rec.trig.enabled:
                continue
            tz = _resolve_tz(rec.trig.timezone)
            cron = _make_cron(rec.trig, now.astimezone(tz))
            self._items.append(_Scheduled(trig=rec.trig, tz=tz, next_fire=cron))
            installed += 1
        for orec in once_records:
            if not orec.trig.enabled:
                continue
            tz = _resolve_tz(orec.trig.timezone)
            fire_at = _parse_fire_at(orec.trig.fire_at, orec.trig.id)
            self._items.append(_Scheduled(trig=orec.trig, tz=tz, next_fire=fire_at, once=True))
            installed += 1
        return installed

    def _check_can_add(self, trig_id: str) -> None:
        if self._dyn_store is None:
            raise ConfigError("dynamic triggers not enabled (no eonlet_dir)")
        if not is_dynamic_id(trig_id):
            raise ConfigError(f"dynamic trigger id must start with {DYNAMIC_ID_PREFIX!r}")
        if trig_id in self._static_ids:
            raise ConfigError(f"id collides with a static trigger: {trig_id!r}")
        if any(s.trig.id == trig_id for s in self._items):
            raise ConfigError(f"duplicate trigger id: {trig_id!r}")
        total = len(self._dyn_store.all()) + len(self._dyn_store.all_once())
        if total >= MAX_DYNAMIC_TRIGGERS:
            raise ConfigError(
                f"dynamic trigger cap reached ({MAX_DYNAMIC_TRIGGERS}); "
                "remove some with `schedule(action='remove', …)`"
            )

    async def add_dynamic(
        self, trig: CronTrigger, *, created_by: str = "agent"
    ) -> DynamicTriggerRecord:
        """Validate, persist, and install a recurring dynamic trigger."""
        self._check_can_add(trig.id)
        assert self._dyn_store is not None
        # Validate schedule + tz by constructing the next-fire time.
        tz = _resolve_tz(trig.timezone)
        now = datetime.now(UTC)
        next_fire = _make_cron(trig, now.astimezone(tz))
        record = DynamicTriggerRecord(
            trig=trig,
            created_at=now.astimezone(tz).isoformat(),
            created_by=created_by,
        )
        await self._dyn_store.add(record)
        if trig.enabled:
            self._items.append(_Scheduled(trig=trig, tz=tz, next_fire=next_fire))
            self._kick()
        return record

    async def add_once_dynamic(
        self, trig: OnceTrigger, *, created_by: str = "agent"
    ) -> DynamicOnceRecord:
        """Validate, persist, and install a one-shot dynamic trigger.

        The fire time may be in the past (caller may have intended it that
        way); we accept it and rely on ``catch_up_missed`` semantics to decide
        whether to fire (within grace) or skip (past grace).
        """
        self._check_can_add(trig.id)
        assert self._dyn_store is not None
        tz = _resolve_tz(trig.timezone)
        fire_at = _parse_fire_at(trig.fire_at, trig.id)
        now = datetime.now(UTC)
        record = DynamicOnceRecord(
            trig=trig,
            created_at=now.astimezone(tz).isoformat(),
            created_by=created_by,
        )
        await self._dyn_store.add_once(record)
        if trig.enabled:
            self._items.append(_Scheduled(trig=trig, tz=tz, next_fire=fire_at, once=True))
            self._kick()
        return record

    async def remove_dynamic(self, trigger_id: str) -> bool:
        """Drop a dynamic trigger. Refuses static IDs with a clear error."""
        if self._dyn_store is None:
            raise ConfigError("dynamic triggers not enabled (no eonlet_dir)")
        if trigger_id in self._static_ids:
            raise ConfigError(
                f"refusing to remove static trigger {trigger_id!r} "
                "(declared in agent.yaml; disable instead)"
            )
        if not is_dynamic_id(trigger_id):
            raise ConfigError(f"not a dynamic trigger id: {trigger_id!r}")
        removed = await self._dyn_store.remove(trigger_id)
        self._items = [s for s in self._items if s.trig.id != trigger_id]
        if removed:
            self._kick()
        return removed

    async def set_enabled(self, trigger_id: str, enabled: bool) -> bool:
        """Toggle a trigger. Dynamic toggles persist; static toggles are in-process only."""
        if trigger_id in self._static_ids:
            # Static: mutate the live _Scheduled; do not touch agent.yaml.
            for s in self._items:
                if s.trig.id == trigger_id:
                    s.trig.enabled = enabled
                    self._kick()
                    return True
            # Not in _items but is a static id → previously disabled in yaml.
            # Re-enabling at runtime would require reloading from definition;
            # out of scope for ADR-0002.
            return False
        if self._dyn_store is None:
            return False
        ok = await self._dyn_store.set_enabled(trigger_id, enabled)
        if not ok:
            return False
        # Sync the in-memory schedule.
        existing = next((s for s in self._items if s.trig.id == trigger_id), None)
        if enabled and existing is None:
            rec = self._dyn_store.get(trigger_id)
            if rec is not None:
                tz = _resolve_tz(rec.trig.timezone)
                now = datetime.now(UTC)
                self._items.append(
                    _Scheduled(
                        trig=rec.trig, tz=tz, next_fire=_make_cron(rec.trig, now.astimezone(tz))
                    )
                )
            else:
                orec = self._dyn_store.get_once(trigger_id)
                if orec is not None:
                    tz = _resolve_tz(orec.trig.timezone)
                    self._items.append(
                        _Scheduled(
                            trig=orec.trig,
                            tz=tz,
                            next_fire=_parse_fire_at(orec.trig.fire_at, orec.trig.id),
                            once=True,
                        )
                    )
        elif not enabled and existing is not None:
            self._items = [s for s in self._items if s.trig.id != trigger_id]
        self._kick()
        return True

    async def clear_dynamic(self) -> int:
        """Drop all dynamic triggers. Returns the number cleared."""
        if self._dyn_store is None:
            return 0
        n = await self._dyn_store.clear()
        self._items = [s for s in self._items if not is_dynamic_id(s.trig.id)]
        if n > 0:
            self._kick()
        return n

    def _kick(self) -> None:
        """Wake ``run()`` so it picks up scheduling changes."""
        self._wake.set()


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
    trig: CronTrigger | OnceTrigger,
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

    kind = "once" if isinstance(trig, OnceTrigger) else "cron"
    catchup_note = "\n  (catching up after downtime)" if catchup else ""
    return (
        f'<trigger kind="{kind}" id="{trig.id}" fired_at="{fired_local.isoformat()}">\n'
        f"  Previous successful run: {last_str}\n"
        f"  Time since last run: {since}{catchup_note}\n\n"
        f"  {body.strip()}\n"
        f"</trigger>"
    )


def _parse_fire_at(value: str, trig_id: str) -> datetime:
    """Parse an ISO-8601 ``fire_at`` string into a UTC-aware datetime."""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ConfigError(f"trigger {trig_id!r}: invalid fire_at {value!r}: {e}") from e
    if dt.tzinfo is None:
        raise ConfigError(
            f"trigger {trig_id!r}: fire_at must include a timezone offset (got {value!r})"
        )
    return dt.astimezone(UTC)


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
