"""Persistence layer for dynamic (runtime-created) cron triggers.

Per ADR-0002. Source of truth: ``<eonlet_dir>/dynamic_triggers.json``.

We intentionally do not append events to the SQLite store for dynamic-trigger
mutations — keeps the event-kind surface small. The JSON file is the only
record of "what's currently scheduled dynamically"; restart replays it.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import anyio

from ..config import CronTrigger, OnceTrigger
from ..errors import ConfigError

DYNAMIC_ID_PREFIX = "dyn-"
MAX_DYNAMIC_TRIGGERS = 64
STORE_FILENAME = "dynamic_triggers.json"
STORE_VERSION = 1


@dataclass(slots=True)
class DynamicTriggerRecord:
    """One persisted recurring dynamic trigger."""

    trig: CronTrigger
    created_at: str  # ISO-8601 with tz
    created_by: str  # "agent" | "cli" | "user"

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.trig.id,
            "schedule": self.trig.schedule,
            "timezone": self.trig.timezone,
            "message": self.trig.message,
            "grace_period": self.trig.grace_period,
            "enabled": self.trig.enabled,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_json(cls, d: dict[str, object]) -> DynamicTriggerRecord:
        tid = str(d["id"])
        if not tid.startswith(DYNAMIC_ID_PREFIX):
            raise ConfigError(f"dynamic trigger id missing {DYNAMIC_ID_PREFIX!r} prefix: {tid!r}")
        trig = CronTrigger(
            id=tid,
            schedule=str(d["schedule"]),
            timezone=str(d["timezone"]),
            message=str(d["message"]),
            grace_period=str(d.get("grace_period", "1h")),
            enabled=bool(d.get("enabled", True)),
        )
        return cls(
            trig=trig,
            created_at=str(d.get("created_at", "")),
            created_by=str(d.get("created_by", "agent")),
        )


@dataclass(slots=True)
class DynamicOnceRecord:
    """One persisted one-shot dynamic trigger."""

    trig: OnceTrigger
    created_at: str
    created_by: str

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.trig.id,
            "fire_at": self.trig.fire_at,
            "timezone": self.trig.timezone,
            "message": self.trig.message,
            "grace_period": self.trig.grace_period,
            "enabled": self.trig.enabled,
            "created_at": self.created_at,
            "created_by": self.created_by,
        }

    @classmethod
    def from_json(cls, d: dict[str, object]) -> DynamicOnceRecord:
        tid = str(d["id"])
        if not tid.startswith(DYNAMIC_ID_PREFIX):
            raise ConfigError(f"dynamic trigger id missing {DYNAMIC_ID_PREFIX!r} prefix: {tid!r}")
        trig = OnceTrigger(
            id=tid,
            fire_at=str(d["fire_at"]),
            timezone=str(d["timezone"]),
            message=str(d["message"]),
            grace_period=str(d.get("grace_period", "1h")),
            enabled=bool(d.get("enabled", True)),
        )
        return cls(
            trig=trig,
            created_at=str(d.get("created_at", "")),
            created_by=str(d.get("created_by", "agent")),
        )


class DynamicTriggerStore:
    """Atomic, lock-guarded JSON-on-disk store for one eonlet's dynamic triggers.

    Holds two parallel lists — recurring (``cron``) and one-shot (``once``).
    IDs share a namespace: ``dyn-…`` IDs must be unique across both kinds.
    """

    def __init__(self, eonlet_dir: Path) -> None:
        self._path = eonlet_dir / STORE_FILENAME
        self._lock = anyio.Lock()
        self._records: list[DynamicTriggerRecord] = []
        self._once: list[DynamicOnceRecord] = []
        self._loaded = False

    # ── load / save ─────────────────────────────────────────────────────────

    def load(self) -> tuple[list[DynamicTriggerRecord], list[DynamicOnceRecord]]:
        """Read the JSON file (or return empties if missing). Populates the cache."""
        if not self._path.exists():
            self._records = []
            self._once = []
            self._loaded = True
            return [], []
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        version = raw.get("version") if isinstance(raw, dict) else None
        if version != STORE_VERSION:
            raise ConfigError(
                f"dynamic_triggers.json: unsupported version {version!r} (expected {STORE_VERSION})"
            )
        self._records = [DynamicTriggerRecord.from_json(d) for d in (raw.get("triggers") or [])]
        self._once = [DynamicOnceRecord.from_json(d) for d in (raw.get("once") or [])]
        self._loaded = True
        return list(self._records), list(self._once)

    def _write_sync(self) -> None:
        """Atomic write-temp-then-rename. Caller must hold ``self._lock``."""
        payload = {
            "version": STORE_VERSION,
            "triggers": [r.to_json() for r in self._records],
            "once": [r.to_json() for r in self._once],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._path)

    def _has_id(self, trigger_id: str) -> bool:
        return any(r.trig.id == trigger_id for r in self._records) or any(
            r.trig.id == trigger_id for r in self._once
        )

    # ── mutations: cron ─────────────────────────────────────────────────────

    async def add(self, record: DynamicTriggerRecord) -> None:
        async with self._lock:
            if self._has_id(record.trig.id):
                raise ConfigError(f"duplicate trigger id: {record.trig.id!r}")
            self._records.append(record)
            self._write_sync()

    # ── mutations: once ─────────────────────────────────────────────────────

    async def add_once(self, record: DynamicOnceRecord) -> None:
        async with self._lock:
            if self._has_id(record.trig.id):
                raise ConfigError(f"duplicate trigger id: {record.trig.id!r}")
            self._once.append(record)
            self._write_sync()

    # ── mutations: shared ───────────────────────────────────────────────────

    async def remove(self, trigger_id: str) -> bool:
        """Drop by ID from either list. Returns True if removed. Refuses non-``dyn-`` IDs."""
        if not is_dynamic_id(trigger_id):
            raise ConfigError(f"refusing to remove non-dynamic trigger: {trigger_id!r}")
        async with self._lock:
            before = len(self._records) + len(self._once)
            self._records = [r for r in self._records if r.trig.id != trigger_id]
            self._once = [r for r in self._once if r.trig.id != trigger_id]
            if len(self._records) + len(self._once) == before:
                return False
            self._write_sync()
            return True

    async def set_enabled(self, trigger_id: str, enabled: bool) -> bool:
        async with self._lock:
            for r in self._records:
                if r.trig.id == trigger_id:
                    r.trig.enabled = enabled
                    self._write_sync()
                    return True
            for r2 in self._once:
                if r2.trig.id == trigger_id:
                    r2.trig.enabled = enabled
                    self._write_sync()
                    return True
            return False

    async def clear(self) -> int:
        async with self._lock:
            n = len(self._records) + len(self._once)
            if n == 0:
                return 0
            self._records = []
            self._once = []
            self._write_sync()
            return n

    # ── lookup ──────────────────────────────────────────────────────────────

    def all(self) -> list[DynamicTriggerRecord]:
        return list(self._records)

    def all_once(self) -> list[DynamicOnceRecord]:
        return list(self._once)

    def get(self, trigger_id: str) -> DynamicTriggerRecord | None:
        return next((r for r in self._records if r.trig.id == trigger_id), None)

    def get_once(self, trigger_id: str) -> DynamicOnceRecord | None:
        return next((r for r in self._once if r.trig.id == trigger_id), None)


# ── helpers ──────────────────────────────────────────────────────────────────


def is_dynamic_id(trigger_id: str) -> bool:
    return trigger_id.startswith(DYNAMIC_ID_PREFIX)


def mint_dynamic_id(now: datetime | None = None) -> str:
    """Return a fresh ``dyn-YYYY-MM-DD-XXXX`` ID (4 hex chars from secrets)."""
    when = now or datetime.now(UTC)
    suffix = secrets.token_hex(2)
    return f"{DYNAMIC_ID_PREFIX}{when.strftime('%Y-%m-%d')}-{suffix}"
