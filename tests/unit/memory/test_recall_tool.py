"""`recall` builtin tool — modes, include scopes, event emission."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio

from eonlet.memory.recall import RecallIndex
from eonlet.runtime.events import Event, EventKind, user_message
from eonlet.tools.builtin.recall import RecallArgs, RecallTool
from eonlet.tools.protocol import ToolContext


def _ts(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1_000_000)


def _make_event(ev: Event, *, id_: int, ts: int) -> Event:
    return ev.model_copy(update={"id": id_, "ts": ts})


def _ctx(tmp_path: Path, idx: RecallIndex) -> tuple[ToolContext, list[Event]]:
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        stamped = ev.model_copy(update={"id": len(captured) + 1000})
        captured.append(stamped)
        return stamped

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
        record_event=record,
        extra={"recall_index": idx},
    )
    return ctx, captured


def test_by_keyword_returns_markdown_with_event_id(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    idx.index_event(_make_event(user_message("trim AAPL"), id_=42, ts=_ts(2026, 5, 22)))
    ctx, captured = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="by_keyword", query="AAPL"), ctx)
        assert not out.is_error
        assert "AAPL" in out.content
        assert "#42" in out.content

    anyio.run(go)
    idx.close()
    assert any(e.kind == EventKind.MEM_RECALL_INVOKED for e in captured)
    recall_ev = next(e for e in captured if e.kind == EventKind.MEM_RECALL_INVOKED)
    assert recall_ev.payload["hits"] == 1
    assert recall_ev.payload["query"] == "AAPL"


def test_by_keyword_missing_query_errors(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="by_keyword"), ctx)
        assert out.is_error and "query" in out.content

    anyio.run(go)
    idx.close()


def test_by_date_filters_correctly(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    idx.index_event(_make_event(user_message("yday"), id_=1, ts=_ts(2026, 5, 21)))
    idx.index_event(_make_event(user_message("today"), id_=2, ts=_ts(2026, 5, 22)))
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="by_date", date="2026-05-22"), ctx)
        assert "today" in out.content
        assert "yday" not in out.content

    anyio.run(go)
    idx.close()


def test_around_event_renders_neighbors(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    for i in range(1, 8):
        idx.index_event(_make_event(user_message(f"m{i}"), id_=i, ts=_ts(2026, 5, 22, 10 + i)))
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(
            RecallArgs(mode="around_event", around_event_id=4, context_radius=1),
            ctx,
        )
        # Should contain ids 3, 4, 5 — not 2 or 6.
        assert "#3" in out.content
        assert "#4" in out.content
        assert "#5" in out.content
        assert "#2" not in out.content
        assert "#6" not in out.content

    anyio.run(go)
    idx.close()


def test_around_event_missing_id_errors(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="around_event"), ctx)
        assert out.is_error and "around_event_id" in out.content

    anyio.run(go)
    idx.close()


def test_includes_notes_in_keyword_mode(tmp_path: Path) -> None:
    from eonlet.memory.notes import NotesStore

    idx = RecallIndex(tmp_path)
    # No events match, but a note does.
    store = NotesStore(tmp_path)
    anyio.run(lambda: store.add(id="n1", content="reminder about AAPL"))
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(
            RecallArgs(mode="by_keyword", query="AAPL", include=["events", "notes"]),
            ctx,
        )
        assert "notes hits" in out.content
        assert "reminder about AAPL" in out.content

    anyio.run(go)
    idx.close()


def test_includes_todos_in_keyword_mode(tmp_path: Path) -> None:
    from eonlet.memory.todos import TodosStore

    idx = RecallIndex(tmp_path)
    tstore = TodosStore(tmp_path)
    anyio.run(lambda: tstore.add(id="t1", content="check the AAPL filing"))
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(
            RecallArgs(mode="by_keyword", query="AAPL", include=["events", "todos"]),
            ctx,
        )
        assert "todos hits" in out.content
        assert "AAPL filing" in out.content

    anyio.run(go)
    idx.close()


def test_no_index_in_context_errors(tmp_path: Path) -> None:
    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
    )

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="by_keyword", query="x"), ctx)
        assert out.is_error and "index" in out.content

    anyio.run(go)


def test_invalid_date_propagates_as_error(tmp_path: Path) -> None:
    idx = RecallIndex(tmp_path)
    ctx, _ = _ctx(tmp_path, idx)

    async def go() -> None:
        out = await RecallTool()(RecallArgs(mode="by_date", date="garbage"), ctx)
        assert out.is_error and "invalid date" in out.content

    anyio.run(go)
    idx.close()
