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
import contextlib
import logging
import os
import signal
import sys
import time
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
from ..tasks import Task, next_runnable
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
    await _recover_stale_tasks(runtime)

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
    runtime.on_delta = _make_delta_broadcaster(server, runtime)

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


async def _recover_stale_tasks(runtime: AgentRuntime) -> None:
    """Re-queue tasks left ``active`` by a crashed worker.

    A worker that died mid task run replays that task as ``active`` — a state
    the scheduler never re-picks, wedging it forever. At startup nothing can
    legitimately be active (execution is strictly serial and no run is in
    flight), so any active task is a crash residue.
    """
    from ..runtime.events import task_transitioned

    for stale in runtime.task_forest.by_status("active"):
        log.warning("crash recovery: re-queueing task %s (was active at shutdown)", stale.id)
        await runtime._record(
            task_transitioned(
                id=stale.id, from_state="active", to_state="pending", reason="crash_recovery"
            )
        )


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


SCHED_POLL_S = 1.0  # idle re-check cadence when the task scheduler is enabled
_CLOSED = object()  # sentinel: the trigger stream was closed


def _try_receive(recv: MemoryObjectReceiveStream[TriggerItem]) -> TriggerItem | None:
    """Non-blocking drain to the first real trigger. ``None`` if only wakes/empty.

    ``task_wake`` sentinels (pushed by the IPC handler to unblock an idle loop)
    carry no work — they are drained here so the caller falls through to the
    scheduler check.
    """
    while True:
        try:
            item = recv.receive_nowait()
        except (anyio.WouldBlock, anyio.EndOfStream, anyio.ClosedResourceError):
            return None
        if item.kind != "task_wake":
            return item


async def _recv_one(recv: MemoryObjectReceiveStream[TriggerItem]) -> TriggerItem | object:
    """Block for one trigger; ``_CLOSED`` if the stream closed. Caller bounds the wait."""
    try:
        return await recv.receive()
    except (anyio.EndOfStream, anyio.ClosedResourceError):
        return _CLOSED


async def _main_loop(
    runtime: AgentRuntime,
    recv: MemoryObjectReceiveStream[TriggerItem],
    scheduler: CronScheduler,
    shutdown: anyio.Event,
) -> None:
    """Drive triggers and autonomous task work, one beat at a time.

    Each beat: a queued trigger (user/cron input) takes precedence; otherwise,
    when ``tasks.scheduling.enabled``, the task scheduler picks the next runnable
    task and the agent runs it task-scoped (ADR-0007 M2). When fully idle the
    loop blocks — with a poll timeout under scheduling so a task created
    out-of-band (e.g. via the `task` IPC) is noticed. The worker's single
    consumer keeps execution strictly serial (the "one human-like worker"
    invariant). After every run the compaction cascade runs inline.
    """
    async with recv:
        while not shutdown.is_set():
            sched = runtime.definition.config.tasks.scheduling

            # 1. A queued trigger always wins over autonomous task work.
            item = _try_receive(recv)

            # 2. Otherwise, run the next scheduler-selected task.
            if item is None and sched.enabled:
                task = next_runnable(runtime.task_forest)
                if task is not None:
                    try:
                        await _run_task(runtime, task)
                    except Exception:
                        log.exception("main_loop: task run failed")
                    await _run_cascade(runtime)
                    continue

            # 3. Block for the next trigger. Under scheduling, bound the wait so
            #    a task created out-of-band (e.g. via the `task` IPC) is noticed.
            if item is None:
                got: TriggerItem | object | None = None
                if sched.enabled:
                    with anyio.move_on_after(SCHED_POLL_S):
                        got = await _recv_one(recv)
                else:
                    got = await _recv_one(recv)
                if got is _CLOSED:
                    return
                if got is None:
                    continue  # timed out → re-check the scheduler
                assert isinstance(got, TriggerItem)
                item = got

            # A wake sentinel only exists to re-check the scheduler — it carries
            # no message to run.
            if item.kind == "task_wake":
                continue

            # A scheduled task-template fire hatches a fresh task instance, then
            # the scheduler picks it up on the next beat (ADR-0007 M3).
            if item.kind == "task_hatch":
                await _hatch_task(runtime, item)
                continue

            # Stamp the turn origin (ADR-0008 §5) so a task created this turn is
            # attributed correctly, and clear the interactive-interrupt mark this
            # message satisfied (§3).
            if item.kind == "interactive":
                runtime.turn_origin = "user"
                runtime.pending_interactive = max(0, runtime.pending_interactive - 1)
            else:
                runtime.turn_origin = "trigger"

            ok = True
            try:
                async for _ in runtime.handle_user_message(item.content):
                    pass
            except Exception:
                log.exception("main_loop: run failed")
                ok = False
            finally:
                runtime.turn_origin = "user"
            if item.kind == "cron" and item.trigger_id:
                scheduler.record_outcome(item.trigger_id, success=ok)

            await _run_cascade(runtime)


