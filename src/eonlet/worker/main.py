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
from ..errors import ConfigError, EonletError
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
from ..web import HTTPFetcher
from .decisions import DecisionBroker
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
    # Memory files (short_term.md / long_term.md / notes.md / todos.jsonl)
    # are created lazily on first write — see MEMORY_SPEC §2.

    from ..config import load_global_config

    global_cfg = load_global_config()

    store = EventStore(paths.state_db(eonlet_id))
    # Recall index — derived state. If it lags behind the event log (missing
    # index, schema mismatch, crash mid-write), catch up by replaying any
    # events with id > the highest indexed id (M-I1 in MEMORY_SPEC §12).
    from ..memory.recall import RecallIndex

    recall_index = RecallIndex(memory)
    catchup_from = recall_index.latest_indexed_id()
    if catchup_from < store.latest_id():
        for ev in store.read(since=catchup_from):
            recall_index.index_event(ev)

    runtime = AgentRuntime.restore(
        eonlet_id=eonlet_id,
        definition=definition,
        store=store,
        workspace=workspace,
        memory_dir=memory,
        global_cfg=global_cfg,
    )
    runtime.recall_index = recall_index

    http_fetcher: HTTPFetcher | None = None

    write_pid(eonlet_id)
    write_status(eonlet_id, "running")
    write_heartbeat(eonlet_id)

    # Single-consumer queue: IPC + scheduler push, main_loop drains.
    send, recv = anyio.create_memory_object_stream[TriggerItem](QUEUE_CAPACITY)
    scheduler = CronScheduler(
        definition.config.triggers,
        store,
        send,
        eonlet_id,
        eonlet_dir=paths.eonlet_dir(eonlet_id),
    )
    scheduler.load_dynamic()
    runtime.scheduler = scheduler

    server = IPCServer(
        str(paths.runtime_sock(eonlet_id)),
        _make_handler(runtime, eonlet_id, send, scheduler),
    )
    # Blocking user-decision channel (ADR-0006): shared by the interactive
    # permission confirm and (M3) compaction proposals.
    broker = DecisionBroker(server)
    server.on_disconnect = broker.on_session_closed
    runtime.decision_broker = broker
    runtime.event_listener = _make_event_broadcaster(server)
    runtime.on_delta = _make_delta_broadcaster(server)

    def _on_signal() -> None:
        log.info("worker: signal received, shutting down")
        shutdown.set()

    try:
        # Outbound HTTP client — shared by web_fetch / web_search and any
        # future tool that needs SSRF-guarded egress. Built from the
        # agent's web.fetch config block (ADR-0004). Constructed inside
        # the try so a bad config still tears down via the finally.
        web_cfg = definition.config.web.fetch
        http_fetcher = HTTPFetcher(
            max_bytes=web_cfg.max_bytes,
            timeout=web_cfg.timeout_seconds,
            allow_private_networks=web_cfg.allow_private_networks,
            user_agent=web_cfg.user_agent,
        )
        runtime.http_fetcher = http_fetcher

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
        recall_index.close()
        if http_fetcher is not None:
            await http_fetcher.aclose()


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
    After each run we check the tier-1 compaction trigger (MEMORY_SPEC §4.1)
    and run it inline before pulling the next item — keeping the model's
    context bounded between turns.
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

            # Post-run compaction cascade (MEMORY_SPEC §4.1 / §4.4 / §4.5).
            tier1_ran = await _maybe_run_tier1(runtime)
            if tier1_ran:
                await _maybe_run_tier2(runtime)
            await _maybe_run_tier3(runtime)


