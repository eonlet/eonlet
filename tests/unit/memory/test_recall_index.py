"""RecallIndex schema, incremental write, and queries (MEMORY_SPEC §2.5 / §5.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from eonlet.memory.recall import SCHEMA_VERSION, RecallIndex
from eonlet.runtime.events import (
    Event,
    EventKind,
    assistant_message,
    tool_call,
    tool_result,
    user_message,
)


def _make_event(ev: Event, *, id_: int, ts: int) -> Event:
    return ev.model_copy(update={"id": id_, "ts": ts})


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1_000_000)


# ── schema & open ──────────────────────────────────────────────────────────


def test_fresh_db_has_schema_version(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        cur = idx._conn.execute("PRAGMA user_version")
        assert int(cur.fetchone()[0]) == SCHEMA_VERSION
    finally:
        idx.close()


def test_schema_mismatch_triggers_rebuild(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    idx.index_event(_make_event(user_message("hello"), id_=1, ts=_ts(2026, 5, 22)))
    assert idx.latest_indexed_id() == 1
    # Forge a future schema version.
    idx._conn.execute("PRAGMA user_version = 999")
    idx._conn.commit()
    idx.close()
    # Reopen — should reset and lose the row.
    idx2 = RecallIndex(tmp_path)
    try:
        assert idx2.latest_indexed_id() == 0
    finally:
        idx2.close()


def test_reset_recreates_tables(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("x"), id_=1, ts=_ts(2026, 5, 22)))
        idx.reset()
        assert idx.latest_indexed_id() == 0
    finally:
        idx.close()


# ── incremental indexing ────────────────────────────────────────────────────


def test_index_event_skips_non_text_kinds(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        ev = _make_event(
            Event(kind=EventKind.PERMISSION_GRANTED, payload={"tool_name": "x"}),
            id_=1,
            ts=_ts(2026, 5, 22),
        )
        idx.index_event(ev)
        assert idx.latest_indexed_id() == 0  # not indexed

    finally:
        idx.close()


def test_index_event_is_idempotent(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        ev = _make_event(user_message("once"), id_=1, ts=_ts(2026, 5, 22))
        idx.index_event(ev)
        idx.index_event(ev)
        idx.index_event(ev)
        hits = idx.search_keyword("once")
        assert len(hits) == 1
    finally:
        idx.close()


def test_event_without_id_is_ignored(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(user_message("no id yet"))  # id is None
        assert idx.latest_indexed_id() == 0
    finally:
        idx.close()


# ── queries ────────────────────────────────────────────────────────────────


def test_search_keyword_phrase_hits_only_matching(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("trim AAPL by 3%"), id_=1, ts=_ts(2026, 5, 22)))
        idx.index_event(
            _make_event(assistant_message("scheduled the trade"), id_=2, ts=_ts(2026, 5, 22, 13))
        )
        idx.index_event(_make_event(user_message("unrelated topic"), id_=3, ts=_ts(2026, 5, 22)))
        hits = idx.search_keyword("AAPL")
        ids = [h.event_id for h in hits]
        assert ids == [1]
    finally:
        idx.close()


def test_search_keyword_quoting_disables_operators(tmp_path: Path) -> None:
    """A user query 'foo OR bar' should be treated as a phrase, not as an FTS
    boolean expression."""
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("just foo here"), id_=1, ts=_ts(2026, 5, 22)))
        idx.index_event(_make_event(user_message("just bar"), id_=2, ts=_ts(2026, 5, 22, 13)))
        idx.index_event(
            _make_event(user_message("foo OR bar literally"), id_=3, ts=_ts(2026, 5, 22, 14))
        )
        hits = idx.search_keyword("foo OR bar")
        # Quoted as a phrase, only #3 contains the literal string.
        assert [h.event_id for h in hits] == [3]
    finally:
        idx.close()


def test_events_on_date_returns_full_day(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("a"), id_=1, ts=_ts(2026, 5, 21, 23)))
        idx.index_event(_make_event(user_message("b"), id_=2, ts=_ts(2026, 5, 22, 0)))
        idx.index_event(_make_event(user_message("c"), id_=3, ts=_ts(2026, 5, 22, 12)))
        idx.index_event(_make_event(user_message("d"), id_=4, ts=_ts(2026, 5, 23, 0)))
        hits = idx.events_on_date("2026-05-22")
        assert [h.event_id for h in hits] == [2, 3]
    finally:
        idx.close()


def test_events_in_range_inclusive_start_exclusive_end(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("a"), id_=1, ts=_ts(2026, 5, 21)))
        idx.index_event(_make_event(user_message("b"), id_=2, ts=_ts(2026, 5, 22)))
        idx.index_event(_make_event(user_message("c"), id_=3, ts=_ts(2026, 5, 23)))
        hits = idx.events_in_range("2026-05-22", "2026-05-23")
        assert [h.event_id for h in hits] == [2]
    finally:
        idx.close()


def test_around_event_returns_neighbors(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        for i in range(1, 11):
            idx.index_event(_make_event(user_message(f"m{i}"), id_=i, ts=_ts(2026, 5, 22, 10 + i)))
        hits = idx.around_event(5, radius=2)
        assert [h.event_id for h in hits] == [3, 4, 5, 6, 7]
    finally:
        idx.close()


def test_tool_call_and_result_are_indexed(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(
            _make_event(tool_call("c1", "bash", {"command": "ls"}), id_=1, ts=_ts(2026, 5, 22))
        )
        idx.index_event(
            _make_event(
                tool_result("c1", "bash", "main.py README.md"),
                id_=2,
                ts=_ts(2026, 5, 22),
            )
        )
        # tool_call payload includes tool name; result includes its output.
        hits = idx.search_keyword("README.md")
        assert [h.event_id for h in hits] == [2]
        hits2 = idx.search_keyword("bash")
        assert any(h.event_id == 1 for h in hits2)
    finally:
        idx.close()


def test_rebuild_from_events_replaces_state(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("old"), id_=1, ts=_ts(2026, 5, 1)))
        assert idx.latest_indexed_id() == 1
        events = [
            _make_event(user_message("new-a"), id_=10, ts=_ts(2026, 6, 1)),
            _make_event(user_message("new-b"), id_=11, ts=_ts(2026, 6, 2)),
        ]
        n = idx.rebuild_from_events(events)
        assert n == 2
        assert idx.latest_indexed_id() == 11
        assert not idx.search_keyword("old")
    finally:
        idx.close()


def test_empty_query_returns_empty(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("x"), id_=1, ts=_ts(2026, 5, 22)))
        assert idx.search_keyword("") == []
        assert idx.search_keyword("   ") == []
    finally:
        idx.close()


# ── date validation ─────────────────────────────────────────────────────────


def test_events_on_date_rejects_invalid_format(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        with pytest.raises(ValueError):
            idx.events_on_date("not-a-date")
        with pytest.raises(ValueError):
            idx.events_on_date("2026/05/22")
    finally:
        idx.close()


def test_events_in_range_accepts_full_iso(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    try:
        idx.index_event(_make_event(user_message("a"), id_=1, ts=_ts(2026, 5, 22, 10)))
        idx.index_event(_make_event(user_message("b"), id_=2, ts=_ts(2026, 5, 22, 14)))
        hits = idx.events_in_range("2026-05-22T11:00:00+00:00", "2026-05-22T15:00:00+00:00")
        assert [h.event_id for h in hits] == [2]
    finally:
        idx.close()


# ── persistence across reopen ──────────────────────────────────────────────


def test_reopen_preserves_index(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    idx.index_event(_make_event(user_message("hello"), id_=1, ts=_ts(2026, 5, 22)))
    idx.close()
    idx2 = RecallIndex(tmp_path)
    try:
        assert idx2.latest_indexed_id() == 1
        assert len(idx2.search_keyword("hello")) == 1
    finally:
        idx2.close()
