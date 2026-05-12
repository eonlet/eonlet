"""Context injection: preamble assembly + recent-window slicing (MEMORY_SPEC §3)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.config import MemoryConfig
from eonlet.memory.injection import (
    build_memory_preamble,
    select_recent_window,
    working_window_token_estimate,
)
from eonlet.memory.notes import NotesStore
from eonlet.memory.todos import TodosStore
from eonlet.runtime.events import (
    Event,
    EventKind,
    assistant_message,
    tool_call,
    tool_result,
    user_message,
)


def _evt(ev: Event, *, id_: int) -> Event:
    return ev.model_copy(update={"id": id_})


# ── preamble assembly ──────────────────────────────────────────────────────


def test_empty_dir_returns_empty_preamble(tmp_path: Path) -> None:
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert out == ""


def test_preamble_disabled_when_subsystem_off(tmp_path: Path) -> None:
    (tmp_path / "long_term.md").write_text("# LTM\nstuff")
    cfg = MemoryConfig(enabled=False)
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert out == ""


def test_preamble_includes_ltm_when_present(tmp_path: Path) -> None:
    (tmp_path / "long_term.md").write_text("## user\n- prefers concise")
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "<memory>" in out
    assert "<long_term>" in out
    assert "prefers concise" in out
    assert "</memory>" in out


def test_preamble_includes_pending_todos_only(tmp_path: Path) -> None:
    store = TodosStore(tmp_path)
    anyio.run(lambda: store.add(id="t1", content="do thing"))
    anyio.run(lambda: store.add(id="t2", content="archived"))
    anyio.run(lambda: store.mark_done(id="t2"))
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "do thing" in out
    assert "archived" not in out  # done items NOT injected


def test_preamble_ordering_long_notes_todos_short(tmp_path: Path) -> None:
    (tmp_path / "long_term.md").write_text("LTM-MARKER")
    (tmp_path / "short_term.md").write_text("STM-MARKER")
    nstore = NotesStore(tmp_path)
    anyio.run(lambda: nstore.add(id="n1", content="NOTE-MARKER"))
    tstore = TodosStore(tmp_path)
    anyio.run(lambda: tstore.add(id="t1", content="TODO-MARKER"))

    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    # All four substrings present and in spec-ordered positions
    p_ltm = out.index("LTM-MARKER")
    p_note = out.index("NOTE-MARKER")
    p_todo = out.index("TODO-MARKER")
    p_stm = out.index("STM-MARKER")
    assert p_ltm < p_note < p_todo < p_stm


def test_preamble_omits_notes_when_inject_false(tmp_path: Path) -> None:
    nstore = NotesStore(tmp_path)
    anyio.run(lambda: nstore.add(id="n1", content="HIDDEN"))
    cfg = MemoryConfig.model_validate({"notes": {"inject": False}})
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "HIDDEN" not in out


def test_preamble_omits_todos_when_inject_active_false(tmp_path: Path) -> None:
    tstore = TodosStore(tmp_path)
    anyio.run(lambda: tstore.add(id="t1", content="HIDDEN_TODO"))
    cfg = MemoryConfig.model_validate({"todos": {"inject_active": False}})
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "HIDDEN_TODO" not in out


# ── recent window selection ────────────────────────────────────────────────


def test_window_respects_watermark() -> None:
    events = [
        _evt(user_message("old"), id_=1),
        _evt(assistant_message("old reply"), id_=2),
        _evt(user_message("new"), id_=3),
        _evt(assistant_message("new reply"), id_=4),
    ]
    cfg = MemoryConfig()
    out = select_recent_window(events, cfg, watermark=2)
    assert [e.id for e in out.events] == [3, 4]


def test_window_keeps_min_messages_even_under_tight_budget() -> None:
    events = [_evt(user_message("x" * 5000), id_=i) for i in range(1, 6)]
    cfg = MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 100, "keep_recent_messages_min": 3}}
    )
    out = select_recent_window(events, cfg, watermark=0)
    # min=3 guarantees we keep the last 3 even when budget is blown.
    assert len(out.events) == 3
    assert [e.id for e in out.events] == [3, 4, 5]


def test_window_skips_orphan_tool_result_at_boundary() -> None:
    events = [
        _evt(user_message("u"), id_=1),
        _evt(assistant_message("a", tool_calls=[{"id": "c", "name": "x", "args": {}}]), id_=2),
        _evt(tool_call("c", "x", {}), id_=3),
        _evt(tool_result("c", "x", "out"), id_=4),
        _evt(user_message("u2"), id_=5),
    ]
    cfg = MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 64, "keep_recent_messages_min": 1}}
    )
    out = select_recent_window(events, cfg, watermark=3)  # only events with id>3
    # id=4 is a tool_result whose call is outside the window → must be skipped.
    assert all(e.kind != EventKind.TOOL_RESULT for e in out.events[:1])
    assert any(e.id == 5 for e in out.events)


def test_working_window_token_estimate_counts_text_events() -> None:
    events = [
        _evt(user_message("a" * 400), id_=1),  # ~100 tokens
        _evt(assistant_message("b" * 400), id_=2),
        _evt(Event(kind=EventKind.PERMISSION_GRANTED, payload={}), id_=3),  # 0
    ]
    n = working_window_token_estimate(events, watermark=0)
    assert n > 150  # roughly 200+
    # watermark pruning works
    assert working_window_token_estimate(events, watermark=99) == 0
