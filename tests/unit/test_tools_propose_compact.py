"""tools/builtin/memory.py — the propose_compact action (ADR-0006 M3).

The tool's job is orchestration: guards → proposal event → consent → tier-1 at
the agent's boundary. tier-1 itself is exercised in test_tier1.py, so here we
stub ``run_tier1`` and assert the orchestration + emitted events.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest

from eonlet.memory.config import MemoryConfig
from eonlet.memory.tier1 import CompactionOutcome
from eonlet.runtime.events import EventKind, assistant_message, user_message
from eonlet.runtime.store import EventStore
from eonlet.tools import builtin as _builtin  # noqa: F401 — register builtins
from eonlet.tools.builtin.memory import MemoryArgs, MemoryTool
from eonlet.tools.protocol import ToolContext


class _Broker:
    def __init__(self, answer: str = "approve", *, listener: bool = True) -> None:
        self.answer = answer
        self._listener = listener
        self.asked: list[tuple[str, str, dict[str, Any] | None]] = []

    def has_listener(self) -> bool:
        return self._listener

    async def ask(
        self,
        *,
        kind: str,
        prompt: str,
        options: list[str],
        payload: dict[str, Any] | None = None,
        decline: str = "deny",
    ) -> str:
        self.asked.append((kind, prompt, payload))
        return self.answer


def _seed(store: EventStore, n: int = 6) -> list[Any]:
    out = []
    for i in range(n):
        out.append(store.append(user_message(f"u{i}")))
        out.append(store.append(assistant_message(f"a{i}")))
    return out


def _runtime(
    tmp_path: Path,
    store: EventStore,
    *,
    mode: str = "ask",
    broker: _Broker | None = None,
    **cfg_overrides: Any,
) -> MagicMock:
    cfg = MemoryConfig.model_validate(
        {
            "episodic": {
                "working_memory_tokens": 100,
                "propose_floor_tokens": 64,
                "propose_min_interval_seconds": 0,
                "propose_min_turns": 0,
                **cfg_overrides,
            }
        }
    )
    rt = MagicMock()
    rt.store = store
    rt.memory_dir = tmp_path / "mem"
    rt.definition.config.memory = cfg
    rt.gate.mode = mode
    rt.decision_broker = broker
    rt.provider.model = cfg.compaction_model  # so _build_provider returns it as-is
    return rt


def _ctx(rt: MagicMock, recorder: list[Any]) -> ToolContext:
    mem = rt.memory_dir
    mem.mkdir(parents=True, exist_ok=True)

    async def record(ev: Any) -> Any:
        recorder.append(ev)
        return ev

    return ToolContext(
        eonlet_id="t.x",
        workspace=mem.parent / "ws",
        memory_dir=mem,
        skills={},
        env={},
        record_event=record,
        extra={"runtime": rt},
    )


def _run(args: MemoryArgs, ctx: ToolContext) -> Any:
    return anyio.run(MemoryTool().__call__, args, ctx)


def _kinds(recorder: list[Any]) -> list[EventKind]:
    return [e.kind for e in recorder]


def _stub_tier1(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    async def fake_run_tier1(**kwargs: Any) -> CompactionOutcome:
        calls.append(kwargs)
        return CompactionOutcome(
            ran=True,
            boundary_event_id=kwargs["boundary"],
            sections_added=1,
            tokens_before=200,
            tokens_after=20,
        )

    monkeypatch.setattr("eonlet.tools.builtin.memory.run_tier1", fake_run_tier1)


# ── happy paths ──────────────────────────────────────────────────────────────


def test_propose_approved_compacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    rt = _runtime(tmp_path, store, broker=_Broker("approve"))
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    calls: list[dict[str, Any]] = []
    _stub_tier1(monkeypatch, calls)

    boundary = events[4].id
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=boundary, reason="moved on"), ctx
    )

    assert not res.is_error
    assert rt.decision_broker.asked and rt.decision_broker.asked[0][0] == "compaction"
    assert _kinds(rec) == [EventKind.MEM_COMPACT_PROPOSED, EventKind.MEM_COMPACT_APPROVED]
    assert calls and calls[0]["boundary"] == boundary
    store.close()


def test_propose_declined_does_not_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    rt = _runtime(tmp_path, store, broker=_Broker("deny"))
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    calls: list[dict[str, Any]] = []
    _stub_tier1(monkeypatch, calls)

    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert not res.is_error
    assert "declined" in res.content
    assert _kinds(rec) == [EventKind.MEM_COMPACT_PROPOSED, EventKind.MEM_COMPACT_DECLINED]
    assert calls == []  # tier-1 never invoked
    store.close()


def test_propose_yolo_auto_approves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    # yolo + no broker at all: still auto-approves, no round-trip.
    rt = _runtime(tmp_path, store, mode="yolo", broker=None)
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    calls: list[dict[str, Any]] = []
    _stub_tier1(monkeypatch, calls)

    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert not res.is_error
    assert _kinds(rec) == [EventKind.MEM_COMPACT_PROPOSED, EventKind.MEM_COMPACT_APPROVED]
    approved = next(e for e in rec if e.kind == EventKind.MEM_COMPACT_APPROVED)
    assert approved.payload["rule"] == "yolo"
    assert calls and calls[0]["boundary"] == events[4].id
    store.close()


# ── refusals (no events recorded) ────────────────────────────────────────────


def test_propose_no_listener_is_skipped(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    rt = _runtime(tmp_path, store, mode="ask", broker=_Broker(listener=False))
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert "no interactive session" in res.content
    assert rec == []  # nothing recorded for a headless no-op
    store.close()


def test_propose_floor_guard_blocks(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    rt = _runtime(tmp_path, store, broker=_Broker("approve"), propose_floor_tokens=100_000)
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert "not now" in res.content and "floor" in res.content
    assert rec == []
    store.close()


def test_propose_invalid_boundary_errors(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "s.db")
    _seed(store)
    rt = _runtime(tmp_path, store, broker=_Broker("approve"))
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    latest = store.latest_id()
    res = _run(MemoryArgs(action="propose_compact", boundary_event_id=latest + 5, reason="x"), ctx)
    assert res.is_error and "boundary_event_id" in res.content
    assert rec == []
    store.close()


def test_propose_disabled_flag(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    rt = _runtime(tmp_path, store, broker=_Broker("approve"), propose_semantic=False)
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert "disabled" in res.content
    assert rec == []
    store.close()


def test_propose_interval_cooldown_blocks(tmp_path: Path) -> None:
    from eonlet.runtime.events import mem_compacted

    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    store.append(
        mem_compacted(
            snapshot_id=1,
            boundary_event_id=1,
            sections_added=1,
            tokens_before=1,
            tokens_after=1,
            model="x",
        )
    )  # a fresh compaction → wall-clock cooldown not yet elapsed
    rt = _runtime(
        tmp_path,
        store,
        broker=_Broker("approve"),
        propose_min_interval_seconds=3600,
    )
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert "not now" in res.content and "since the last compaction" in res.content
    assert rec == []
    store.close()


def test_propose_turns_cooldown_blocks(tmp_path: Path) -> None:
    from eonlet.runtime.events import mem_compacted

    store = EventStore(tmp_path / "s.db")
    events = _seed(store)
    store.append(
        mem_compacted(
            snapshot_id=1,
            boundary_event_id=1,
            sections_added=1,
            tokens_before=1,
            tokens_after=1,
            model="x",
        )
    )
    store.append(user_message("just one turn since"))  # < propose_min_turns
    rt = _runtime(
        tmp_path,
        store,
        broker=_Broker("approve"),
        propose_min_interval_seconds=0,
        propose_min_turns=5,
    )
    rec: list[Any] = []
    ctx = _ctx(rt, rec)
    res = _run(
        MemoryArgs(action="propose_compact", boundary_event_id=events[4].id, reason="x"), ctx
    )
    assert "not now" in res.content and "turn" in res.content
    assert rec == []
    store.close()
