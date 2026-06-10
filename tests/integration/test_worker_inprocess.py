"""In-process worker integration test.

Runs ``run_worker`` directly in a TaskGroup (no subprocess) so pytest-cov can
see the worker code paths. The subprocess test in ``test_worker_subprocess.py``
remains for "real OS process" coverage; this one drives the same logic so the
coverage report reflects it.

Connects via a temp Unix socket using the same ``IPCClient`` the CLI uses.
Shutdown is signaled by setting the ``anyio.Event`` directly.
"""

from __future__ import annotations

import functools
from pathlib import Path

import anyio
import pytest

from eonlet import paths
from eonlet.worker.ipc import IPCClient
from eonlet.worker.lifecycle import write_meta
from eonlet.worker.main import run_worker


def _write_fake_definition(home_root: Path, model: str = "fake-echo") -> Path:
    d = paths.agents_dir() / "echobot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"""apiVersion: eonlet/v1
kind: Agent
metadata:
  name: echobot
  description: in-process test
  version: 0.0.1
runtime:
  model: {model}
  max_steps_per_run: 5
tools:
  builtin: [sleep]
permissions:
  mode: yolo
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# echobot\nbe terse.\n", encoding="utf-8")
    return d


def _prep_eonlet(eid: str, defn: Path) -> None:
    paths.eonlet_dir(eid).mkdir(parents=True)
    paths.memory_dir(eid).mkdir()
    paths.workspace_dir(eid).mkdir()
    paths.logs_dir(eid).mkdir()
    write_meta(eid, type_="echobot", name="test", definition=defn, version="0.0.1")


def test_inproc_streams_back(isolated_home: Path) -> None:
    """End-to-end with the real IPC server + IPC client + agent loop + fake provider."""
    paths.ensure_home()
    defn = _write_fake_definition(isolated_home)
    eid = "echobot.alice"
    _prep_eonlet(eid, defn)

    deltas: list[str] = []
    final_content: list[str] = []

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            # Wait for the socket to appear before connecting.
            sock = str(paths.runtime_sock(eid))
            for _ in range(100):
                if Path(sock).exists():
                    break
                await anyio.sleep(0.02)
            else:
                pytest.fail(f"socket {sock} never appeared")

            async with IPCClient(sock) as client, anyio.create_task_group() as ctg:
                ctg.start_soon(client.run)
                await client.request("session.start", {"client_id": "test"})
                await client.request("message.send", {"content": "hi"})
                async for msg in client.notifications():
                    method = msg.get("method")
                    params = msg.get("params") or {}
                    if method == "token_delta":
                        deltas.append(params["delta_text"])
                    elif method == "event" and params.get("kind") == "assistant_message":
                        final_content.append(params["payload"]["content"])
                        ctg.cancel_scope.cancel()
                        break

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(10):
            await go()

    anyio.run(with_timeout)

    # The user turn is prefixed with a local-datetime tag (ADR-0006) before the
    # fake provider echoes it, so assert structure rather than the exact string.
    joined = "".join(deltas)
    assert joined.startswith("echo:")
    assert joined.endswith("hi")
    assert final_content == [joined]