async def _maybe_run_tier1(runtime: AgentRuntime) -> bool:
    """Run tier-1 if threshold is crossed. Returns True if compaction ran."""
    cfg = runtime.definition.config.memory
    if not cfg.enabled or not runtime.auto_compact_enabled:
        return False
    from ..memory.injection import working_window_token_estimate
    from ..memory.tier1 import run_tier1
    from ..memory.watermark import read_watermark

    watermark = read_watermark(runtime.memory_dir)
    events = runtime.store.read(since=watermark)
    if not events:
        return False
    tokens = working_window_token_estimate(events, watermark=0)
    if tokens < cfg.episodic.working_memory_tokens:
        return False
    from ..memory.compactor import LLMCompactor

    prov = _resolve_compaction_provider(runtime, cfg.compaction_model)
    if prov is None:
        return False
    compactor = LLMCompactor(prov)
    try:
        outcome = await run_tier1(
            memory_dir=runtime.memory_dir,
            store=runtime.store,
            cfg=cfg,
            compactor=compactor,
            record_event=runtime._record,
        )
        return outcome.ran
    except Exception:
        log.exception("auto-compact: tier-1 raised; skipping")
        return False


async def _maybe_run_tier2(runtime: AgentRuntime) -> None:
    """Run tier-2 (STM→LTM) if STM exceeds short_term_tokens budget."""
    cfg = runtime.definition.config.memory
    if not cfg.enabled or not runtime.auto_compact_enabled:
        return
    from ..memory.paths import short_term_path
    from ..memory.tier2 import run_tier2
    from ..memory.tokens import estimate

    stm_path = short_term_path(runtime.memory_dir)
    if not stm_path.exists():
        return
    stm_tokens = estimate(stm_path.read_text(encoding="utf-8"))
    if stm_tokens < cfg.episodic.short_term_tokens:
        return

    provider = _resolve_compaction_provider(runtime, cfg.compaction_model)
    if provider is None:
        return
    try:
        await run_tier2(
            memory_dir=runtime.memory_dir,
            cfg=cfg,
            provider=provider,
            snapshot_id=runtime.store.latest_id(),
            record_event=runtime._record,
        )
    except Exception:
        log.exception("auto-compact: tier-2 raised; skipping")


async def _maybe_run_tier3(runtime: AgentRuntime) -> None:
    """Run tier-3 (LTM forgetting) if LTM exceeds long_term_tokens budget."""
    cfg = runtime.definition.config.memory
    if not cfg.enabled or not runtime.auto_compact_enabled:
        return
    from ..memory.paths import long_term_path
    from ..memory.tier3 import run_tier3
    from ..memory.tokens import estimate

    ltm_path = long_term_path(runtime.memory_dir)
    if not ltm_path.exists():
        return
    ltm_tokens = estimate(ltm_path.read_text(encoding="utf-8"))
    if ltm_tokens < cfg.episodic.long_term_tokens:
        return

    provider = _resolve_compaction_provider(runtime, cfg.compaction_model)
    if provider is None:
        return
    try:
        await run_tier3(
            memory_dir=runtime.memory_dir,
            cfg=cfg,
            provider=provider,
            record_event=runtime._record,
        )
    except Exception:
        log.exception("auto-compact: tier-3 raised; skipping")