async def _run_task(runtime: AgentRuntime, task: Task) -> None:
    """Run one scheduler-selected task to a yield point, then apply its outcome.

    The task is activated, run with its assembled prompt, then classified: the
    agent marks it done (DONE), adds subtasks (DECOMPOSED → block on them),
    yields without finishing (YIELDED → checkpoint + suspend), or is paused mid-
    run for a higher-priority task (preempted → checkpoint + re-queue as pending,
    so it resumes once the preemptor is done). All cooperative (ADR-0007 M2/M3).
    """
    from ..runtime.events import task_checkpointed, task_transitioned
    from ..tasks import PostRun, classify_post_run
    from ..tasks.context import build_task_prompt

    sched = runtime.definition.config.tasks.scheduling
    preempt_to: dict[str, str] = {}  # {"id": contender} once we decide to pause

    await runtime._record(
        task_transitioned(id=task.id, from_state=task.status, to_state="active", reason="scheduled")
    )
    # On first dispatch, give the task its down-tree decision trace (ADR-0009 M3)
    # so build_task_prompt can carry the parent's/chat's decisions into the run.
    await _ensure_framing(runtime, task)
    prompt = build_task_prompt(runtime.task_forest, task.id)
    runtime.current_task_id = task.id
    runtime.turn_origin = "agent"
    # Always install the turn-boundary hook: user-input preemption (ADR-0008 §3)
    # is possible regardless of the `preempt`/budget config, and the hook also
    # enforces the per-task token budget (ADR-0007 M4) and agent-initiated
    # cross-tree preemption (§6) when those are configured.
    runtime.pause_check = _make_pause_check(runtime, task, sched, preempt_to)
    # Fold the task's own-scope working memory into its brief when it exceeds
    # budget, at each turn boundary (ADR-0009 M4).
    runtime.on_turn_boundary = lambda: _maybe_compact_task(runtime, task.id)
    try:
        async for _ in runtime.handle_user_message(prompt):
            pass
    finally:
        runtime.current_task_id = None
        runtime.turn_origin = "user"
        runtime.pause_check = None
        runtime.on_turn_boundary = None

    if preempt_to:
        if preempt_to.get("gone"):
            # Cancelled/deleted out from under the run via the control plane —
            # there is nothing left to checkpoint or transition.
            return
        # Paused (user interrupt or a higher-priority tree): checkpoint a resume
        # brief and put the task back to pending so the scheduler re-picks it once
        # the preemptor — now outranking it — finishes, resuming from the brief.
        # A user-input pause is expected to resume promptly and (boundary=None)
        # keeps the raw scoped window intact, so the cheap structural brief
        # suffices — no LLM call per chat message while a task runs.
        user_interrupt = preempt_to.get("reason") == "preempted:user-input"
        summary, boundary = await _checkpoint_summary(
            runtime, task.id, structural_only=user_interrupt
        )
        await runtime._record(
            task_checkpointed(id=task.id, progress_summary=summary, boundary_event_id=boundary)
        )
        reason = str(preempt_to.get("reason") or f"preempted:{preempt_to.get('id', '')}")
        await runtime._record(
            task_transitioned(id=task.id, from_state="active", to_state="pending", reason=reason)
        )
        return

    outcome = classify_post_run(runtime.task_forest, task.id)
    if outcome is PostRun.DECOMPOSED:
        await runtime._record(
            task_transitioned(
                id=task.id, from_state="active", to_state="blocked", reason="decomposed"
            )
        )
    elif outcome is PostRun.YIELDED:
        # Cap the suspended backlog (ADR-0007 M4): if too many tasks are already
        # suspended, drop this no-progress task instead of growing the pile.
        suspended = len(runtime.task_forest.by_status("suspended"))
        if sched.max_suspended and suspended >= sched.max_suspended:
            log.warning("suspended backlog full (%d); cancelling %s", suspended, task.id)
            await runtime._record(
                task_transitioned(
                    id=task.id,
                    from_state="active",
                    to_state="cancelled",
                    reason="suspend_backlog_full",
                )
            )
            # Don't let the backlog cap drop a root silently — tell the chat.
            await _surface_root_result(runtime, task.id)
        else:
            summary, boundary = await _checkpoint_summary(runtime, task.id)
            await runtime._record(
                task_checkpointed(id=task.id, progress_summary=summary, boundary_event_id=boundary)
            )
            await runtime._record(
                task_transitioned(
                    id=task.id, from_state="active", to_state="suspended", reason="yielded"
                )
            )
    elif outcome is PostRun.DONE:
        # The upward edge of ADR-0009's asymmetric flow: a finished ROOT's
        # result surfaces into the chat scope, where the model (and tier-1 →
        # episodic memory) can see it. Without this the conversation never
        # learns what its own background work produced.
        await _surface_root_result(runtime, task.id)
    # GONE: the task was deleted mid-run — nothing to surface.


