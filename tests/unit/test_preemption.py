"""Preemption consent path — _approve_preempt (ADR-0007 M3)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anyio

from eonlet.tasks import Task
from eonlet.worker.main import _approve_preempt


def _tasks() -> tuple[Task, Task]:
    cur = Task(id="cur", content="current", priority=2, status="active")
    hot = Task(id="hot", content="urgent", priority=9)
    return cur, hot


class _Broker:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def ask(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.answer


def _rt(mode: str, broker: Any) -> Any:
    return SimpleNamespace(gate=SimpleNamespace(mode=mode), decision_broker=broker)


def test_auto_by_priority_approves_without_broker() -> None:
    cur, hot = _tasks()
    rt = _rt("ask", None)
    sched = SimpleNamespace(preempt="auto_by_priority")
    assert anyio.run(lambda: _approve_preempt(rt, cur, hot, sched)) is True


def test_yolo_mode_auto_approves() -> None:
    cur, hot = _tasks()
    rt = _rt("yolo", None)
    sched = SimpleNamespace(preempt="ask")  # ask, but yolo overrides
    assert anyio.run(lambda: _approve_preempt(rt, cur, hot, sched)) is True


def test_ask_switch_approves() -> None:
    cur, hot = _tasks()
    broker = _Broker("switch")
    rt = _rt("ask", broker)
    sched = SimpleNamespace(preempt="ask")
    assert anyio.run(lambda: _approve_preempt(rt, cur, hot, sched)) is True
    assert broker.calls and broker.calls[0]["kind"] == "task_preempt"
    assert broker.calls[0]["payload"] == {"pause": "cur", "start": "hot"}


def test_ask_keep_declines() -> None:
    cur, hot = _tasks()
    rt = _rt("ask", _Broker("keep"))
    sched = SimpleNamespace(preempt="ask")
    assert anyio.run(lambda: _approve_preempt(rt, cur, hot, sched)) is False


def test_ask_headless_declines() -> None:
    cur, hot = _tasks()
    rt = _rt("ask", None)  # no attached session / broker
    sched = SimpleNamespace(preempt="ask")
    assert anyio.run(lambda: _approve_preempt(rt, cur, hot, sched)) is False