def _resolve_compaction_provider(runtime: AgentRuntime, model: str) -> Any:
    """Return the LLMProvider for compaction, or None on error."""
    if runtime.definition.config.runtime.model == model:
        return runtime.provider
    from ..config import load_global_config
    from ..llm import resolve_model

    try:
        return resolve_model(model, load_global_config())
    except Exception:
        log.exception("auto-compact: failed to build compaction provider; skipping")
        return None


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
                    "is_running": runtime.is_running,
                    "current_activity": runtime.current_activity,
                    "recent_messages": _recent_messages_for_attach(runtime),
                },
            }
        if method == "session.end":
            return {"ok": True}
        if method == "decision.respond":
            # User answered a blocking decision prompt (ADR-0006). First
            # responder wins; an unknown/stale id is a no-op.
            did = str(params.get("id") or "")
            choice = str(params.get("choice") or "")
            broker = runtime.decision_broker
            applied = broker.resolve(did, choice) if broker is not None else False
            return {"ok": applied}
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
        if method == "triggers.add":
            try:
                from ..config import CronTrigger
                from ..triggers.dynamic_store import mint_dynamic_id

                trig = CronTrigger(
                    id=mint_dynamic_id(),
                    schedule=str(params.get("schedule", "")),
                    timezone=str(params.get("timezone", "")),
                    message=str(params.get("message", "")),
                    grace_period=str(params.get("grace_period", "1h")),
                    enabled=True,
                )
                rec = await scheduler.add_dynamic(trig, created_by="cli")
                return {"ok": True, "trigger_id": rec.trig.id}
            except (ConfigError, ValueError) as e:
                return {"ok": False, "error": str(e)}
        if method == "triggers.add_once":
            try:
                from datetime import UTC as _UTC
                from datetime import datetime as _dt
                from datetime import timedelta as _td

                from ..config import OnceTrigger, parse_duration
                from ..triggers.dynamic_store import mint_dynamic_id

                fire_at = params.get("fire_at")
                in_dur = params.get("in")
                if bool(fire_at) == bool(in_dur):
                    return {
                        "ok": False,
                        "error": "provide exactly one of 'fire_at' or 'in'",
                    }
                if in_dur is not None:
                    seconds = parse_duration(str(in_dur))
                    fire_at = (_dt.now(_UTC) + _td(seconds=seconds)).isoformat()
                once_trig = OnceTrigger(
                    id=mint_dynamic_id(),
                    fire_at=str(fire_at),
                    timezone=str(params.get("timezone", "")),
                    message=str(params.get("message", "")),
                    grace_period=str(params.get("grace_period", "1h")),
                    enabled=True,
                )
                once_rec = await scheduler.add_once_dynamic(once_trig, created_by="cli")
                return {
                    "ok": True,
                    "trigger_id": once_rec.trig.id,
                    "fire_at": once_rec.trig.fire_at,
                }
            except (ConfigError, ValueError) as e:
                return {"ok": False, "error": str(e)}
        if method == "triggers.remove":
            tid = str(params.get("trigger_id", ""))
            try:
                removed = await scheduler.remove_dynamic(tid)
            except ConfigError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": removed}
        if method == "triggers.set_enabled":
            tid = str(params.get("trigger_id", ""))
            enabled = bool(params.get("enabled", True))
            try:
                ok = await scheduler.set_enabled(tid, enabled)
            except ConfigError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": ok}
        if method == "triggers.clear":
            n = await scheduler.clear_dynamic()
            return {"ok": True, "cleared": n}
        if method.startswith(("memory.", "task.")):
            return await _handle_memory_ipc(method, params, runtime)
        return {"error": f"unknown method: {method}"}

    return handle