async def _surface_root_result(runtime: AgentRuntime, task_id: str) -> None:
    """Record a chat-scope ``<task_result>`` envelope for a terminal root task.

    Mirrors the ``<trigger>`` envelope convention: a user-role message that
    enters the chat window naturally on the next turn and is folded into STM by
    tier-1. Subtask results stay out — they flow up via the parent's synthesis
    turn, and surfacing them here would leak child internals past the parent.
    No agent run is triggered; the envelope just waits in the window.
    """
    from ..runtime.events import user_message

    t = runtime.task_forest.get(task_id)
    if t is None or t.parent_id is not None:
        return
    body = t.result.strip() if t.result else "(no result recorded)"
    await runtime._record(
        user_message(
            f'<task_result id="{t.id}" status="{t.status}">\n'
            f"Goal: {t.goal or t.content}\n"
            f"Result: {body}\n"
            "</task_result>"
        )
    )


def _task_label(task: Task) -> str:
    text = (task.goal or task.content).strip()
    return text if len(text) <= 60 else text[:59] + "…"


def _make_pause_check(
    runtime: AgentRuntime, task: Task, sched: Any, preempt_to: dict[str, str]
) -> Callable[[], Awaitable[bool]]:
    """Build the turn-boundary hook: per-task budget cap + preemption."""
    from ..config import parse_duration
    from ..tasks import is_terminal, preemptor

    cooldown = parse_duration(sched.preempt_cooldown)
    budget = int(sched.per_task_budget_tokens or 0)
    # Incremental token accounting: each check reads only the events appended
    # since the previous check, not the whole run suffix every turn.
    spent = 0
    last_seen = runtime.store.latest_id()  # token baseline for this run

    async def check() -> bool:
        nonlocal spent, last_seen
        if preempt_to:  # already decided to pause this run
            return True
        # 0. The task was cancelled/deleted out from under the run (via the
        #    control plane): stop immediately instead of burning tokens to the
        #    natural end. ``gone`` tells _run_task to skip checkpoint/transition.
        cur = runtime.task_forest.get(task.id)
        if cur is None or is_terminal(cur.status):
            preempt_to["gone"] = "1"
            preempt_to["reason"] = "task_gone"
            log.info("task %s gone/terminal mid-run; ending run", task.id)
            return True
        # 1. A queued interactive user message preempts unconditionally (ADR-0008
        #    §3): the user is interrupting — yield so the worker can handle the
        #    message (which may create a higher-priority task). No consent, no
        #    cooldown; re-queued as pending (preempt_to set).
        if runtime.pending_interactive > 0:
            preempt_to["reason"] = "preempted:user-input"
            log.info("task %s paused for queued user input", task.id)
            return True
        # 2. Per-task token budget (ADR-0007 M4): end the run if it has spent its
        #    allowance. Leaves preempt_to empty → classified as a yield (suspend).
        if budget:
            for e in runtime.store.read(since=last_seen):
                spent += (e.tokens_in or 0) + (e.tokens_out or 0)
                if (e.id or 0) > last_seen:
                    last_seen = e.id or last_seen
            if spent >= budget:
                log.info("task %s hit per-task token budget %d (spent %d)", task.id, budget, spent)
                return True
        # 3. Cross-tree preemption by a strictly-higher-priority root (ADR-0008
        #    §3/§4). Consent splits by the contender root's origin: a user tree
        #    preempts unconditionally; an agent tree keeps ADR-0007 §6 consent +
        #    cooldown; a trigger tree is never returned by `preemptor`.
        contender = preemptor(runtime.task_forest, cur)
        if contender is None:
            return False
        croot = runtime.task_forest.root_of(contender.id)
        origin = croot.origin if croot is not None else "agent"
        if origin != "user":
            # Agent-initiated cross-tree preemption: governed by `preempt`,
            # the cooldown, and the consent channel.
            if sched.preempt == "off":
                return False
            if time.monotonic() - runtime.last_preempt_monotonic < cooldown:
                return False  # anti-thrash
            if not await _approve_preempt(runtime, cur, contender, sched):
                return False
        preempt_to["id"] = contender.id
        preempt_to["reason"] = f"preempted:{origin}:{contender.id}"
        runtime.last_preempt_monotonic = time.monotonic()
        log.info("task %s paused for higher-priority %s (%s)", task.id, contender.id, origin)
        return True

    return check


