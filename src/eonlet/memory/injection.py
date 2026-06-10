"""Context injection (MEMORY_SPEC §3).

Builds two artifacts per LLM call:

1. **Memory preamble** — appended to the system prompt as a single
   ``<memory>...</memory>`` block containing ``<knowledge_index>``,
   ``<long_term>``, ``<todos>``, and ``<short_term>`` sub-elements (empty
   stores are omitted; the outer ``<memory>`` is omitted when all sub-stores
   are empty). The knowledge index is the always-injected map of the curated
   knowledge tree (ADR-0005); file bodies are opened on demand, never injected.
2. **Recent-messages window** — the suffix of the event log with
   ``id > compaction_watermark`` accumulated until ``working_memory_tokens``
   is reached, snapped to a tool_call/tool_result-safe boundary.

The whole module is **pure**: it reads from on-disk stores and the in-memory
``AgentState`` but never mutates anything. Compaction (which DOES mutate) is
in ``compactor.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog

from ..runtime.events import Event, EventKind
from ..tasks import Task, TaskForest
from ..tasks.config import TasksConfig
from .config import MemoryConfig
from .knowledge import KnowledgeStore
from .paths import long_term_path, short_term_path
from .stm import STMStore
from .tokens import estimate, estimate_message
from .watermark import read_watermark

log = structlog.get_logger(__name__)

# Sentinel returned by ``build_memory_preamble`` when nothing should be
# injected. Callers append the preamble to the system prompt only if
# truthy (empty string is falsy).
EMPTY_PREAMBLE = ""


@dataclass(slots=True)
class WindowSlice:
    """Result of recent-window selection."""

    events: list[Event]
    estimated_tokens: int
    over_threshold: bool


# ── Preamble assembly ──────────────────────────────────────────────────────


async def build_memory_preamble(memory_dir: Path, cfg: MemoryConfig) -> str:
    """Return the ``<memory>...</memory>`` block, or ``""`` when empty.

    Sub-blocks are emitted in the order knowledge_index → long_term →
    short_term. The knowledge index (ADR-0005) is the always-injected map of
    the curated knowledge tree; its file bodies are never injected. Each block
    is omitted when its source is empty (or its ``inject*`` flag is false).

    Tasks are **not** part of memory anymore (ADR-0005) — the runtime injects a
    sibling ``<tasks>`` block via :func:`build_tasks_block`.
    """
    if not cfg.enabled:
        return EMPTY_PREAMBLE

    blocks: list[str] = []

    # ── knowledge index (the always-injected map; bodies stay on disk) ──
    if cfg.knowledge.inject_index:
        index_text = KnowledgeStore(memory_dir).index_text()
        if index_text:
            tokens = estimate(index_text)
            if tokens > cfg.knowledge.index_max_tokens:
                log.warning(
                    "knowledge index exceeds index_max_tokens",
                    tokens=tokens,
                    limit=cfg.knowledge.index_max_tokens,
                )
            blocks.append(f"<knowledge_index>\n{index_text}\n</knowledge_index>")

    # ── long-term (episodic timeline) ──────────────────────────────────
    ltm_path = long_term_path(memory_dir)
    if ltm_path.exists():
        ltm_text = ltm_path.read_text(encoding="utf-8").strip()
        if ltm_text:
            blocks.append(f"<long_term>\n{ltm_text}\n</long_term>")

    # ── short-term ─────────────────────────────────────────────────────
    stm_p = short_term_path(memory_dir)
    if stm_p.exists():
        stm_text = stm_p.read_text(encoding="utf-8").strip()
        if stm_text:
            blocks.append(f"<short_term>\n{stm_text}\n</short_term>")

    if not blocks:
        return EMPTY_PREAMBLE
    return "<memory>\n" + "\n\n".join(blocks) + "\n</memory>"


# ── Tasks block (sibling of <memory>, ADR-0005) ─────────────────────────────


def build_tasks_block(forest: TaskForest, cfg: TasksConfig) -> str:
    """Return the ``<tasks>...</tasks>`` block of pending leaf tasks, or ``""``.

    Tasks are workflow state, not memory, so this block is injected as a
    sibling of ``<memory>`` — not nested inside it. Only pending **leaves** (the
    actionable work items) are surfaced, highest priority first; internal
    orchestration nodes and completed work stay out of the window (ADR-0007).
    The forest is the runtime's live projection (folded from the event log), so
    this is pure in-memory rendering.
    """
    if not cfg.inject_pending:
        return EMPTY_PREAMBLE
    pending = forest.pending_leaves()
    # Suspended tasks are surfaced too: they only ever resume by an explicit
    # `resume`, so hiding them here would make yielded work silently vanish
    # (nobody — model or user — would ever be reminded it exists).
    suspended = sorted(forest.by_status("suspended"), key=lambda t: (t.created_at, t.id))
    if not pending and not suspended:
        return EMPTY_PREAMBLE

    def _line(t: Task) -> str:
        prio = f" (p{t.priority})" if t.priority else ""
        due = f" (due: {t.due})" if t.due else ""
        tags = "  (tags: " + ", ".join(t.tags) + ")" if t.tags else ""
        body = t.goal or t.content
        return f"- [{t.id}]{prio} {body}{due}{tags}"

    lines = [_line(t) for t in pending]
    if suspended:
        lines.append('suspended — will not run again unless resumed (task action="resume"):')
        lines.extend(_line(t) for t in suspended)
    return "<tasks>\n" + "\n".join(lines) + "\n</tasks>"


# ── Per-turn timestamp rendering (ADR-0006) ─────────────────────────────────


def format_turn_timestamp(ts: int) -> str:
    """Render an event ``ts`` (unix microseconds) as a local ``[date time ±zz]``
    tag, e.g. ``[2026-05-30 14:23 +08:00]``.
    """
    dt = datetime.fromtimestamp(ts / 1_000_000).astimezone()
    raw = dt.strftime("%z")  # e.g. "+0800"
    offset = f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else (raw or "+00:00")
    return f"[{dt.strftime('%Y-%m-%d %H:%M')} {offset}]"


def prefix_user_timestamp(content: str, ts: int | None) -> str:
    """Prefix a user message with its local datetime tag (ADR-0006).

    Render-time only: the returned string is what the LLM sees; the stored
    ``USER_MESSAGE`` payload is never modified (invariant 1 / events immutable).
    A ``None`` timestamp (e.g. an unpersisted message) is passed through as-is.
    """
    if ts is None:
        return content
    return f"{format_turn_timestamp(ts)} {content}"


# ── Recent-window selection ─────────────────────────────────────────────────


def select_recent_window(events: list[Event], cfg: MemoryConfig, watermark: int) -> WindowSlice:
    """Pick the suffix to render as raw history.

    Inputs ``events`` MUST be sorted ascending by id. The watermark, if non-
    zero, prunes events with ``id <= watermark`` — they are represented by
    STM and MUST NOT appear in the window (M-I3-adjacent invariant).
    """
    eligible = [e for e in events if (e.id or 0) > watermark]
    if not eligible:
        return WindowSlice(events=[], estimated_tokens=0, over_threshold=False)

    budget = cfg.episodic.working_memory_tokens
    min_keep = cfg.episodic.keep_recent_messages_min

    # Walk back from newest, accumulating tokens. Hard cap at 1000 to bound DB
    # work (MEMORY_SPEC §3.2 step 2).
    selected: list[Event] = []
    total = 0
    over_threshold = False
    for ev in reversed(eligible):
        cost = _event_tokens(ev)
        if len(selected) >= min_keep and total + cost > budget and len(selected) >= 1:
            over_threshold = True
            break
        selected.append(ev)
        total += cost
        if len(selected) >= 1000:
            break
    selected.reverse()

    # Boundary safety (§3.2 step 4): never start the window with a
    # tool_result/tool_error whose tool_call is older than the window. Walk
    # forward until we land on a non-tool-result kind.
    while selected and selected[0].kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR):
        selected.pop(0)

    return WindowSlice(events=selected, estimated_tokens=total, over_threshold=over_threshold)


def _event_tokens(event: Event) -> int:
    payload = event.payload
    if event.kind == EventKind.USER_MESSAGE:
        return estimate_message("user", str(payload.get("content") or ""))
    if event.kind == EventKind.ASSISTANT_MESSAGE:
        return estimate_message(
            "assistant",
            str(payload.get("content") or ""),
            tool_calls=len(payload.get("tool_calls") or []),
        )
    if event.kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR):
        return estimate_message("tool", str(payload.get("output") or ""))
    if event.kind == EventKind.TOOL_CALL:
        # tool_call payload is small structural metadata; the LLM sees it as
        # part of the parent assistant_message, so we don't double-count.
        return 0
    # Bookkeeping events (permission, log, mem_*) — invisible to LLM.
    return 0


# ── Compaction-trigger check ────────────────────────────────────────────────


def working_window_token_estimate(events: list[Event], watermark: int) -> int:
    """Tokens currently sitting between watermark and HEAD, for tier-1 trigger."""
    return sum(_event_tokens(e) for e in events if (e.id or 0) > watermark)


def chat_scope_only(events: list[Event]) -> list[Event]:
    """Keep only chat-scope events (ADR-0009 §5).

    Episodic memory is the *conversation* timeline: task-scoped turns
    (``task_id`` set) are ephemeral working context for a task, never summarized
    into STM/LTM nor counted toward the chat working-window threshold. Their
    durable residue is the task's ``result`` + checkpoint brief + the recall log.
    """
    return [e for e in events if e.task_id is None]


# ── Read-only convenience ──────────────────────────────────────────────────


def current_watermark(memory_dir: Path) -> int:
    """Thin shim so callers don't import ``watermark`` directly."""
    return read_watermark(memory_dir)


# Re-exports for the runtime/tools layer
__all__ = [
    "EMPTY_PREAMBLE",
    "STMStore",
    "WindowSlice",
    "build_memory_preamble",
    "build_tasks_block",
    "chat_scope_only",
    "current_watermark",
    "format_turn_timestamp",
    "prefix_user_timestamp",
    "select_recent_window",
    "working_window_token_estimate",
]