def test_inproc_trigger_fire(isolated_home: Path) -> None:
    """Define an agent with a cron trigger, then manually fire it via IPC."""
    paths.ensure_home()
    # Definition with a daily trigger — the cron schedule won't actually fire in
    # the test's 10s budget, but ``trigger.fire`` skips the schedule.
    d = paths.agents_dir() / "scheduled"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        """apiVersion: eonlet/v1
kind: Agent
metadata:
  name: scheduled
  description: t
  version: 0.0.1
runtime:
  model: fake-echo
triggers:
  - id: morning
    kind: cron
    schedule: "0 8 * * *"
    timezone: UTC
    message: "do the morning thing"
    grace_period: 0s
tools:
  builtin: [sleep]
permissions:
  mode: yolo
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# scheduled bot\n", encoding="utf-8")
    eid = "scheduled.morn"
    _prep_eonlet(eid, d)

    fired_message: list[dict] = []

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    # Verify triggers.list reflects the configured trigger.
                    listing = await client.request("triggers.list", {})
                    assert any(t["id"] == "morning" for t in listing["triggers"])

                    # Fire it.
                    resp = await client.request("trigger.fire", {"trigger_id": "morning"})
                    assert resp["ok"]

                    # Listen for the resulting assistant_message.
                    async for msg in client.notifications():
                        if (
                            msg.get("method") == "event"
                            and msg["params"].get("kind") == "assistant_message"
                        ):
                            fired_message.append(msg["params"]["payload"])
                            ctg.cancel_scope.cancel()
                            break

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(10):
            await go()

    anyio.run(with_timeout)

    assert len(fired_message) == 1
    # The fake-echo provider echoes back the trigger envelope, which is fine —
    # we just want to confirm the trigger fired and produced a response.
    assert fired_message[0]["content"].startswith("echo:")


def test_inproc_scheduler_runs_task_to_done(isolated_home: Path) -> None:
    """With scheduling enabled, a task added via IPC is picked up and run to done."""
    paths.ensure_home()
    d = paths.agents_dir() / "taskbot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        """apiVersion: eonlet/v1
kind: Agent
metadata:
  name: taskbot
  description: scheduler test
  version: 0.0.1
runtime:
  model: fake-task-done
  max_steps_per_run: 5
tools:
  builtin: [task]
permissions:
  mode: yolo
tasks:
  scheduling:
    enabled: true
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# taskbot\nrun tasks.\n", encoding="utf-8")
    eid = "taskbot.alice"
    _prep_eonlet(eid, d)

    final: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    add = await client.request("task.add", {"content": "do the thing"})
                    assert add["ok"]
                    tid = add["id"]

                    # The scheduler picks it up within the poll interval and the
                    # fake provider completes it. Poll until it lands as done.
                    for _ in range(60):
                        listed = await client.request("task.list", {"status": "done"})
                        done = {t["id"]: t for t in listed["tasks"]}
                        if tid in done:
                            final.update(done[tid])
                            break
                        await anyio.sleep(0.2)
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(20):
            await go()

    anyio.run(with_timeout)

    assert final.get("status") == "done"
    assert final.get("result") == "completed"


def test_inproc_events_replay_filters_by_task_scope(isolated_home: Path) -> None:
    """``events.replay`` with a ``task_id`` returns only that task's conversation
    turns — the scoping the attach UI uses to show a single task's execution
    (ADR-0009). The task's own turns carry the scope; the chat-scope log carries
    the scope-neutral task lifecycle events."""
    paths.ensure_home()
    d = paths.agents_dir() / "scopebot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        """apiVersion: eonlet/v1
kind: Agent
metadata:
  name: scopebot
  description: scope replay test
  version: 0.0.1
runtime:
  model: fake-task-done
  max_steps_per_run: 5
tools:
  builtin: [task]
permissions:
  mode: yolo
tasks:
  scheduling:
    enabled: true
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# scopebot\nrun tasks.\n", encoding="utf-8")
    eid = "scopebot.alice"
    _prep_eonlet(eid, d)

    captured: dict[str, list] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    add = await client.request("task.add", {"content": "do the thing"})
                    tid = add["id"]

                    for _ in range(60):
                        listed = await client.request("task.list", {"status": "done"})
                        if tid in {t["id"] for t in listed["tasks"]}:
                            break
                        await anyio.sleep(0.2)

                    captured["scoped"] = await client.request("events.replay", {"task_id": tid})
                    captured["all"] = await client.request("events.replay", {"from": 0})
                    captured["tid"] = tid  # type: ignore[assignment]
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(20):
            await go()

    anyio.run(with_timeout)

    tid = captured["tid"]
    scoped = captured["scoped"]
    # Every scoped event belongs to the task and is a conversation turn.
    assert scoped, "task should have produced conversation events"
    assert all(e["task_id"] == tid for e in scoped)
    assert any(e["kind"] == "user_message" for e in scoped)

    # The unfiltered log carries scope-neutral lifecycle events (task_id None)
    # plus the task's scoped turns — i.e. the two are genuinely separable.
    all_events = captured["all"]
    assert any(e["kind"] == "task_created" and e["task_id"] is None for e in all_events)
    assert any(e["task_id"] == tid for e in all_events)


