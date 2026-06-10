"""Context injection: preamble assembly + recent-window slicing (MEMORY_SPEC §3)."""

from __future__ import annotations

import re
from pathlib import Path

import anyio

from eonlet.memory.config import MemoryConfig
from eonlet.memory.injection import (
    build_memory_preamble,
    build_tasks_block,
    format_turn_timestamp,
    prefix_user_timestamp,
    select_recent_window,
    working_window_token_estimate,
)
from eonlet.runtime.events import (
    Event,
    EventKind,
    assistant_message,
    task_created,
    task_transitioned,
    tool_call,
    tool_result,
    user_message,
)
from eonlet.tasks import TaskForest, fold_tasks
from eonlet.tasks.config import TasksConfig


def _evt(ev: Event, *, id_: int) -> Event:
    return ev.model_copy(update={"id": id_})


# ── per-turn timestamp rendering (ADR-0006) ─────────────────────────────────

_STAMP_RE = re.compile(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{2}:\d{2}\]$")


def test_format_turn_timestamp_shape() -> None:
    # 2026-05-30T12:00:00Z = 1_780_142_400 s.
    tag = format_turn_timestamp(1_780_142_400 * 1_000_000)
    assert _STAMP_RE.match(tag), tag


def test_prefix_user_timestamp_adds_tag() -> None:
    out = prefix_user_timestamp("hello", 1_780_142_400 * 1_000_000)
    assert out.endswith(" hello")
    assert _STAMP_RE.match(out.removesuffix(" hello"))


def test_prefix_user_timestamp_none_passthrough() -> None:
    assert prefix_user_timestamp("hello", None) == "hello"


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


def test_preamble_injects_knowledge_index(tmp_path: Path) -> None:
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "index.md").write_text("# Knowledge Index\n\n- [Testing](rules/testing.md) — DB rule\n")
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "<knowledge_index>" in out
    assert "rules/testing.md" in out
    assert "DB rule" in out


def test_preamble_omits_knowledge_index_when_disabled(tmp_path: Path) -> None:
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "index.md").write_text("# Knowledge Index\n\n- [A](a.md) — x\n")
    cfg = MemoryConfig()
    cfg.knowledge.inject_index = False
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "<knowledge_index>" not in out


def test_preamble_knowledge_index_tree_fallback(tmp_path: Path) -> None:
    # Files present, no index.md → injection regenerates a map from the tree.
    kdir = tmp_path / "knowledge"
    (kdir / "rules").mkdir(parents=True)
    (kdir / "rules" / "testing.md").write_text("body")
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "<knowledge_index>" in out
    assert "rules/testing.md" in out


def test_preamble_does_not_include_tasks(tmp_path: Path) -> None:
    # Tasks live OUTSIDE <memory> now (ADR-0005) — the preamble must not carry them.
    (tmp_path / "long_term.md").write_text("LTM-MARKER")
    cfg = MemoryConfig()
    out = anyio.run(lambda: build_memory_preamble(tmp_path, cfg))
    assert "<tasks>" not in out
    assert "<todos>" not in out


# ── tasks block (sibling of <memory>) ──────────────────────────────────────


def _forest(*events: Event) -> TaskForest:
    stamped = [e.model_copy(update={"id": i + 1}) for i, e in enumerate(events)]
    return fold_tasks(stamped)


def test_tasks_block_includes_pending_only(tmp_path: Path) -> None:
    forest = _forest(
        task_created(id="t1", content="do thing"),
        task_created(id="t2", content="archived"),
        task_transitioned(id="t2", from_state="pending", to_state="done"),
    )
    out = build_tasks_block(forest, TasksConfig())
    assert out.startswith("<tasks>")
    assert "do thing" in out
    assert "archived" not in out  # done items NOT injected


def test_tasks_block_empty_when_no_pending(tmp_path: Path) -> None:
    assert build_tasks_block(TaskForest(), TasksConfig()) == ""


def test_tasks_block_omitted_when_inject_pending_false(tmp_path: Path) -> None:
    forest = _forest(task_created(id="t1", content="HIDDEN_TASK"))
    assert build_tasks_block(forest, TasksConfig(inject_pending=False)) == ""


def test_tasks_block_surfaces_suspended(tmp_path: Path) -> None:
    # Suspended tasks only resume explicitly — hiding them would make yielded
    # work silently vanish (design-review P3).
    forest = _forest(
        task_created(id="t1", content="active work"),
        task_created(id="t2", content="paused work"),
        task_transitioned(id="t2", from_state="pending", to_state="suspended", reason="yielded"),
    )
    out = build_tasks_block(forest, TasksConfig())
    assert "active work" in out
    assert "paused work" in out
    assert "suspended" in out and "resume" in out


def test_tasks_block_suspended_only(tmp_path: Path) -> None:
    forest = _forest(
        task_created(id="t1", content="paused work"),
        task_transitioned(id="t1", from_state="pending", to_state="suspended", reason="yielded"),
    )
    out = build_tasks_block(forest, TasksConfig())
    assert out.startswith("<tasks>") and "paused work" in out


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
        {"episodic": {"working_memory_tokens": 100, "keep_recent_messages_min": 3}}
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
        {"episodic": {"working_memory_tokens": 64, "keep_recent_messages_min": 1}}
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