async def _handle_memory_ipc(
    method: str,
    params: dict[str, Any],
    runtime: AgentRuntime,
) -> dict[str, Any]:
    """Dispatch ``memory.knowledge.*`` / ``task.*`` / ``memory.{compact,show,...}``
    IPC methods.

    Events are appended through ``runtime._record`` so they flow into the
    IPC broadcaster the same way tool-driven calls do.
    """
    from ..errors import KnowledgeError, KnowledgePathError
    from ..memory.knowledge import KnowledgeStore
    from ..memory.paths import long_term_path, short_term_path
    from ..runtime.events import (
        kb_deleted,
        kb_moved,
        kb_written,
        mem_paused,
        mem_resumed,
        task_created,
        task_deleted,
        task_transitioned,
        task_updated,
    )
    from ..tasks import can_transition, mint_task_id

    # ── compact / pause / resume / show ──────────────────────────────────
    if method == "memory.compact":
        cfg = runtime.definition.config.memory
        if not cfg.enabled:
            return {"ok": False, "error": "memory subsystem disabled"}
        from ..memory.compactor import LLMCompactor
        from ..memory.tier1 import run_tier1

        prov = _resolve_compaction_provider(runtime, cfg.compaction_model)
        if prov is None:
            return {
                "ok": False,
                "error": f"failed to build compaction provider: {cfg.compaction_model!r}",
            }
        compactor = LLMCompactor(prov)
        # User-forced /compact is the "clean slate" full compaction (ADR-0006):
        # the whole working window is summarized into STM and emptied.
        outcome = await run_tier1(
            memory_dir=runtime.memory_dir,
            store=runtime.store,
            cfg=cfg,
            compactor=compactor,
            record_event=runtime._record,
            full=True,
        )
        if outcome.ran and outcome.error is None:
            # Emptying the working window ends the current episode and starts a
            # fresh one carrying only the injected memory preamble.
            from ..runtime.events import session_ended, session_started

            await runtime._record(session_ended(reason="compact"))
            await runtime._record(session_started(reason="compact"))
        return {
            "ok": outcome.error is None,
            "ran": outcome.ran,
            "sections_added": outcome.sections_added,
            "boundary_event_id": outcome.boundary_event_id,
            "tokens_before": outcome.tokens_before,
            "tokens_after": outcome.tokens_after,
            "error": outcome.error,
        }
    if method == "memory.pause":
        runtime.auto_compact_enabled = False
        await runtime._record(mem_paused())
        return {"ok": True}
    if method == "memory.resume":
        runtime.auto_compact_enabled = True
        await runtime._record(mem_resumed())
        return {"ok": True}
    if method == "memory.show":
        store_name = str(params.get("store", "all"))
        md = runtime.memory_dir
        out: dict[str, Any] = {"ok": True}
        if store_name in ("stm", "all"):
            p = short_term_path(md)
            out["stm"] = p.read_text(encoding="utf-8") if p.exists() else ""
        if store_name in ("ltm", "all"):
            p = long_term_path(md)
            out["ltm"] = p.read_text(encoding="utf-8") if p.exists() else ""
        if store_name in ("knowledge", "all"):
            out["knowledge"] = [
                {"path": e.path, "title": e.title, "hook": e.hook}
                for e in await KnowledgeStore(md).list_entries()
            ]
        out["auto_compact_enabled"] = runtime.auto_compact_enabled
        return out

    # ── knowledge (curated knowledge axis, ADR-0005) ──────────────────────
    if method.startswith("memory.knowledge."):
        kstore = KnowledgeStore(runtime.memory_dir)
        sub = method.removeprefix("memory.knowledge.")
        if sub == "list":
            entries = await kstore.list_entries()
            return {
                "ok": True,
                "knowledge": [{"path": e.path, "title": e.title, "hook": e.hook} for e in entries],
            }
        if sub == "open":
            path = str(params.get("path", ""))
            body = await kstore.open(path) if path else None
            if body is None:
                return {"ok": False, "error": f"no such knowledge file: {path}"}
            return {"ok": True, "path": path, "body": body}
        if sub == "write":
            path = str(params.get("path", "")).strip()
            content = params.get("content")
            if not path or content is None:
                return {"ok": False, "error": "path and content required"}
            try:
                rel = await kstore.write(
                    path=path, content=str(content), index_line=params.get("index_line")
                )
            except (KnowledgeError, KnowledgePathError) as e:
                return {"ok": False, "error": str(e)}
            await runtime._record(kb_written(path=rel, size=len(str(content)), action="write"))
            return {"ok": True, "path": rel}
        if sub == "delete":
            path = str(params.get("path", ""))
            if not path:
                return {"ok": False, "error": "path required"}
            try:
                existed = await kstore.delete(path=path)
            except (KnowledgeError, KnowledgePathError) as e:
                return {"ok": False, "error": str(e)}
            if not existed:
                return {"ok": False, "error": f"no such knowledge file: {path}"}
            await runtime._record(kb_deleted(path=path))
            return {"ok": True}
        if sub == "move":
            src = str(params.get("path", ""))
            dst = str(params.get("new_path", ""))
            if not src or not dst:
                return {"ok": False, "error": "path and new_path required"}
            try:
                src_rel, dst_rel = await kstore.move(
                    src=src, dst=dst, index_line=params.get("index_line")
                )
            except (KnowledgeError, KnowledgePathError) as e:
                return {"ok": False, "error": str(e)}
            await runtime._record(kb_moved(src=src_rel, dst=dst_rel))
            return {"ok": True, "src": src_rel, "dst": dst_rel}
        return {"ok": False, "error": f"unknown method: {method}"}

    # ── tasks (workflow state — event-sourced forest, ADR-0007) ────────────
    if method.startswith("task."):
        forest = runtime.task_forest
        sub = method.removeprefix("task.")
        if sub == "add":
            content = str(params.get("content", "")).strip()
            if not content:
                return {"ok": False, "error": "content required"}
            parent_id = params.get("parent_id")
            if parent_id and forest.get(str(parent_id)) is None:
                return {"ok": False, "error": f"no such parent task: {parent_id}"}
            tid = mint_task_id()
            await runtime._record(
                task_created(
                    id=tid,
                    content=content,
                    goal=str(params.get("goal") or ""),
                    priority=int(params.get("priority") or 0),
                    parent_id=str(parent_id) if parent_id else None,
                    origin="user",
                    due=params.get("due"),
                    tags=list(params.get("tags") or []),
                )
            )
            return {"ok": True, "id": tid}
        if sub == "list":
            status_param = str(params.get("status", "pending"))
            valid = ("pending", "active", "suspended", "blocked", "done", "cancelled", "all")
            if status_param not in valid:
                return {"ok": False, "error": f"invalid status: {status_param}"}
            tasks = forest.by_status(status_param)  # type: ignore[arg-type]
            return {"ok": True, "tasks": [t.to_dict() for t in tasks]}
        if sub in ("done", "cancel"):
            tid = str(params.get("id", ""))
            if not tid:
                return {"ok": False, "error": "id required"}
            task = forest.get(tid)
            if task is None:
                return {"ok": False, "error": f"no such task: {tid}"}
            dst = "done" if sub == "done" else "cancelled"
            if not can_transition(task.status, dst):
                return {"ok": False, "error": f"cannot {sub} task in state {task.status}"}
            await runtime._record(
                task_transitioned(id=tid, from_state=task.status, to_state=dst, reason=f"cli:{sub}")
            )
            return {"ok": True}
        if sub == "update":
            tid = str(params.get("id", ""))
            if not tid:
                return {"ok": False, "error": "id required"}
            if forest.get(tid) is None:
                return {"ok": False, "error": f"no such task: {tid}"}
            await runtime._record(
                task_updated(
                    id=tid,
                    content=params.get("content"),
                    goal=params.get("goal"),
                    priority=params.get("priority"),
                    due=params.get("due"),
                    tags=list(params["tags"]) if "tags" in params else None,
                )
            )
            return {"ok": True}
        if sub == "delete":
            tid = str(params.get("id", ""))
            if not tid:
                return {"ok": False, "error": "id required"}
            if forest.get(tid) is None:
                return {"ok": False, "error": f"no such task: {tid}"}
            await runtime._record(task_deleted(id=tid))
            return {"ok": True}
        return {"ok": False, "error": f"unknown method: {method}"}

    return {"ok": False, "error": f"unknown method: {method}"}


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