def test_inproc_task_tree_includes_history_and_depth(isolated_home: Path) -> None:
    """``task.tree`` returns the forest in DFS order with a per-node depth,
    including historical (done) tasks. A status filter keeps matching tasks plus
    their ancestors so the tree stays connected — the data behind /task tree."""
    paths.ensure_home()
    defn = _write_fake_definition(isolated_home)
    eid = "echobot.tree"
    _prep_eonlet(eid, defn)

    captured: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    root = await client.request("task.add", {"content": "root task"})
                    rid = root["id"]
                    child = await client.request(
                        "task.add", {"content": "child task", "parent_id": rid}
                    )
                    cid = child["id"]
                    # Mark the child done → it becomes "history".
                    done = await client.request("task.done", {"id": cid})
                    assert done["ok"]

                    captured["all"] = await client.request("task.tree", {"status": "all"})
                    captured["done"] = await client.request("task.tree", {"status": "done"})
                    captured["rid"] = rid  # type: ignore[assignment]
                    captured["cid"] = cid  # type: ignore[assignment]
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(15):
            await go()

    anyio.run(with_timeout)

    rid, cid = captured["rid"], captured["cid"]
    all_nodes = captured["all"]["tasks"]  # type: ignore[index]
    by_id = {n["id"]: n for n in all_nodes}
    assert by_id[rid]["depth"] == 0
    assert by_id[cid]["depth"] == 1
    # DFS order: a parent precedes its child.
    ids = [n["id"] for n in all_nodes]
    assert ids.index(rid) < ids.index(cid)

    # status=done keeps the done child AND its ancestor root (connected tree).
    done_ids = {n["id"] for n in captured["done"]["tasks"]}  # type: ignore[index]
    assert cid in done_ids
    assert rid in done_ids


def test_inproc_permissions_set_mode(isolated_home: Path) -> None:
    """``permissions.set_mode`` switches the live gate (yolo ↔ ask) and reports
    the mode back via ``session.start`` — the wiring behind ``/yolo``."""
    paths.ensure_home()
    defn = _write_fake_definition(isolated_home)  # permissions.mode: yolo
    eid = "echobot.mode"
    _prep_eonlet(eid, defn)

    captured: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    started = await client.request("session.start", {"client_id": "test"})
                    captured["start_mode"] = (started["state"] or {}).get("mode")

                    captured["to_ask"] = await client.request(
                        "permissions.set_mode", {"mode": "ask"}
                    )
                    captured["toggle"] = await client.request(
                        "permissions.set_mode", {"mode": "toggle"}
                    )
                    captured["bad"] = await client.request(
                        "permissions.set_mode", {"mode": "bogus"}
                    )
                    again = await client.request("session.start", {"client_id": "test"})
                    captured["end_mode"] = (again["state"] or {}).get("mode")
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(15):
            await go()

    anyio.run(with_timeout)

    assert captured["start_mode"] == "yolo"  # from the definition
    assert captured["to_ask"] == {"ok": True, "mode": "ask", "previous": "yolo"}
    assert captured["toggle"] == {"ok": True, "mode": "yolo", "previous": "ask"}
    assert captured["bad"]["ok"] is False  # invalid mode rejected
    assert captured["end_mode"] == "yolo"  # toggle landed back on yolo


