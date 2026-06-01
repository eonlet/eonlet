"""New memory event kinds and constructors (MEMORY_SPEC §7)."""

from __future__ import annotations

import pytest

from eonlet.runtime.events import (
    EventKind,
    kb_deleted,
    kb_moved,
    kb_written,
    mem_compact_approved,
    mem_compact_declined,
    mem_compact_proposed,
    mem_compacted,
    mem_ltm_forgotten,
    mem_ltm_promoted,
    mem_paused,
    mem_recall_invoked,
    mem_resumed,
    session_ended,
    session_started,
    task_checkpointed,
    task_created,
    task_deleted,
    task_transitioned,
    task_updated,
)


def test_session_helpers_carry_reason() -> None:
    started = session_started(reason="compact")
    ended = session_ended(reason="compact")
    assert started.kind is EventKind.SESSION_STARTED
    assert ended.kind is EventKind.SESSION_ENDED
    assert started.payload == {"reason": "compact"}
    assert ended.payload == {"reason": "compact"}
    # Omitted reason → empty payload.
    assert session_started().payload == {}


def test_all_memory_kinds_exist() -> None:
    expected = {
        "mem_compacted",
        "mem_ltm_promoted",
        "mem_ltm_forgotten",
        "mem_recall_invoked",
        "mem_paused",
        "mem_resumed",
        "kb_written",
        "kb_deleted",
        "kb_moved",
        "task_created",
        "task_updated",
        "task_transitioned",
        "task_checkpointed",
        "task_deleted",
        "mem_compact_proposed",
        "mem_compact_approved",
        "mem_compact_declined",
    }
    actual = {k.value for k in EventKind}
    missing = expected - actual
    assert not missing, f"missing event kinds: {missing}"


def test_eventkind_count_is_41() -> None:
    # ADR-0007 reworked the task family: TASK_ADDED→TASK_CREATED (rename) plus
    # two new kinds (TASK_TRANSITIONED / TASK_CHECKPOINTED): 39 → 41.
    assert len(list(EventKind)) == 41


def test_compact_proposal_helpers() -> None:
    proposed = mem_compact_proposed(boundary_event_id=12, reason="moved on", working_tokens=6000)
    assert proposed.kind is EventKind.MEM_COMPACT_PROPOSED
    assert proposed.payload == {
        "boundary_event_id": 12,
        "reason": "moved on",
        "working_tokens": 6000,
    }
    approved = mem_compact_approved(boundary_event_id=12, rule="yolo")
    assert approved.kind is EventKind.MEM_COMPACT_APPROVED
    assert approved.payload == {"boundary_event_id": 12, "rule": "yolo"}
    declined = mem_compact_declined(boundary_event_id=12)
    assert declined.kind is EventKind.MEM_COMPACT_DECLINED
    assert declined.payload == {"boundary_event_id": 12, "rule": "user"}


def test_retired_event_kinds_absent() -> None:
    # ADR-0005 retired these; guard against accidental reintroduction.
    actual = {k.value for k in EventKind}
    assert "mem_remember" not in actual
    assert "mem_note_added" not in actual
    assert "mem_todo_added" not in actual  # renamed to task_created
    assert "mem_todo_updated" not in actual
    assert "mem_todo_deleted" not in actual
    assert "task_added" not in actual  # ADR-0007 renamed task_added → task_created


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
        additions=[{"section": "episodic", "content": "2026-05-22: shipped web tools"}],
        kept_section_count=3,
        model="fake-echo",
    )
    assert ev.kind == EventKind.MEM_LTM_PROMOTED
    assert ev.payload["additions"][0]["section"] == "episodic"
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


def test_mem_ltm_forgotten_omits_optional_when_absent() -> None:
    ev = mem_ltm_forgotten(
        kept_count=5,
        dropped_count=1,
        dropped_digest=[{"section": "episodic", "preview": "x", "reason": "stale"}],
    )
    assert "model" not in ev.payload
    assert "snapshot_id" not in ev.payload
    assert ev.payload["cause"] == "tier3"  # the only valid cause now


def test_mem_ltm_forgotten_rejects_retired_forget_cause() -> None:
    # ADR-0005 retired the `forget` tool and its cause variant.
    with pytest.raises(ValueError):
        mem_ltm_forgotten(kept_count=0, dropped_count=0, dropped_digest=[], cause="forget")


def test_kb_event_shapes() -> None:
    w = kb_written(path="rules/testing.md", size=42, action="write")
    e = kb_written(path="rules/testing.md", size=10, action="edit")
    d = kb_deleted(path="rules/testing.md")
    m = kb_moved(src="a.md", dst="b.md")
    assert w.kind == EventKind.KB_WRITTEN and w.payload["action"] == "write"
    assert e.payload["action"] == "edit"
    assert d.kind == EventKind.KB_DELETED and d.payload["path"] == "rules/testing.md"
    assert (
        m.kind == EventKind.KB_MOVED and m.payload["src"] == "a.md" and m.payload["dst"] == "b.md"
    )


def test_task_event_shapes() -> None:
    c = task_created(id="task-1", content="do x", goal="ship it", priority=5, parent_id="root")
    u = task_updated(id="task-1", content="do y", priority=8)
    tr = task_transitioned(id="task-1", from_state="pending", to_state="done", reason="tool:done")
    cp = task_checkpointed(id="task-1", progress_summary="halfway")
    d = task_deleted(id="task-1")
    assert c.kind == EventKind.TASK_CREATED
    assert c.payload["goal"] == "ship it" and c.payload["parent_id"] == "root"
    assert c.payload["priority"] == 5
    assert u.kind == EventKind.TASK_UPDATED
    assert u.payload == {"id": "task-1", "content": "do y", "priority": 8}  # only set fields
    assert tr.kind == EventKind.TASK_TRANSITIONED
    assert tr.payload["from_state"] == "pending" and tr.payload["to_state"] == "done"
    assert cp.kind == EventKind.TASK_CHECKPOINTED and cp.payload["progress_summary"] == "halfway"
    assert d.kind == EventKind.TASK_DELETED


def test_recall_invoked_omits_optional() -> None:
    ev = mem_recall_invoked(mode="by_keyword", hits=3, query="AAPL")
    assert ev.kind == EventKind.MEM_RECALL_INVOKED
    assert ev.payload["query"] == "AAPL"
    assert "date" not in ev.payload


def test_pause_resume() -> None:
    assert mem_paused().kind == EventKind.MEM_PAUSED
    assert mem_resumed().kind == EventKind.MEM_RESUMED
