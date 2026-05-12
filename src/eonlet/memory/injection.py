"""Context injection (MEMORY_SPEC §3).

Builds two artifacts per LLM call:

1. **Memory preamble** — appended to the system prompt as a single
   ``<memory>...</memory>`` block containing ``<long_term>``, ``<notes>``,
   ``<todos>``, and ``<short_term>`` sub-elements (empty stores are
   omitted; the outer ``<memory>`` is omitted when all sub-stores are empty).
2. **Recent-messages window** — the suffix of the event log with
   ``id > compaction_watermark`` accumulated until ``working_memory_tokens``
   is reached, snapped to a tool_call/tool_result-safe boundary.

The whole module is **pure**: it reads from on-disk stores and the in-memory
``AgentState`` but never mutates anything. Compaction (which DOES mutate) is
in ``compactor.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..runtime.events import Event, EventKind
from .config import MemoryConfig
from .notes import NotesStore
from .paths import long_term_path, notes_path, short_term_path, todos_path
from .stm import STMStore
from .todos import TodosStore
from .tokens import estimate_message
from .watermark import read_watermark

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

    Per MEMORY_SPEC §3.1, sub-blocks are emitted in the order
    long_term → notes → todos → short_term. Each block is omitted when its
    source is empty (or ``inject: false`` for notes/todos).
    """
    if not cfg.enabled:
        return EMPTY_PREAMBLE

    blocks: list[str] = []

    # ── long-term ──────────────────────────────────────────────────────
    ltm_path = long_term_path(memory_dir)
    if ltm_path.exists():
        ltm_text = ltm_path.read_text(encoding="utf-8").strip()
        if ltm_text:
            blocks.append(f"<long_term>\n{ltm_text}\n</long_term>")

    # ── notes ──────────────────────────────────────────────────────────
    if cfg.notes.inject:
        notes_p = notes_path(memory_dir)
        if notes_p.exists():
            notes_text = notes_p.read_text(encoding="utf-8").strip()
            if notes_text:
                blocks.append(f"<notes>\n{notes_text}\n</notes>")

    # ── todos (pending only by default) ────────────────────────────────
    if cfg.todos.inject_active and todos_path(memory_dir).exists():
        todos = await TodosStore(memory_dir).list_todos(status="pending")
        if todos:
            lines = []
            for t in todos:
                due = f" (due: {t.due})" if t.due else ""
                tags = "  (tags: " + ", ".join(t.tags) + ")" if t.tags else ""
                lines.append(f"- [{t.id}] {t.content}{due}{tags}")
            blocks.append("<todos>\n" + "\n".join(lines) + "\n</todos>")

    # ── short-term ─────────────────────────────────────────────────────
    stm_p = short_term_path(memory_dir)
    if stm_p.exists():
        stm_text = stm_p.read_text(encoding="utf-8").strip()
        if stm_text:
            blocks.append(f"<short_term>\n{stm_text}\n</short_term>")

    if not blocks:
        return EMPTY_PREAMBLE
    return "<memory>\n" + "\n\n".join(blocks) + "\n</memory>"


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

    budget = cfg.conversation.working_memory_tokens
    min_keep = cfg.conversation.keep_recent_messages_min

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


# ── Read-only convenience ──────────────────────────────────────────────────


def current_watermark(memory_dir: Path) -> int:
    """Thin shim so callers don't import ``watermark`` directly."""
    return read_watermark(memory_dir)


# Re-exports for the runtime/tools layer
__all__ = [
    "EMPTY_PREAMBLE",
    "NotesStore",
    "STMStore",
    "WindowSlice",
    "build_memory_preamble",
    "current_watermark",
    "select_recent_window",
    "working_window_token_estimate",
]