def _recent_messages_for_attach(runtime: AgentRuntime) -> list[dict[str, Any]]:
    """Pick a message slice that conveys what the agent has been doing.

    Strategy: anchor on the last user_message and include everything after it
    (the "current run"). If that span is short, pad with one prior turn so the
    user always has at least 4 messages of context. Hard cap at 30 to keep the
    payload bounded across a very long tool-heavy run.
    """
    msgs = runtime.state.messages
    if not msgs:
        return []
    # Find index of the most recent user_message — start of the latest run.
    anchor = 0
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].role == "user":
            anchor = i
            break
    # Pad backwards so re-attach also shows the prior turn for continuity.
    if anchor > 0 and (len(msgs) - anchor) < 4:
        prev_user = 0
        for j in range(anchor - 1, -1, -1):
            if msgs[j].role == "user":
                prev_user = j
                break
        anchor = prev_user
    slice_ = msgs[max(anchor, len(msgs) - 30) :]
    return [
        {
            "role": m.role,
            "content": m.content,
            "tool_calls": [{"name": tc.get("name"), "args": tc.get("args")} for tc in m.tool_calls],
            "is_error": m.is_error,
        }
        for m in slice_
    ]


def _event_to_dict(event: Event) -> dict[str, Any]:
    d = event.model_dump()
    d["kind"] = str(event.kind)
    return d


if __name__ == "__main__":
    main()
