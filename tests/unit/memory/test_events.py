"""New memory event kinds and constructors (MEMORY_SPEC §7)."""

from __future__ import annotations

import pytest

from eonlet.runtime.events import (
    EventKind,
    mem_compacted,
    mem_ltm_forgotten,
    mem_ltm_promoted,
    mem_note_added,
    mem_note_deleted,
    mem_note_updated,
    mem_paused,
    mem_recall_invoked,
    mem_remember,
    mem_resumed,
    mem_todo_added,
    mem_todo_deleted,
    mem_todo_updated,
)


def test_all_memory_kinds_exist() -> None:
    expected = {
        "mem_compacted",
        "mem_ltm_promoted",
        "mem_ltm_forgotten",
        "mem_note_added",
        "mem_note_updated",
        "mem_note_deleted",
        "mem_todo_added",
        "mem_todo_updated",
        "mem_todo_deleted",
        "mem_remember",
        "mem_recall_invoked",
        "mem_paused",
        "mem_resumed",
    }
    actual = {k.value for k in EventKind}
    missing = expected - actual
    assert not missing, f"missing event kinds: {missing}"


def test_mem_compacted_payload() -> None:
    ev = mem_compacted(
        snapshot_id=42,
        boundary_event_id=40,
        sections_added=2,
        tokens_before=9000,
        tokens_after=1200,
        model="claude-haiku-4.5@anthropic",
    )
    assert ev.kind == EventKind.MEM_COMPACTED
    assert ev.payload["tier"] == 1
    assert ev.payload["boundary_event_id"] == 40
    assert ev.payload["sections_added"] == 2
    assert ev.payload["model"].startswith("claude-haiku")


def test_mem_ltm_promoted_carries_additions() -> None:
    ev = mem_ltm_promoted(
        snapshot_id=100,
        additions=[{"section": "user", "content": "prefers concise replies"}],
        kept_section_count=3,
        model="fake-echo",
    )
    assert ev.kind == EventKind.MEM_LTM_PROMOTED
    assert ev.payload["additions"][0]["section"] == "user"
    assert ev.payload["kept_section_count"] == 3


def test_mem_ltm_forgotten_tier3() -> None:
    ev = mem_ltm_forgotten(
        snapshot_id=200,
        kept_count=10,
        dropped_count=4,
        dropped_digest=[{"section": "fact", "preview": "old fact", "reason": "stale"}],
        cause="tier3",
        model="fake-echo",
    )
    assert ev.kind == EventKind.MEM_LTM_FORGOTTEN
    assert ev.payload["cause"] == "tier3"
    assert ev.payload["kept_count"] == 10
    assert ev.payload["model"] == "fake-echo"


def test_mem_ltm_forgotten_forget_action_omits_optional() -> None:
    ev = mem_ltm_forgotten(
        kept_count=5,
        dropped_count=1,
        dropped_digest=[{"section": "fact", "preview": "x", "reason": "user_forget"}],
        cause="forget",
    )
    assert "model" not in ev.payload
    assert "snapshot_id" not in ev.payload
    assert ev.payload["cause"] == "forget"


def test_mem_ltm_forgotten_rejects_bad_cause() -> None:
    with pytest.raises(ValueError):
        mem_ltm_forgotten(
            kept_count=0,
            dropped_count=0,
            dropped_digest=[],
            cause="bogus",
        )


def test_note_event_shapes() -> None:
    a = mem_note_added(id="note-1", title="t", tags=["x"])
    u = mem_note_updated(id="note-1")
    d = mem_note_deleted(id="note-1")
    assert a.kind == EventKind.MEM_NOTE_ADDED and a.payload["id"] == "note-1"
    assert u.kind == EventKind.MEM_NOTE_UPDATED
    assert d.kind == EventKind.MEM_NOTE_DELETED


def test_todo_event_shapes() -> None:
    a = mem_todo_added(id="todo-1", content="do x", due=None, tags=[])
    u = mem_todo_updated(id="todo-1", status="done", done_at="2026-05-22T15:00:00+08:00")
    d = mem_todo_deleted(id="todo-1")
    assert a.kind == EventKind.MEM_TODO_ADDED
    assert u.kind == EventKind.MEM_TODO_UPDATED
    assert u.payload["status"] == "done"
    assert d.kind == EventKind.MEM_TODO_DELETED


def test_remember_event_shape() -> None:
    ev = mem_remember(section="user", content_preview="prefers...", ts="2026-05-22")
    assert ev.kind == EventKind.MEM_REMEMBER
    assert ev.payload["src"] == "explicit"


def test_recall_invoked_omits_optional() -> None:
    ev = mem_recall_invoked(mode="by_keyword", hits=3, query="AAPL")
    assert ev.kind == EventKind.MEM_RECALL_INVOKED
    assert ev.payload["query"] == "AAPL"
    assert "date" not in ev.payload


def test_pause_resume() -> None:
    assert mem_paused().kind == EventKind.MEM_PAUSED
    assert mem_resumed().kind == EventKind.MEM_RESUMED
