"""SQLite event store.

Per SPEC §7.4: single writer (the worker), msgpack payload encoding, WAL mode.
We use apsw if available, falling back to stdlib sqlite3 — both speak the same
SQL and apsw isn't strictly required for the MVP's correctness.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import msgpack

try:
    import apsw

    _HAS_APSW = True
except ImportError:  # pragma: no cover — fallback only
    import sqlite3 as apsw  # type: ignore[no-redef]

    _HAS_APSW = False

from .events import Event, EventKind

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     BLOB NOT NULL,
    parent_id   INTEGER,
    trigger_id  TEXT,
    cost_usd    REAL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    FOREIGN KEY (parent_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS events_ts_idx      ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_idx    ON events(kind, id);
CREATE INDEX IF NOT EXISTS events_trigger_idx ON events(trigger_id, id);

CREATE TABLE IF NOT EXISTS trigger_state (
    trigger_id            TEXT PRIMARY KEY,
    last_fired_at         INTEGER,
    last_success_at       INTEGER,
    last_failure_at       INTEGER,
    consecutive_failures  INTEGER DEFAULT 0,
    total_fires           INTEGER DEFAULT 0,
    total_successes       INTEGER DEFAULT 0
);
"""


class EventStore:
    """Append-only event log over SQLite. Single-writer per process."""

    def __init__(self, db_path: Path | str) -> None:
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        if _HAS_APSW:
            self._conn = apsw.Connection(str(db_path))
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        else:
            # Fallback path: ``apsw`` here is actually stdlib ``sqlite3``.
            self._conn = apsw.connect(str(db_path), isolation_level=None)  # type: ignore[attr-defined]
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        for stmt in _SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                self._conn.execute(s)

    # ── core ops ─────────────────────────────────────────────────────────────

    def append(self, event: Event) -> Event:
        """Persist an event and return it with its assigned ``id``."""
        payload_blob = msgpack.packb(event.payload, use_bin_type=True)
        cur = self._conn.cursor()
        cur.execute(
            """INSERT INTO events (ts, kind, payload, parent_id, trigger_id,
                                   cost_usd, tokens_in, tokens_out)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.ts,
                str(event.kind),
                payload_blob,
                event.parent_id,
                event.trigger_id,
                event.cost_usd,
                event.tokens_in,
                event.tokens_out,
            ),
        )
        # apsw and sqlite3 both support last_insert_rowid via this query
        row = next(self._conn.execute("SELECT last_insert_rowid()"))
        new_id = row[0]
        return event.model_copy(update={"id": new_id})

    def read(self, *, since: int = 0, limit: int | None = None) -> list[Event]:
        """Read events with ``id > since``, oldest first."""
        sql = (
            "SELECT id, ts, kind, payload, parent_id, trigger_id, "
            "cost_usd, tokens_in, tokens_out "
            "FROM events WHERE id > ? ORDER BY id ASC"
        )
        params: list[Any] = [since]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        out: list[Event] = []
        for row in self._conn.execute(sql, params):
            out.append(_row_to_event(row))
        return out

    def latest_id(self) -> int:
        row = next(self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM events"))
        return int(row[0])

    def count(self) -> int:
        row = next(self._conn.execute("SELECT COUNT(*) FROM events"))
        return int(row[0])

    # ── trigger state ────────────────────────────────────────────────────────

    def get_trigger_state(self, trigger_id: str) -> dict[str, Any]:
        row = next(
            self._conn.execute(
                """SELECT last_fired_at, last_success_at, last_failure_at,
                          consecutive_failures, total_fires, total_successes
                   FROM trigger_state WHERE trigger_id=?""",
                (trigger_id,),
            ),
            (None, None, None, 0, 0, 0),
        )
        return {
            "last_fired_at": row[0],
            "last_success_at": row[1],
            "last_failure_at": row[2],
            "consecutive_failures": row[3] or 0,
            "total_fires": row[4] or 0,
            "total_successes": row[5] or 0,
        }

    def update_trigger_state(self, trigger_id: str, **fields: Any) -> None:
        cur = self.get_trigger_state(trigger_id)
        merged = {**cur, **fields}
        self._conn.execute(
            """INSERT INTO trigger_state
               (trigger_id, last_fired_at, last_success_at, last_failure_at,
                consecutive_failures, total_fires, total_successes)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(trigger_id) DO UPDATE SET
                 last_fired_at=excluded.last_fired_at,
                 last_success_at=excluded.last_success_at,
                 last_failure_at=excluded.last_failure_at,
                 consecutive_failures=excluded.consecutive_failures,
                 total_fires=excluded.total_fires,
                 total_successes=excluded.total_successes""",
            (
                trigger_id,
                merged["last_fired_at"],
                merged["last_success_at"],
                merged["last_failure_at"],
                merged["consecutive_failures"],
                merged["total_fires"],
                merged["total_successes"],
            ),
        )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Best-effort transaction wrapper (apsw uses implicit txns)."""
        self._conn.execute("BEGIN")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise


def _row_to_event(row: tuple[Any, ...]) -> Event:
    (id_, ts, kind, payload_blob, parent_id, trigger_id, cost_usd, tokens_in, tokens_out) = row
    payload = msgpack.unpackb(bytes(payload_blob), raw=False) if payload_blob else {}
    return Event(
        id=id_,
        ts=ts,
        kind=EventKind(kind),
        payload=payload,
        parent_id=parent_id,
        trigger_id=trigger_id,
        cost_usd=cost_usd,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