def _write_scheduling_agent(
    name: str, model: str, *, tools: str = "[task]", preempt: str = "ask"
) -> Path:
    d = paths.agents_dir() / name
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"""apiVersion: eonlet/v1
kind: Agent
metadata:
  name: {name}
  description: scheduler test
  version: 0.0.1
runtime:
  model: {model}
  max_steps_per_run: 8
tools:
  builtin: {tools}
permissions:
  mode: yolo
tasks:
  scheduling:
    enabled: true
    preempt: "{preempt}"
    preempt_cooldown: 0s
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text(f"# {name}\nrun tasks.\n", encoding="utf-8")
    return d


def test_inproc_scheduler_decompose_then_synthesize(isolated_home: Path) -> None:
    """A parent task decomposes into two children; they run depth-first, then the
    parent synthesizes and completes — the full M2 tree flow end-to-end."""
    paths.ensure_home()
    d = _write_scheduling_agent("treebot", "fake-task-tree")
    eid = "treebot.alice"
    _prep_eonlet(eid, d)

    tasks_final: dict[str, dict] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    add = await client.request(
                        "task.add", {"content": "DECOMPOSE: build the thing"}
                    )
                    assert add["ok"]

                    # Wait until the whole forest (parent + 2 children) is done.
                    for _ in range(80):
                        listed = await client.request("task.list", {"status": "all"})
                        by_id = {t["id"]: t for t in listed["tasks"]}
                        done = [t for t in by_id.values() if t["status"] == "done"]
                        if len(by_id) == 3 and len(done) == 3:
                            tasks_final.update(by_id)
                            break
                        await anyio.sleep(0.2)
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(25):
            await go()

    anyio.run(with_timeout)

    assert len(tasks_final) == 3, tasks_final
    parents = [t for t in tasks_final.values() if t["parent_id"] is None]
    children = [t for t in tasks_final.values() if t["parent_id"] is not None]
    assert len(parents) == 1 and len(children) == 2
    assert parents[0]["result"] == "synthesized"
    assert all(c["parent_id"] == parents[0]["id"] for c in children)
    assert all(c["result"] == "leaf done" for c in children)


def test_inproc_scheduler_yield_checkpoints_and_suspends(isolated_home: Path) -> None:
    """A task whose run neither completes nor decomposes is checkpointed and
    suspended (M2 voluntary-yield path), with a non-empty resume brief."""
    paths.ensure_home()
    # fake-echo just emits text and ends the turn → no done, no children → yield.
    d = _write_scheduling_agent("yieldbot", "fake-echo")
    eid = "yieldbot.alice"
    _prep_eonlet(eid, d)

    final: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})
                    add = await client.request("task.add", {"content": "open-ended musing"})
                    tid = add["id"]
                    for _ in range(60):
                        listed = await client.request("task.list", {"status": "suspended"})
                        by_id = {t["id"]: t for t in listed["tasks"]}
                        if tid in by_id:
                            final.update(by_id[tid])
                            break
                        await anyio.sleep(0.2)
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(20):
            await go()

    anyio.run(with_timeout)

    assert final.get("status") == "suspended"
    assert final.get("progress_summary")  # non-empty resume brief


def test_inproc_scheduler_preempts_lower_priority(isolated_home: Path) -> None:
    """A running low-priority task is paused mid-run when a higher-priority task
    appears; the high-priority one completes, the low one is re-queued (M3)."""
    paths.ensure_home()
    d = _write_scheduling_agent(
        "preemptbot", "fake-task-busy", tools="[task, sleep]", preempt="auto_by_priority"
    )
    eid = "preemptbot.alice"
    _prep_eonlet(eid, d)

    result: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    # Low-priority long task starts running (it just sleeps).
                    await client.request("task.add", {"content": "BUSY: long job", "priority": 1})
                    await anyio.sleep(0.3)  # let it get into its run
                    # Higher-priority task arrives → should preempt.
                    hi = await client.request("task.add", {"content": "urgent", "priority": 9})
                    hi_id = hi["id"]

                    # Wait until the urgent task is done.
                    for _ in range(80):
                        listed = await client.request("task.list", {"status": "all"})
                        by_id = {t["id"]: t for t in listed["tasks"]}
                        if by_id.get(hi_id, {}).get("status") == "done":
                            result["hi"] = by_id[hi_id]
                            # Capture the preemption transition from the log.
                            evs = await client.request("events.replay", {"from": 0})
                            result["preempt_events"] = [
                                e
                                for e in evs
                                if e["kind"] == "task_transitioned"
                                and str(e["payload"].get("reason", "")).startswith("preempted:")
                            ]
                            break
                        await anyio.sleep(0.2)
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(25):
            await go()

    anyio.run(with_timeout)

    # The urgent task completed...
    assert result.get("hi", {}).get("status") == "done"  # type: ignore[union-attr]
    assert result["hi"]["result"] == "quick done"  # type: ignore[index]
    # ...because the busy task was preempted to make way for it.
    preempts = result.get("preempt_events") or []
    assert preempts, "expected a 'preempted:' transition for the busy task"
    # Both tasks were created via the control plane (origin="user"), so the
    # reason carries the contender origin + id (ADR-0008 §4).
    assert preempts[0]["payload"]["reason"] == f"preempted:user:{result['hi']['id']}"  # type: ignore[index]


def test_inproc_scheduled_task_hatches_and_runs(isolated_home: Path) -> None:
    """A message registers a recurring task template; firing it hatches a fresh
    task instance (origin=trigger) that the scheduler runs to done (M3)."""
    paths.ensure_home()
    d = _write_scheduling_agent("schedbot", "fake-schedule", preempt="off")
    eid = "schedbot.alice"
    _prep_eonlet(eid, d)

    out: dict[str, object] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    # A normal message → the agent registers a scheduled template.
                    await client.request("message.send", {"content": "set up my daily report"})
                    trig_id = None
                    for _ in range(50):
                        listing = await client.request("triggers.list", {})
                        trigs = listing.get("triggers") or []
                        if trigs:
                            trig_id = trigs[0]["id"]
                            break
                        await anyio.sleep(0.1)
                    assert trig_id, "scheduled trigger was never registered"

                    # Fire it manually → hatches a task instance that runs.
                    fired = await client.request("trigger.fire", {"trigger_id": trig_id})
                    assert fired["ok"]

                    for _ in range(60):
                        listed = await client.request("task.list", {"status": "all"})
                        done = [t for t in listed["tasks"] if t["status"] == "done"]
                        if done:
                            out["task"] = done[0]
                            break
                        await anyio.sleep(0.2)
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(25):
            await go()

    anyio.run(with_timeout)

    task = out.get("task")
    assert task, "scheduled task never hatched + ran"
    assert task["origin"] == "trigger"  # type: ignore[index]
    assert task["content"] == "daily report"  # type: ignore[index]
    assert task["result"] == "ran"  # type: ignore[index]


def test_inproc_task_suspend_resume_ipc(isolated_home: Path) -> None:
    """The M4 CLI ops (suspend/resume) over IPC move a task through its lifecycle.

    Scheduling is OFF so the task sits still instead of auto-running.
    """
    paths.ensure_home()
    d = paths.agents_dir() / "manualbot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        """apiVersion: eonlet/v1
kind: Agent
metadata:
  name: manualbot
  description: t
  version: 0.0.1
runtime:
  model: fake-echo
tools:
  builtin: [task]
permissions:
  mode: yolo
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# manualbot\n", encoding="utf-8")
    eid = "manualbot.alice"
    _prep_eonlet(eid, d)

    seen: dict[str, str] = {}

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})
                    add = await client.request("task.add", {"content": "sits still"})
                    tid = add["id"]

                    async def status_of() -> str:
                        listed = await client.request("task.list", {"status": "all"})
                        return {t["id"]: t for t in listed["tasks"]}[tid]["status"]

                    seen["added"] = await status_of()
                    assert (await client.request("task.suspend", {"id": tid}))["ok"]
                    seen["suspended"] = await status_of()
                    assert (await client.request("task.resume", {"id": tid}))["ok"]
                    seen["resumed"] = await status_of()
                    ctg.cancel_scope.cancel()

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(15):
            await go()

    anyio.run(with_timeout)

    assert seen["added"] == "pending"
    assert seen["suspended"] == "suspended"
    assert seen["resumed"] == "pending"