async def _approve_preempt(
    runtime: AgentRuntime, current: Task, contender: Task, sched: Any
) -> bool:
    """Consent for a preemption switch. ``auto_by_priority`` (or yolo) auto-
    approves; ``ask`` prompts the attached user (declines if headless)."""
    if sched.preempt == "auto_by_priority" or runtime.gate.mode == "yolo":
        return True
    broker = runtime.decision_broker
    if broker is None:
        return False
    choice = await broker.ask(
        kind="task_preempt",
        prompt=(
            f"Pause '{_task_label(current)}' to run higher-priority '{_task_label(contender)}'?"
        ),
        options=["switch", "keep"],
        payload={"pause": current.id, "start": contender.id},
        decline="keep",
    )
    return bool(choice == "switch")


_MIN_TRACE_SOURCE_EVENTS = 2  # below this the goal already suffices — skip the LLM call


async def _ensure_framing(runtime: AgentRuntime, task: Task) -> None:
    """Compute & store a task's down-tree decision trace on its first dispatch.

    ADR-0009 §3: a subtask inherits a compressed snapshot of its parent's
    decisions; a user-origin root inherits the chat turns that motivated it — so
    the run stays coherent with how the work was decomposed (Cognition Principle
    1). Computed once (stored on the task via ``framing``) and skipped on later
    runs. Trigger-origin roots, a trivial source, a disabled/absent compaction
    provider, or any failure all leave the task goal-only (never fatal).
    """
    if task.framing:
        return  # already computed on an earlier dispatch
    cfg = runtime.definition.config.memory
    if not cfg.enabled:
        return

    parent = runtime.task_forest.get(task.parent_id) if task.parent_id else None
    if parent is not None:
        source_scope: str | None = parent.id  # the parent task's scope
        parent_goal = parent.goal or parent.content
    elif task.origin == "user":
        source_scope = None  # the chat scope motivated this root
        parent_goal = "the user conversation"
    else:
        return  # trigger-origin root etc. — nothing decomposed it

    source_events = [e for e in runtime.store.read() if e.task_id == source_scope]
    if len(source_events) < _MIN_TRACE_SOURCE_EVENTS:
        return  # the goal already captures a trivial source

    prov = _resolve_compaction_provider(runtime, cfg.compaction_model)
    if prov is None:
        return
    try:
        from ..tasks.brief import build_decision_trace

        trace = await build_decision_trace(
            prov,
            parent_goal=parent_goal,
            child_goal=task.goal or task.content,
            events=source_events,
        )
    except Exception:
        log.exception("down-tree trace: compression failed; task runs goal-only")
        return
    if trace.strip():
        from ..runtime.events import task_updated

        await runtime._record(task_updated(id=task.id, framing=trace.strip()))


