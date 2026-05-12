"""Recall index — SQLite FTS5 over the event log (MEMORY_SPEC §2.5 / §5.1).

The index is **derived state**. If ``index.sqlite`` is missing, corrupt, or
schema-mismatched, the runtime rebuilds it from the event store on startup
(M-I1). Writes are incremental: every appended event with text-bearing
payload is also written here.

Memory documents (STM/LTM/notes) get their own virtual table ``memory_fts``;
populating it is the responsibility of the compaction layer (P4/P5). For
P3 the schema exists but only ``msg_fts`` is actively maintained.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..runtime.events import Event, EventKind
from .paths import index_db_path

# Bump this when the schema changes — a mismatch triggers a rebuild.
SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
    content,
    role UNINDEXED,
    kind UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS msg_meta (
    event_id   INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    role       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    fts_rowid  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS msg_meta_ts ON msg_meta(ts);
CREATE INDEX IF NOT EXISTS msg_meta_kind ON msg_meta(kind);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    doc UNINDEXED,
    section_id UNINDEXED,
    content,
    tokenize='unicode61 remove_diacritics 2'
);
"""


# Events that carry searchable user-facing text. Permission / budget / log
# events live in the event store but are noise in recall results.
_TEXT_KINDS: frozenset[EventKind] = frozenset(
    {
        EventKind.USER_MESSAGE,
        EventKind.ASSISTANT_MESSAGE,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_ERROR,
        EventKind.TOOL_CALL,
    }
)


def _role_of(kind: EventKind) -> str:
    if kind == EventKind.USER_MESSAGE:
        return "user"
    if kind == EventKind.ASSISTANT_MESSAGE:
        return "assistant"
    if kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR, EventKind.TOOL_CALL):
        return "tool"
    return "system"


def _text_of(event: Event) -> str | None:
    """Return the searchable text for an event, or None if it isn't text-bearing."""
    if event.kind not in _TEXT_KINDS:
        return None
    payload = event.payload
    if event.kind in (EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE):
        return str(payload.get("content") or "") or None
    if event.kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR):
        return str(payload.get("output") or "") or None
    if event.kind == EventKind.TOOL_CALL:
        name = str(payload.get("tool_name") or "")
        args = payload.get("args") or {}
        return f"{name} {args}".strip() or None
    return None


@dataclass(slots=True)
class IndexedMsg:
    """One hit from a recall query."""

    event_id: int
    ts: int  # microseconds since epoch
    role: str
    kind: str
    content: str

    @property
    def iso_ts(self) -> str:
        return datetime.fromtimestamp(self.ts / 1_000_000, tz=UTC).isoformat()


# ── Index ──────────────────────────────────────────────────────────────────


