"""Worker entrypoint: spawned by ``eonlet create`` for each eonlet instance.

Per SPEC §7.2 — anyio TaskGroup over four concerns:

- ``serve_ipc``       — Unix socket JSON-RPC server
- ``heartbeat_loop``  — write the heartbeat file every 10s
- ``trigger_scheduler`` — fires cron triggers (v0.0.2+)
- ``main_loop``       — consume the trigger queue, dispatch to AgentRuntime

The IPC handler and the cron scheduler are both producers; ``main_loop`` is
the single consumer. This is why a long LLM turn doesn't deadlock IPC: the
socket task pushes to the queue and returns immediately.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC
from pathlib import Path
from typing import Any

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .. import paths
from ..errors import EonletError
from ..runtime.agent import AgentRuntime
from ..runtime.definition import import_custom_tool_module, load_definition
from ..runtime.events import Event
from ..runtime.store import EventStore
from ..tools import builtin as _builtin  # noqa: F401 — side-effect: register builtin tools
from ..triggers.scheduler import (
    CronScheduler,
    TriggerItem,
    build_trigger_message,
)
from .ipc import IPCServer
from .lifecycle import (
    cleanup,
    read_meta,
    write_heartbeat,
    write_pid,
    write_status,
)

HEARTBEAT_INTERVAL_S = 10
QUEUE_CAPACITY = 16  # TRIGGER_SPEC §9
log = logging.getLogger("eonlet.worker")


# ── main entrypoint ──────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(prog="eonlet-worker")
    parser.add_argument("eonlet_id", help="<type>.<name>")
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("EONLET_LOG", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(_log_file(args.eonlet_id), encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    try:
        anyio.run(_worker_main, args.eonlet_id)
    except EonletError as e:
        log.error("worker fatal: %s", e)
        write_status(args.eonlet_id, "dead")
        sys.exit(2)


def _log_file(eonlet_id: str) -> str:
    p = paths.current_log(eonlet_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


async def _worker_main(eonlet_id: str) -> None:
    """Process-mode entry point. Installs a signal watcher then delegates."""
    shutdown = anyio.Event()
    await run_worker(eonlet_id, shutdown, install_signal_watcher=True)


async def run_worker(
    eonlet_id: str,
    shutdown: anyio.Event,
    *,
    install_signal_watcher: bool = True,
) -> None:
    """The worker's core loop, factored out for in-process testing.

    The ``install_signal_watcher`` flag should be False in tests — pytest
    owns the signal handlers and ``anyio.open_signal_receiver`` would race
    with them. Tests drive ``shutdown`` directly.
    """
    meta = read_meta(eonlet_id)
    if meta is None:
        raise EonletError(f"meta.json missing for {eonlet_id}; was this eonlet created?")
    defn_path = Path(meta["definition_path"])
    definition = load_definition(defn_path)

    for tp in definition.custom_tool_paths:
        import_custom_tool_module(tp)

    workspace = paths.workspace_dir(eonlet_id)
    memory = paths.memory_dir(eonlet_id)
    workspace.mkdir(parents=True, exist_ok=True)
    memory.mkdir(parents=True, exist_ok=True)
    for nf in definition.config.memory.notes_files:
        (memory / nf).touch(exist_ok=True)

    store = EventStore(paths.state_db(eonlet_id))
    runtime = AgentRuntime.restore(
        eonlet_id=eonlet_id,
        definition=definition,
        store=store,
        workspace=workspace,
        memory_dir=memory,
    )

    write_pid(eonlet_id)
    write_status(eonlet_id, "running")
    write_heartbeat(eonlet_id)

    # Single-consumer queue: IPC + scheduler push, main_loop drains.
    send, recv = anyio.create_memory_object_stream[TriggerItem](QUEUE_CAPACITY)
    scheduler = CronScheduler(definition.config.triggers, store, send, eonlet_id)

    server = IPCServer(
        str(paths.runtime_sock(eonlet_id)),
        _make_handler(runtime, eonlet_id, send, scheduler),
    )
    runtime.event_listener = _make_event_broadcaster(server)
    runtime.on_delta = _make_delta_broadcaster(server)

    def _on_signal() -> None:
        log.info("worker: signal received, shutting down")
        shutdown.set()

    try:
        await scheduler.catch_up_missed()
        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            tg.start_soon(_heartbeat_loop, eonlet_id, shutdown)
            if install_signal_watcher:
                tg.start_soon(_signal_watcher, shutdown, _on_signal)
            tg.start_soon(_main_loop, runtime, recv, scheduler, shutdown)
            tg.start_soon(scheduler.run)
            await shutdown.wait()
            tg.cancel_scope.cancel()
    finally:
        write_status(eonlet_id, "dead")
        cleanup(eonlet_id)
        store.close()


# ── tasks ────────────────────────────────────────────────────────────────────


async def _heartbeat_loop(eonlet_id: str, shutdown: anyio.Event) -> None:
    while not shutdown.is_set():
        write_heartbeat(eonlet_id)
        with anyio.move_on_after(HEARTBEAT_INTERVAL_S):
            await shutdown.wait()


async def _signal_watcher(shutdown: anyio.Event, on_signal: Callable[[], None]) -> None:
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
        async for _ in signals:
            on_signal()
            return


async def _main_loop(
    runtime: AgentRuntime,
    recv: MemoryObjectReceiveStream[TriggerItem],
    scheduler: CronScheduler,
    shutdown: anyio.Event,
) -> None:
    """Drain trigger items one at a time, dispatch to the agent runtime.

    A cron-fired item carries trigger_id; we report success/failure back to
    the scheduler so it can update consecutive_failures + backoff counters.
    """
    async with recv:
        while not shutdown.is_set():
            try:
                item = await recv.receive()
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return
            ok = True
            try:
                async for _ in runtime.handle_user_message(item.content):
                    pass
            except Exception:
                log.exception("main_loop: run failed")
                ok = False
            if item.kind == "cron" and item.trigger_id:
                scheduler.record_outcome(item.trigger_id, success=ok)


# ── IPC handler ──────────────────────────────────────────────────────────────


def _make_handler(
    runtime: AgentRuntime,
    eonlet_id: str,
    send: MemoryObjectSendStream[TriggerItem],
    scheduler: CronScheduler,
) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """Build the JSON-RPC method dispatcher. Methods per SPEC §8.1."""

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method == "session.start":
            runtime.gate.session_attached = True
            session = params.get("_session")
            return {
                "session_id": session.id if session is not None else None,
                "state": {
                    "eonlet_id": eonlet_id,
                    "message_count": len(runtime.state.messages),
                    "model": runtime.provider.model,
                },
            }
        if method == "session.end":
            return {"ok": True}
        if method == "message.send":
            content = params.get("content", "")
            try:
                send.send_nowait(TriggerItem(kind="interactive", content=content))
            except anyio.WouldBlock:
                return {"ok": False, "error": "queue full"}
            return {"ok": True}
        if method == "trigger.fire":
            tid = params.get("trigger_id")
            if not tid:
                return {"ok": False, "error": "missing trigger_id"}
            trig = scheduler.get(tid)
            if trig is None:
                return {"ok": False, "error": f"no such trigger: {tid}"}
            override = params.get("message")
            state = runtime.store.get_trigger_state(tid)
            from datetime import datetime
            from zoneinfo import ZoneInfo

            content = build_trigger_message(
                trig,
                tz=ZoneInfo(trig.timezone),
                fired_at=datetime.now(UTC),
                last_success_at=state["last_success_at"],
                eonlet_id=eonlet_id,
                catchup=False,
                override_message=override,
            )
            try:
                send.send_nowait(TriggerItem(kind="cron", content=content, trigger_id=tid))
            except anyio.WouldBlock:
                return {"ok": False, "error": "queue full"}
            return {"ok": True}
        if method == "state.get":
            return {
                "messages": [m.__dict__ for m in runtime.state.messages[-20:]],
                "message_count": len(runtime.state.messages),
            }
        if method == "events.replay":
            since = int(params.get("from") or 0)
            events = runtime.store.read(since=since, limit=200)
            return [_event_to_dict(e) for e in events]
        if method == "triggers.list":
            return {"triggers": scheduler.serializable()}
        return {"error": f"unknown method: {method}"}

    return handle


def _make_event_broadcaster(server: IPCServer) -> Callable[[Event], Awaitable[None]]:
    async def listener(event: Event) -> None:
        await server.broadcast("event", _event_to_dict(event))

    return listener


def _make_delta_broadcaster(server: IPCServer) -> Callable[[str], Awaitable[None]]:
    """Token delta hook — pushed as a JSON-RPC ``token_delta`` notification
    (SPEC §8.1) without going through the event store.
    """

    async def listener(text: str) -> None:
        await server.broadcast("token_delta", {"delta_text": text})

    return listener


def _event_to_dict(event: Event) -> dict[str, Any]:
    d = event.model_dump()
    d["kind"] = str(event.kind)
    return d


if __name__ == "__main__":
    main()
