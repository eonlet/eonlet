"""schedule → task-template bridge: registration + hatch (ADR-0007 M3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio

from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.task import TaskArgs, TaskTool
from eonlet.tools.protocol import ToolContext


class _FakeScheduler:
    def __init__(self) -> None:
        self.added: list[Any] = []

    async def add_dynamic(self, trig: Any, *, created_by: str = "agent") -> Any:
        self.added.append(trig)
        return trig


def _ctx(scheduler: Any) -> ToolContext:
    async def record(ev: Event) -> Event:
        return ev

    return ToolContext(
        eonlet_id="t.x",
        workspace=Path("/tmp"),
        memory_dir=Path("/tmp/memory"),
        skills={},
        env={},
        record_event=record,
        scheduler=scheduler,
    )


def test_scheduled_add_registers_template_trigger() -> None:
    sched = _FakeScheduler()
    ctx = _ctx(sched)
    out = anyio.run(
        lambda: TaskTool()(
            TaskArgs(
                action="add",
                content="daily digest",
                goal="email me a digest",
                priority=4,
                schedule="0 8 * * *",
                timezone="UTC",
            ),
            ctx,
        )
    )
    assert not out.is_error and "scheduled" in out.content
    assert len(sched.added) == 1
    trig = sched.added[0]
    assert trig.schedule == "0 8 * * *" and trig.timezone == "UTC"
    tmpl = trig.task_template
    assert tmpl["content"] == "daily digest"
    assert tmpl["goal"] == "email me a digest"
    assert tmpl["priority"] == 4


def test_scheduled_add_requires_timezone() -> None:
    ctx = _ctx(_FakeScheduler())
    out = anyio.run(
        lambda: TaskTool()(TaskArgs(action="add", content="x", schedule="0 8 * * *"), ctx)
    )
    assert out.is_error and "timezone" in out.content


def test_scheduled_add_needs_scheduler() -> None:
    ctx = _ctx(None)  # no scheduler in this context
    out = anyio.run(
        lambda: TaskTool()(
            TaskArgs(action="add", content="x", schedule="0 8 * * *", timezone="UTC"), ctx
        )
    )
    assert out.is_error and "scheduling unavailable" in out.content


def test_hatch_records_task_created_from_template() -> None:
    from eonlet.triggers.scheduler import TriggerItem
    from eonlet.worker.main import _hatch_task

    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        stamped = ev.model_copy(update={"id": len(captured) + 1})
        captured.append(stamped)
        return stamped

    runtime = type("RT", (), {"_record": staticmethod(record)})()
    item = TriggerItem(
        kind="task_hatch",
        content="",
        trigger_id="trig-1",
        task_template={"content": "daily report", "goal": "report", "priority": 2},
    )
    anyio.run(lambda: _hatch_task(runtime, item))
    assert len(captured) == 1
    ev = captured[0]
    assert ev.kind == EventKind.TASK_CREATED
    assert ev.payload["content"] == "daily report"
    assert ev.payload["origin"] == "trigger"
    assert ev.payload["priority"] == 2


def test_template_trigger_round_trips_through_store(tmp_path: Path) -> None:
    # A scheduled task's template must survive a worker restart (store reload).
    from eonlet.config import CronTrigger
    from eonlet.triggers.dynamic_store import (
        DynamicTriggerRecord,
        DynamicTriggerStore,
        mint_dynamic_id,
    )

    async def go() -> Any:
        store = DynamicTriggerStore(tmp_path)
        trig = CronTrigger.model_validate(
            {
                "id": mint_dynamic_id(),
                "schedule": "0 8 * * *",
                "timezone": "UTC",
                "message": "(scheduled task)",
                "task_template": {"content": "daily", "priority": 3},
            }
        )
        await store.add(
            DynamicTriggerRecord(trig=trig, created_at="2026-05-30", created_by="agent")
        )
        reloaded = DynamicTriggerStore(tmp_path)
        reloaded.load()
        return reloaded.all()

    recs = anyio.run(go)
    assert len(recs) == 1
    assert getattr(recs[0].trig, "task_template", None) == {"content": "daily", "priority": 3}


def test_hatch_skips_empty_template() -> None:
    from eonlet.triggers.scheduler import TriggerItem
    from eonlet.worker.main import _hatch_task

    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    runtime = type("RT", (), {"_record": staticmethod(record)})()
    item = TriggerItem(kind="task_hatch", content="", task_template={"content": "  "})
    anyio.run(lambda: _hatch_task(runtime, item))
    assert captured == []  # no content → nothing hatched