async def _checkpoint_summary(
    runtime: AgentRuntime, task_id: str, *, structural_only: bool = False
) -> tuple[str, int | None]:
    """The resume brief written when a task is suspended/yielded/preempted.

    Returns ``(brief, boundary_event_id)``. ADR-0009 M2/M4: compress the task's
    **un-folded** own-scope events (id > the task's brief watermark), cumulative
    with any prior brief, into key details/events/decisions via the compaction LLM
    (decision continuity — Cognition). ``boundary_event_id`` is the latest folded
    id, advancing the watermark so a resume window starts clean.

    ``structural_only=True`` skips the LLM call outright — used for user-input
    pauses, which resume promptly and keep their raw window (boundary=None), so
    paying an LLM compression per chat message would be waste.

    Falls back to a structural summary with ``boundary=None`` (no watermark
    advance, so nothing is pruned/lost) when memory is disabled, no compaction
    provider resolves, the task has no new turns, or the LLM call fails — a
    checkpoint must never be blocked.
    """
    t = runtime.task_forest.get(task_id)
    goal = (t.goal or t.content) if t is not None else task_id
    prior = t.progress_summary if t is not None else ""
    wm = t.brief_watermark if t is not None else 0

    cfg = runtime.definition.config.memory
    scope = [e for e in runtime.store.read() if e.task_id == task_id and (e.id or 0) > wm]
    boundary = scope[-1].id if scope else None
    if cfg.enabled and scope and not structural_only:
        prov = _resolve_compaction_provider(runtime, cfg.compaction_model)
        if prov is not None:
            try:
                from ..tasks.brief import build_task_brief

                brief = await build_task_brief(prov, goal=goal, events=scope, prior_brief=prior)
                if brief.strip():
                    return brief.strip(), boundary
            except Exception:
                log.exception("checkpoint brief: LLM compression failed; using structural fallback")
    # Structural fallback: do NOT advance the watermark (boundary=None) so the
    # un-folded turns stay in the window — a lossy structural brief must never
    # cause raw turns to be pruned.
    return _structural_checkpoint_summary(runtime, task_id, goal, prior), None


def _structural_checkpoint_summary(
    runtime: AgentRuntime, task_id: str, goal: str, prior: str = ""
) -> str:
    """Deterministic fallback brief: the task's last non-empty assistant turn,
    appended to any prior brief so earlier compactions aren't dropped."""
    last = next(
        (
            m.content
            for m in reversed(runtime.state.messages)
            if m.role == "assistant" and m.content.strip() and m.task_id == task_id
        ),
        "",
    )
    note = (last[:500] + " …") if len(last) > 500 else (last or "(no output)")
    base = f"In progress: {goal}\nLast: {note}"
    return f"{prior}\n{base}" if prior.strip() else base


async def _maybe_compact_task(runtime: AgentRuntime, task_id: str) -> None:
    """Task-scope tier-1 (ADR-0009 M4): when a running task's un-folded own-scope
    window exceeds budget, fold its older turns (keeping a recent tail) into the
    cumulative brief and advance the brief watermark, so the window stays bounded
    while continuity is preserved (the brief is injected into the system prompt).

    Reversible-before-irreversible: recent turns stay raw in the window; only the
    older portion is summarized. Any failure leaves the window bounded by the
    ordinary budget walk (non-fatal).
    """
    from ..memory.injection import working_window_token_estimate
    from ..memory.tier1 import compute_suggested_boundary
    from ..runtime.events import task_checkpointed

    cfg = runtime.definition.config.memory
    if not cfg.enabled:
        return
    t = runtime.task_forest.get(task_id)
    if t is None:
        return
    scope = [
        e for e in runtime.store.read() if e.task_id == task_id and (e.id or 0) > t.brief_watermark
    ]
    if working_window_token_estimate(scope, watermark=0) < cfg.episodic.working_memory_tokens:
        return
    boundary = compute_suggested_boundary(scope, cfg)
    if boundary <= t.brief_watermark:
        return  # nothing on the non-tail side to fold
    fold = [e for e in scope if (e.id or 0) <= boundary]
    if not fold:
        return
    prov = _resolve_compaction_provider(runtime, cfg.compaction_model)
    if prov is None:
        return
    try:
        from ..tasks.brief import build_task_brief

        brief = await build_task_brief(
            prov, goal=t.goal or t.content, events=fold, prior_brief=t.progress_summary
        )
    except Exception:
        log.exception("task-scope compaction failed; window stays bounded by the budget walk")
        return
    if brief.strip():
        await runtime._record(
            task_checkpointed(
                id=task_id, progress_summary=brief.strip(), boundary_event_id=boundary
            )
        )


