"""Tier-1 compaction orchestration (MEMORY_SPEC §4.1 / §4.3).

Sequence per pass:

1. Capture snapshot — current ``store.latest_id()`` is the upper bound; new
   events appended during this run land in the *next* pass (§4.3).
2. Read events in ``(watermark, snapshot_id]`` from the event store.
3. Compute a suggested boundary that keeps ~30% of the budget unsummarized
   *and* doesn't split a tool_call / tool_result pair.
4. Call the compactor LLM with that suggested boundary.
5. On success: append sections to STM, advance the watermark to the
   returned ``boundary_event_id``, emit ``mem_compacted``.
6. On any failure: log + emit ``ERROR``; do not change persistent state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import anyio

from ..runtime.events import Event, EventKind, mem_compacted
from ..runtime.store import EventStore
from .compactor import CompactionResult, Compactor
from .config import MemoryConfig
from .injection import working_window_token_estimate
from .stm import STMStore
from .watermark import read_watermark, write_watermark

RecordEventFn = Callable[[Event], Awaitable[Event]]

log = logging.getLogger("eonlet.memory.tier1")

# One lock per eonlet directory so tier-1 doesn't race itself.
_locks: dict[str, anyio.Lock] = {}


def _lock_for(memory_dir: Path) -> anyio.Lock:
    key = str(memory_dir.resolve())
    lock = _locks.get(key)
    if lock is None:
        lock = anyio.Lock()
        _locks[key] = lock
    return lock


@dataclass(slots=True)
class CompactionOutcome:
    """Returned by ``run_tier1``. ``ran`` is False if there was nothing to do."""

    ran: bool
    boundary_event_id: int | None = None
    sections_added: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    error: str | None = None


# ── Suggested boundary ──────────────────────────────────────────────────────


def compute_suggested_boundary(events: list[Event], cfg: MemoryConfig) -> int:
    """Pick the boundary the compactor is suggested but not forced to respect.

    Strategy: walk backwards from the most recent event, accumulating tokens
    until we have at least ``keep_recent_messages_min`` messages and at
    least 30% of the budget. The boundary is the id of the most recent
    event NOT in this preserved tail — i.e. the event right before the cut.

    Then nudge backwards if the boundary lands on the inside of a tool
    call/result pair: never cut between an assistant_message's tool_call(s)
    and its tool_result(s).
    """
    if not events:
        return 0
    from .injection import _event_tokens

    budget = cfg.conversation.working_memory_tokens
    min_keep = cfg.conversation.keep_recent_messages_min
    floor_tokens = max(budget * 3 // 10, 1)

    preserved: list[Event] = []
    tokens = 0
    for ev in reversed(events):
        cost = _event_tokens(ev)
        preserved.append(ev)
        tokens += cost
        if len(preserved) >= min_keep and tokens >= floor_tokens:
            break

    if len(preserved) >= len(events):
        # Everything preserved — nothing to compact this pass.
        return (events[0].id or 0) - 1 if events[0].id else 0

    # The boundary is the id of the youngest event NOT preserved.
    preserved.reverse()
    first_preserved_idx = events.index(preserved[0])
    boundary_idx = first_preserved_idx - 1
    boundary = events[boundary_idx].id or 0

    # Tool-pair safety: never put the boundary at a tool_call whose result is
    # in the preserved tail. Pull the boundary back to the assistant_message
    # that owns the tool_call(s) if needed.
    while boundary_idx > 0 and events[boundary_idx].kind in (
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.TOOL_ERROR,
    ):
        boundary_idx -= 1
        boundary = events[boundary_idx].id or 0
    return boundary


# ── Orchestration ───────────────────────────────────────────────────────────


async def run_tier1(
    *,
    memory_dir: Path,
    store: EventStore,
    cfg: MemoryConfig,
    compactor: Compactor,
    record_event: RecordEventFn | None = None,
) -> CompactionOutcome:
    """Run one tier-1 pass.

    ``record_event`` is the runtime's ``_record`` callable when invoked from
    inside an agent; pass ``None`` for stand-alone invocation (tests / CLI
    that doesn't have a live AgentRuntime — though for v0.1 the worker
    always supplies it).
    """
    lock = _lock_for(memory_dir)
    async with lock:
        snapshot_id = store.latest_id()
        watermark = read_watermark(memory_dir)
        if snapshot_id <= watermark:
            return CompactionOutcome(ran=False)

        # Snapshot: events in (watermark, snapshot_id].
        all_events = store.read(since=watermark)
        events = [e for e in all_events if (e.id or 0) <= snapshot_id]
        if not events:
            return CompactionOutcome(ran=False)

        tokens_before = working_window_token_estimate(events, watermark=0)
        if tokens_before == 0:
            # Nothing text-bearing to compact (e.g. only bookkeeping events).
            return CompactionOutcome(ran=False)

        suggested = compute_suggested_boundary(events, cfg)
        if suggested <= watermark:
            # Nothing on the non-tail side to compact.
            return CompactionOutcome(ran=False)

        # ── compactor call ────────────────────────────────────────────
        try:
            result: CompactionResult = await compactor.summarize(events, suggested)
        except Exception as e:
            msg = f"compactor failed: {e}"
            log.warning(msg)
            if record_event is not None:
                ev = Event(
                    kind=EventKind.ERROR,
                    payload={"where": "tier1.compactor", "error": str(e)},
                )
                await record_event(ev)
            return CompactionOutcome(ran=False, error=msg)

        # ── persist STM + advance watermark ───────────────────────────
        try:
            await STMStore(memory_dir).append_sections(result.sections)
        except OSError as e:
            msg = f"failed to write STM: {e}"
            log.error(msg)
            return CompactionOutcome(ran=False, error=msg)

        new_watermark = result.boundary_event_id
        write_watermark(memory_dir, new_watermark)

        # Approximate "tokens_after" as the residual after the boundary.
        tokens_after = working_window_token_estimate(events, watermark=new_watermark)

        # Emit event AFTER state change so replay reconstructs the right order.
        model_name = type(compactor).__name__
        if record_event is not None:
            ev2 = mem_compacted(
                snapshot_id=snapshot_id,
                boundary_event_id=new_watermark,
                sections_added=len(result.sections),
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                model=model_name,
            )
            await record_event(ev2)

        return CompactionOutcome(
            ran=True,
            boundary_event_id=new_watermark,
            sections_added=len(result.sections),
            tokens_before=tokens_before,
            tokens_after=tokens_after,
        )
