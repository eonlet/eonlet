"""Tests for cli/status.py: models, helpers, collect() offline path, render()."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from eonlet import paths
from eonlet.cli.status import (
    ActivityEvent,
    ActivitySection,
    IdentitySection,
    MemorySection,
    MemoryTierInfo,
    ProcessSection,
    StatusReport,
    TokenSection,
    TriggerInfo,
    TriggerSection,
    _bar,
    _collect_activity,
    _collect_tokens,
    _collect_triggers,
    _collect_working,
    _event_preview,
    _human_duration,
    _is_compact_paused,
    _us_to_iso,
    collect,
    render,
)

# ── _human_duration ───────────────────────────────────────────────────────────


def test_human_duration_seconds() -> None:
    assert _human_duration(0) == "0s"
    assert _human_duration(45) == "45s"
    assert _human_duration(59) == "59s"


def test_human_duration_minutes() -> None:
    assert _human_duration(60) == "1m00s"
    assert _human_duration(90) == "1m30s"
    assert _human_duration(3599) == "59m59s"


def test_human_duration_hours() -> None:
    assert _human_duration(3600) == "1h00m"
    assert _human_duration(7384) == "2h03m"
    assert _human_duration(86399) == "23h59m"


def test_human_duration_days() -> None:
    assert _human_duration(86400) == "1d00h"
    assert _human_duration(90061) == "1d01h"


# ── _bar ─────────────────────────────────────────────────────────────────────


def test_bar_empty() -> None:
    result = _bar(0)
    assert "░" * 10 in result
    assert "green" in result


def test_bar_full() -> None:
    result = _bar(100)
    assert "█" * 10 in result
    assert "red" in result


def test_bar_yellow_range() -> None:
    result = _bar(75)
    assert "yellow" in result


def test_bar_red_range() -> None:
    result = _bar(95)
    assert "red" in result


def test_bar_over_100() -> None:
    result = _bar(150)
    assert "█" * 10 in result


# ── _event_preview ────────────────────────────────────────────────────────────


def test_event_preview_user_message_short() -> None:
    out = _event_preview("user_message", {"content": "hello"})
    assert out == '"hello"'


def test_event_preview_user_message_long() -> None:
    content = "x" * 80
    out = _event_preview("user_message", {"content": content})
    assert "…" in out
    assert len(out) < 80


def test_event_preview_assistant_message_with_tokens() -> None:
    out = _event_preview("assistant_message", {"tokens_in": 100, "tokens_out": 50})
    assert "in:100" in out
    assert "out:50" in out


def test_event_preview_assistant_message_with_content_only() -> None:
    out = _event_preview("assistant_message", {"content": "hi"})
    assert out == '"hi"'


def test_event_preview_tool_call() -> None:
    out = _event_preview("tool_call", {"tool_name": "bash"})
    assert out == "bash"


def test_event_preview_tool_result() -> None:
    out = _event_preview("tool_result", {"tool_name": "file_read"})
    assert out == "file_read"


def test_event_preview_tool_error() -> None:
    out = _event_preview("tool_error", {"tool_name": "bash"})
    assert out == "bash"


def test_event_preview_mem_compacted() -> None:
    out = _event_preview("mem_compacted", {"tokens_before": 1000, "tokens_after": 200})
    assert "1000" in out and "200" in out


def test_event_preview_mem_ltm_promoted() -> None:
    out = _event_preview("mem_ltm_promoted", {"additions": ["a", "b", "c"]})
    assert "+3" in out


def test_event_preview_trigger_fired() -> None:
    out = _event_preview("trigger_fired", {"trigger_id": "cron-1"})
    assert out == "cron-1"


def test_event_preview_session_started() -> None:
    out = _event_preview("session_started", {"mode": "interactive"})
    assert out == "interactive"


def test_event_preview_unknown() -> None:
    out = _event_preview("some_other_kind", {})
    assert out == ""


# ── _us_to_iso ────────────────────────────────────────────────────────────────


def test_us_to_iso_none() -> None:
    assert _us_to_iso(None) is None


def test_us_to_iso_valid() -> None:
    result = _us_to_iso(1_000_000_000_000)
    assert result is not None
    assert "T" in result


def test_us_to_iso_zero() -> None:
    result = _us_to_iso(0)
    assert result is not None


# ── collect() — offline, no worker ───────────────────────────────────────────


def test_collect_no_state(isolated_home: Path) -> None:
    """collect() works fine even when no state exists for an eonlet."""
    paths.eonlet_dir("test.agent").mkdir(parents=True, exist_ok=True)
    report = collect("test.agent")
    assert isinstance(report, StatusReport)
    assert report.identity.id == "test.agent"
    assert report.process.pid is None
    assert report.process.alive is False
    assert report.tokens.tokens_in_total == 0
    assert report.triggers.source == "unavailable"
    assert report.activity.events == []


def test_collect_identity_from_meta(isolated_home: Path) -> None:
    from eonlet.worker.lifecycle import write_meta

    eid = "assistant.alice"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    write_meta(
        eid,
        type_="assistant",
        name="alice",
        definition=Path("/fake/agent.yaml"),
        version="v1",
    )
    report = collect(eid)
    assert report.identity.name == "alice"
    assert report.identity.agent_type == "assistant"
    assert report.identity.spec_version == "eonlet/v1"


def test_collect_process_reads_pid_file(isolated_home: Path) -> None:
    import os

    eid = "test.bot"
    d = paths.eonlet_dir(eid)
    d.mkdir(parents=True, exist_ok=True)
    paths.pid_file(eid).write_text(str(os.getpid()), encoding="utf-8")
    report = collect(eid)
    assert report.process.pid == os.getpid()
    assert report.process.alive is True


def test_collect_process_reads_heartbeat(isolated_home: Path) -> None:
    import time

    eid = "test.bot2"
    d = paths.eonlet_dir(eid)
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    paths.heartbeat_file(eid).write_text(str(now), encoding="utf-8")
    report = collect(eid)
    assert report.process.heartbeat_age_s is not None
    assert report.process.heartbeat_age_s >= 0.0


def test_collect_memory_reads_stm(isolated_home: Path) -> None:
    eid = "test.mem"
    mem = paths.memory_dir(eid)
    mem.mkdir(parents=True, exist_ok=True)
    stm_content = (
        "## [2026-05-22T10:00:00+00:00 → 2026-05-22T11:00:00+00:00] topics: [work]\n"
        "some text\n"
        "---\n"
    )
    (mem / "short_term.md").write_text(stm_content, encoding="utf-8")
    report = collect(eid)
    assert report.memory.stm.estimated_tokens > 0


def test_collect_memory_reads_ltm(isolated_home: Path) -> None:
    eid = "test.ltm"
    mem = paths.memory_dir(eid)
    mem.mkdir(parents=True, exist_ok=True)
    (mem / "long_term.md").write_text("<!-- ltm -->\n- bullet one\n", encoding="utf-8")
    report = collect(eid)
    assert report.memory.ltm.estimated_tokens > 0


def test_collect_memory_reads_notes(isolated_home: Path) -> None:
    eid = "test.notes"
    mem = paths.memory_dir(eid)
    mem.mkdir(parents=True, exist_ok=True)
    note_content = "---\nid: n1\ntitle: My Note\ntags: []\ncreated_at: 2026-05-22T00:00:00+00:00\n---\n\nbody\n\n"
    (mem / "notes.md").write_text(note_content, encoding="utf-8")
    report = collect(eid)
    assert report.memory.notes.estimated_tokens > 0


def test_collect_memory_reads_todos(isolated_home: Path) -> None:
    import json

    eid = "test.todos"
    mem = paths.memory_dir(eid)
    mem.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"id": "t1", "content": "do this", "status": "pending"}),
        json.dumps({"id": "t2", "content": "done thing", "status": "done"}),
        json.dumps({"id": "t3", "content": "nope", "status": "cancelled"}),
    ]
    (mem / "todos.jsonl").write_text("\n".join(lines), encoding="utf-8")
    report = collect(eid)
    assert report.memory.todos_active == 1
    assert report.memory.todos_done == 1
    assert report.memory.todos_cancelled == 1


def test_collect_triggers_offline_no_db(isolated_home: Path) -> None:
    eid = "test.trig"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    report = collect(eid)
    assert report.triggers.source == "unavailable"
    assert report.triggers.triggers == []


# ── render() — smoke tests ────────────────────────────────────────────────────


def _make_report(eid: str = "test.bot") -> StatusReport:
    return StatusReport(
        identity=IdentitySection(
            id=eid,
            name="bot",
            agent_type="test",
            definition_path="/fake",
            created_at="2026-01-01",
            spec_version="v1",
        ),
        process=ProcessSection(
            status="running", pid=1234, uptime_s=120.0, heartbeat_age_s=5.0, alive=True
        ),
        tokens=TokenSection(
            tokens_in_total=1000,
            tokens_out_total=500,
            cost_usd_total=0.01,
            cost_usd_today=0.001,
            last_turn_tokens_in=100,
            last_turn_tokens_out=50,
            last_turn_model="claude-sonnet-4-6",
            turn_count=3,
        ),
        memory=MemorySection(
            enabled=True,
            compact_paused=False,
            working=MemoryTierInfo(estimated_tokens=500, budget_tokens=4000, count=5),
            stm=MemoryTierInfo(estimated_tokens=1000, budget_tokens=8000, count=2),
            ltm=MemoryTierInfo(estimated_tokens=200, budget_tokens=4000, count=10),
            notes=MemoryTierInfo(estimated_tokens=100, budget_tokens=2000, count=3),
            todos_active=2,
            todos_done=1,
            todos_cancelled=0,
        ),
        triggers=TriggerSection(
            triggers=[
                TriggerInfo(
                    id="cron-1",
                    schedule="0 9 * * *",
                    total_fires=5,
                    consecutive_failures=0,
                    next_fire_at="2026-06-01T09:00:00+00:00",
                )
            ],
            source="offline",
        ),
        activity=ActivitySection(
            events=[
                ActivityEvent(id=1, kind="user_message", age_s=10.0, preview='"hello"'),
                ActivityEvent(id=2, kind="assistant_message", age_s=8.0, preview="in:100  out:50"),
            ]
        ),
    )


def test_render_smoke() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    render(report, console)
    out = buf.getvalue()
    assert "test.bot" in out
    assert "PROCESS" in out
    assert "TOKENS" in out
    assert "MEMORY" in out
    assert "TRIGGERS" in out
    assert "RECENT ACTIVITY" in out


def test_render_dead_process() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.process.status = "dead"
    report.process.alive = False
    render(report, console)
    out = buf.getvalue()
    assert "dead" in out


def test_render_memory_disabled() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.memory.enabled = False
    render(report, console)
    out = buf.getvalue()
    assert "off" in out


def test_render_compact_paused() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.memory.compact_paused = True
    render(report, console)
    out = buf.getvalue()
    assert "paused" in out


def test_render_no_triggers() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.triggers.triggers = []
    render(report, console)
    out = buf.getvalue()
    assert "none" in out


def test_render_no_activity() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.activity.events = []
    render(report, console)
    out = buf.getvalue()
    assert "no events" in out


def test_render_trigger_with_failures() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.triggers.triggers[0].consecutive_failures = 3
    render(report, console)
    out = buf.getvalue()
    assert "failures: 3" in out


def test_render_last_turn_no_model() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.tokens.last_turn_model = None
    render(report, console)
    out = buf.getvalue()
    assert "last turn" in out


def test_render_todos_done_and_cancelled() -> None:
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=True)
    report = _make_report()
    report.memory.todos_cancelled = 2
    render(report, console)
    out = buf.getvalue()
    assert "cancelled" in out


def test_render_status_report_json_serializable() -> None:
    report = _make_report()
    data = report.model_dump()
    assert data["identity"]["id"] == "test.bot"
    assert data["process"]["alive"] is True
    assert data["memory"]["enabled"] is True


# ── Database-backed tests ─────────────────────────────────────────────────────


def _make_db(eid: str) -> Path:
    """Create a state.db with some assistant_message events for eid."""
    from eonlet.runtime.events import assistant_message, user_message
    from eonlet.runtime.store import EventStore

    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.append(user_message("hello"))
    store.append(
        assistant_message(
            "hi there",
            tokens_in=100,
            tokens_out=50,
            cost_usd=0.001,
        )
    )
    store.append(user_message("second"))
    store.append(
        assistant_message(
            "second reply",
            tokens_in=200,
            tokens_out=80,
            cost_usd=0.002,
        )
    )
    store.close()
    return db


def test_collect_tokens_with_db(isolated_home: Path) -> None:
    eid = "test.tokdb"
    _make_db(eid)
    report = collect(eid)
    assert report.tokens.tokens_in_total == 300
    assert report.tokens.tokens_out_total == 130
    assert abs(report.tokens.cost_usd_total - 0.003) < 1e-6
    assert report.tokens.turn_count == 2
    assert report.tokens.last_turn_tokens_in == 200
    assert report.tokens.last_turn_tokens_out == 80


def test_collect_working_with_db(isolated_home: Path) -> None:
    eid = "test.wrkdb"
    _make_db(eid)
    report = collect(eid)
    assert report.memory.working.count > 0
    assert report.memory.working.estimated_tokens > 0


def test_is_compact_paused_false_when_no_db(isolated_home: Path) -> None:
    eid = "test.nopause"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    assert _is_compact_paused(eid) is False


def test_is_compact_paused_true(isolated_home: Path) -> None:
    from eonlet.runtime.events import mem_paused
    from eonlet.runtime.store import EventStore

    eid = "test.paused"
    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.append(mem_paused())
    store.close()
    assert _is_compact_paused(eid) is True


def test_is_compact_paused_false_after_resume(isolated_home: Path) -> None:
    from eonlet.runtime.events import mem_paused, mem_resumed
    from eonlet.runtime.store import EventStore

    eid = "test.resumed"
    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.append(mem_paused())
    store.append(mem_resumed())
    store.close()
    assert _is_compact_paused(eid) is False


def test_collect_triggers_offline_with_db(isolated_home: Path) -> None:
    from eonlet.runtime.store import EventStore

    eid = "test.trigdb"
    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.update_trigger_state("cron-daily", last_fired_at=1_000_000, total_fires=5)
    store.close()
    report = collect(eid)
    assert report.triggers.source == "offline"
    assert len(report.triggers.triggers) == 1
    assert report.triggers.triggers[0].id == "cron-daily"
    assert report.triggers.triggers[0].total_fires == 5


def test_collect_activity_with_db(isolated_home: Path) -> None:
    eid = "test.actdb"
    _make_db(eid)
    report = collect(eid)
    assert len(report.activity.events) > 0
    kinds = [ev.kind for ev in report.activity.events]
    assert any("message" in k for k in kinds)


def test_collect_tokens_empty_db_returns_defaults(isolated_home: Path) -> None:
    from eonlet.runtime.store import EventStore

    eid = "test.emptytok"
    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.close()
    section = _collect_tokens(eid)
    assert section.tokens_in_total == 0
    assert section.tokens_out_total == 0
    assert section.turn_count == 0


def test_collect_activity_no_db(isolated_home: Path) -> None:
    eid = "test.noactdb"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    section = _collect_activity(eid)
    assert section.events == []


def test_collect_triggers_no_db(isolated_home: Path) -> None:
    eid = "test.notrigdb"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    section = _collect_triggers(eid)
    assert section.source == "unavailable"


def test_collect_process_bad_heartbeat(isolated_home: Path) -> None:
    eid = "test.badhb"
    d = paths.eonlet_dir(eid)
    d.mkdir(parents=True, exist_ok=True)
    # Write a non-float string — triggers ValueError path
    paths.heartbeat_file(eid).write_text("not-a-float", encoding="utf-8")
    report = collect(eid)
    # Should still work, heartbeat_age_s just stays None
    assert report.process.heartbeat_age_s is None


def test_collect_memory_todos_unknown_status(isolated_home: Path) -> None:
    import json

    eid = "test.unkstatus"
    mem = paths.memory_dir(eid)
    mem.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"id": "t1", "content": "task", "status": "unknown_status"}),
    ]
    (mem / "todos.jsonl").write_text("\n".join(lines), encoding="utf-8")
    report = collect(eid)
    # Unknown status should not increment any counter
    assert report.memory.todos_active == 0
    assert report.memory.todos_done == 0
    assert report.memory.todos_cancelled == 0


def test_us_to_iso_extreme_value() -> None:
    # Very large microseconds — might overflow or fail
    result = _us_to_iso(10**20)
    # Should return None on failure, not raise
    # (may or may not be valid depending on platform)
    assert result is None or isinstance(result, str)


def test_event_preview_assistant_message_no_tokens_no_content() -> None:
    out = _event_preview("assistant_message", {})
    assert out == ""


def test_collect_working_no_db(isolated_home: Path) -> None:
    eid = "test.wrknodf"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    section = _collect_working(
        eid, __import__("eonlet.memory.config", fromlist=["MemoryConfig"]).MemoryConfig()
    )
    assert section.estimated_tokens == 0
    assert section.budget_tokens > 0