async def _hatch_task(runtime: AgentRuntime, item: TriggerItem) -> None:
    """Mint a fresh task instance from a scheduled template (ADR-0007 M3).

    Each fire produces its own task (``origin="trigger"``) with its own id and
    history — a recurring schedule is a template that hatches instances, not one
    task that re-runs.
    """
    from ..runtime.events import task_created
    from ..tasks import mint_task_id

    t = item.task_template or {}
    content = str(t.get("content", "")).strip()
    if not content:
        log.warning("task_hatch from trigger %s has no content; skipping", item.trigger_id)
        return
    await runtime._record(
        task_created(
            id=mint_task_id(),
            content=content,
            goal=str(t.get("goal", "")),
            priority=int(t.get("priority", 0) or 0),
            origin="trigger",
            due=t.get("due"),
            tags=list(t.get("tags") or []),
        )
    )


async def _run_cascade(runtime: AgentRuntime) -> None:
    """Post-run compaction cascade (MEMORY_SPEC §4.1 / §4.4 / §4.5)."""
    tier1_ran = await _maybe_run_tier1(runtime)
    if tier1_ran:
        await _maybe_run_tier2(runtime)
    await _maybe_run_tier3(runtime)


async def _maybe_run_tier1(runtime: AgentRuntime) -> bool:
    """Run tier-1 if threshold is crossed. Returns True if compaction ran."""
    cfg = runtime.definition.config.memory
    if not cfg.enabled or not runtime.auto_compact_enabled:
        return False
    from ..memory.injection import chat_scope_only, working_window_token_estimate
    from ..memory.tier1 import run_tier1
    from ..memory.watermark import read_watermark

    watermark = read_watermark(runtime.memory_dir)
    # The chat working-window threshold counts chat-scope turns only (ADR-0009
    # §5): a busy task must not trigger episodic compaction of the conversation.
    events = chat_scope_only(runtime.store.read(since=watermark))
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
                    "mode": runtime.gate.mode,
                    "is_running": runtime.is_running,
                    "current_activity": runtime.current_activity,
                    "recent_messages": _recent_messages_for_attach(runtime),
                },
            }
        if method == "permissions.set_mode":
            # Switch the permission mode for this running session (not persisted
            # to agent.yaml). ``toggle`` flips yolo↔ask. Security-relevant — log it.
            want = str(params.get("mode") or "").lower()
            cur = runtime.gate.mode
            if want == "toggle":
                want = "ask" if cur == "yolo" else "yolo"
            if want not in ("yolo", "ask"):
                return {"ok": False, "error": f"invalid mode: {want} (yolo|ask|toggle)"}
            runtime.gate.mode = "yolo" if want == "yolo" else "ask"
            log.info("permission mode changed: %s %s -> %s", eonlet_id, cur, want)
            return {"ok": True, "mode": runtime.gate.mode, "previous": cur}
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
            # Mark an interactive interrupt pending (ADR-0008 §3): a running
            # task's pause_check yields on this so the user is attended to
            # promptly. Decremented by _main_loop when the message is dequeued.
            runtime.pending_interactive += 1
            return {"ok": True}
        if method == "trigger.fire":
            tid = params.get("trigger_id")
            if not tid:
                return {"ok": False, "error": "missing trigger_id"}
            trig = scheduler.get(tid)
            if trig is None:
                return {"ok": False, "error": f"no such trigger: {tid}"}
            # A task-template trigger hatches an instance (ADR-0007 M3) rather
            # than running a conversation, even when fired manually.
            template = getattr(trig, "task_template", None)
            if isinstance(template, dict):
                item = TriggerItem(
                    kind="task_hatch", content="", trigger_id=tid, task_template=template
                )
            else:
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
                item = TriggerItem(kind="cron", content=content, trigger_id=tid)
            try:
                send.send_nowait(item)
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
            task_id = params.get("task_id")
            if task_id is not None:
                # A task's execution trace (ADR-0009 scope): scan the log and
                # keep that task's conversation events, capped to the last 200.
                scoped = [e for e in runtime.store.read(since=since) if e.task_id == task_id]
                return [_event_to_dict(e) for e in scoped[-200:]]
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
            resp = await _handle_memory_ipc(method, params, runtime)
            # A task mutation may create runnable work; poke the loop so it
            # re-checks the scheduler promptly instead of waiting for the idle
            # poll (ADR-0007 M2). The sentinel carries no message.
            if method.startswith("task.") and runtime.definition.config.tasks.scheduling.enabled:
                with contextlib.suppress(anyio.WouldBlock):
                    send.send_nowait(TriggerItem(kind="task_wake", content=""))
            return resp
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
            await runtime._record(
                kb_written(path=rel, size=len(str(content)), action="write", content=str(content))
            )
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
            from ..tasks import creation_guard_error

            sched = runtime.definition.config.tasks.scheduling
            guard = creation_guard_error(
                forest,
                str(parent_id) if parent_id else None,
                max_depth=sched.max_tree_depth,
                max_fanout=sched.max_fanout,
            )
            if guard is not None:
                return {"ok": False, "error": guard}
            tid = mint_task_id()
            # Control-plane task creation is user-initiated (ADR-0008 §1): a root
            # is origin="user" (preempts without consent); a subtask follows the
            # root-only-priority rule (§2) — priority 0, origin="agent".
            is_subtask = bool(parent_id)
            await runtime._record(
                task_created(
                    id=tid,
                    content=content,
                    goal=str(params.get("goal") or ""),
                    priority=0 if is_subtask else int(params.get("priority") or 0),
                    parent_id=str(parent_id) if parent_id else None,
                    origin="agent" if is_subtask else "user",
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
        if sub == "tree":
            # The forest in DFS order with a ``depth`` per node, for the tree
            # view (incl. history). A status filter keeps matching tasks plus
            # their ancestors so the tree stays connected.
            status_param = str(params.get("status", "all"))
            valid = ("pending", "active", "suspended", "blocked", "done", "cancelled", "all")
            if status_param not in valid:
                return {"ok": False, "error": f"invalid status: {status_param}"}
            keep = {t.id for t in forest.by_status(status_param)}  # type: ignore[arg-type]
            if status_param != "all":
                for tid in list(keep):
                    cur = forest.get(tid)
                    while cur is not None and cur.parent_id is not None:
                        keep.add(cur.parent_id)
                        cur = forest.get(cur.parent_id)
            nodes: list[dict[str, Any]] = []
            for t, depth in forest.dfs():
                if t.id not in keep:
                    continue
                node = t.to_dict()
                node["depth"] = depth
                nodes.append(node)
            return {"ok": True, "tasks": nodes}
        if sub in ("done", "cancel", "suspend", "resume"):
            tid = str(params.get("id", ""))
            if not tid:
                return {"ok": False, "error": "id required"}
            task = forest.get(tid)
            if task is None:
                return {"ok": False, "error": f"no such task: {tid}"}
            # resume re-queues a suspended task (→ pending); the others map to
            # their lifecycle state. ADR-0007 M4 CLI ops.
            dst = {
                "done": "done",
                "cancel": "cancelled",
                "suspend": "suspended",
                "resume": "pending",
            }[sub]
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


def _make_delta_broadcaster(
    server: IPCServer, runtime: AgentRuntime
) -> Callable[[str], Awaitable[None]]:
    """Token delta hook — pushed as a JSON-RPC ``token_delta`` notification
    (SPEC §8.1) without going through the event store.

    The current task scope (``None`` for chat) is stamped on each delta so the
    attach client can suppress thinking output from a conversation it is not
    viewing (ADR-0009 task scope).
    """

    async def listener(text: str) -> None:
        await server.broadcast(
            "token_delta", {"delta_text": text, "task_id": runtime.current_task_id}
        )

    return listener


def _recent_messages_for_attach(runtime: AgentRuntime) -> list[dict[str, Any]]:
    """Pick a message slice that conveys what the agent has been doing.

    Strategy: anchor on the last user_message and include everything after it
    (the "current run"). If that span is short, pad with one prior turn so the
    user always has at least 4 messages of context. Hard cap at 30 to keep the
    payload bounded across a very long tool-heavy run.
    """
    # Chat scope only (ADR-0009): the re-attach banner shows the user↔agent
    # conversation, not task-execution turns (those are followed via /task view).
    msgs = [m for m in runtime.state.messages if m.task_id is None]
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