class RecallIndex:
    """Synchronous SQLite handle. Single writer (the worker).

    Methods are sync because SQLite's APIs are sync and the writes are small
    (a few ms). Callers that want non-blocking semantics should wrap in
    ``anyio.to_thread.run_sync``.
    """

    def __init__(self, memory_dir: Path) -> None:
        self._path = index_db_path(memory_dir)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current == 0:
            # Fresh database — create everything.
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.commit()
            return
        if current != SCHEMA_VERSION:
            # Wrong version — rebuild from scratch. Callers will repopulate
            # via ``rebuild_from_events``.
            self.reset()
            return
        # Sanity: tables present?
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()
        names = {r[0] for r in rows}
        required = {"msg_fts", "msg_meta", "memory_fts"}
        if not required.issubset(names):
            self.reset()

    def reset(self) -> None:
        """Drop all tables and recreate the schema. Used on schema mismatch."""
        for tbl in ("msg_fts", "msg_meta", "memory_fts"):
            self._conn.execute(f"DROP TABLE IF EXISTS {tbl}")
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ── highest indexed event ──────────────────────────────────────────────

    def latest_indexed_id(self) -> int:
        """Return the highest event id already in the index, or 0 if empty."""
        row = self._conn.execute("SELECT COALESCE(MAX(event_id), 0) FROM msg_meta").fetchone()
        return int(row[0])

    # ── incremental write ──────────────────────────────────────────────────

    def index_event(self, event: Event) -> None:
        """Index a single event. No-op for non-text-bearing kinds."""
        if event.id is None:
            return
        text = _text_of(event)
        if text is None:
            return
        # Skip if already indexed — keeps replay-on-startup idempotent.
        existing = self._conn.execute(
            "SELECT 1 FROM msg_meta WHERE event_id=?", (event.id,)
        ).fetchone()
        if existing is not None:
            return
        role = _role_of(event.kind)
        cur = self._conn.cursor()
        cur.execute(
            "INSERT INTO msg_fts(content, role, kind) VALUES (?, ?, ?)",
            (text, role, str(event.kind)),
        )
        fts_rowid = cur.lastrowid
        cur.execute(
            "INSERT INTO msg_meta(event_id, ts, role, kind, fts_rowid) VALUES (?, ?, ?, ?, ?)",
            (event.id, event.ts, role, str(event.kind), fts_rowid),
        )
        self._conn.commit()

    def rebuild_from_events(self, events: Iterable[Event]) -> int:
        """Drop and re-index from a sequence of events. Returns count indexed."""
        self.reset()
        n = 0
        for ev in events:
            self.index_event(ev)
            n += 1
        return n

    # ── queries ────────────────────────────────────────────────────────────

    def search_keyword(self, query: str, *, limit: int = 20) -> list[IndexedMsg]:
        """FTS5 MATCH over content. ``query`` is passed to FTS5 verbatim."""
        if not query.strip():
            return []
        # The FTS5 query is bare user text. Quote it to disable operator
        # parsing — a query like ``a OR b`` from a user should be a phrase
        # search, not an OR. Use double-quotes; FTS5 escapes ``"`` as ``""``.
        safe = '"' + query.replace('"', '""') + '"'
        rows = self._conn.execute(
            """
            SELECT m.event_id, m.ts, m.role, m.kind, f.content
            FROM msg_fts f
            JOIN msg_meta m ON m.fts_rowid = f.rowid
            WHERE msg_fts MATCH ?
            ORDER BY m.ts DESC
            LIMIT ?
            """,
            (safe, limit),
        ).fetchall()
        return [IndexedMsg(*r) for r in rows]

    def events_on_date(self, date_iso: str, *, limit: int = 200) -> list[IndexedMsg]:
        """All events on ``date_iso`` (YYYY-MM-DD), interpreted as UTC."""
        start, end = _utc_day_bounds(date_iso)
        return self.events_in_range_us(start, end, limit=limit)

    def events_in_range(
        self, start_iso: str, end_iso: str, *, limit: int = 500
    ) -> list[IndexedMsg]:
        start_us = _iso_to_us(start_iso)
        end_us = _iso_to_us(end_iso)
        return self.events_in_range_us(start_us, end_us, limit=limit)

    def events_in_range_us(self, start_us: int, end_us: int, *, limit: int) -> list[IndexedMsg]:
        rows = self._conn.execute(
            """
            SELECT m.event_id, m.ts, m.role, m.kind, f.content
            FROM msg_meta m
            JOIN msg_fts f ON f.rowid = m.fts_rowid
            WHERE m.ts >= ? AND m.ts < ?
            ORDER BY m.ts ASC
            LIMIT ?
            """,
            (start_us, end_us, limit),
        ).fetchall()
        return [IndexedMsg(*r) for r in rows]

    def around_event(self, event_id: int, *, radius: int = 5) -> list[IndexedMsg]:
        """Return ``radius`` events on either side of ``event_id`` by id order."""
        lo = max(0, event_id - radius)
        hi = event_id + radius
        rows = self._conn.execute(
            """
            SELECT m.event_id, m.ts, m.role, m.kind, f.content
            FROM msg_meta m
            JOIN msg_fts f ON f.rowid = m.fts_rowid
            WHERE m.event_id BETWEEN ? AND ?
            ORDER BY m.event_id ASC
            """,
            (lo, hi),
        ).fetchall()
        return [IndexedMsg(*r) for r in rows]


# ── date helpers ────────────────────────────────────────────────────────────


def _utc_day_bounds(date_iso: str) -> tuple[int, int]:
    """Return ``[start, end)`` in microseconds for the UTC day named by ``date_iso``."""
    try:
        day = datetime.strptime(date_iso, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise ValueError(f"invalid date {date_iso!r}, expected YYYY-MM-DD") from e
    start = int(day.timestamp() * 1_000_000)
    end = int((day + timedelta(days=1)).timestamp() * 1_000_000)
    return start, end


def _iso_to_us(value: str) -> int:
    """Parse a date or full ISO datetime to microseconds since epoch (UTC)."""
    # Allow plain YYYY-MM-DD (treated as UTC midnight).
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return _utc_day_bounds(value)[0]
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"invalid datetime {value!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1_000_000)
