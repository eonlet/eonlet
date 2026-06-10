"""LLM-compressed task checkpoint brief (ADR-0009 M2).

When a task is suspended, yielded, or preempted, its resume brief
(``progress_summary``) should carry the task's **key details, events, and
decisions** — not just its last assistant message. Cognition's "Don't Build
Multi-Agents" makes this the crux of long-task continuity: a dedicated
compression step that preserves *decisions* is what lets a paused task resume
coherently (and, in M3, what feeds a child's down-tree decision trace).

This reuses the ordinary ``LLMProvider`` with a brief-specific prompt and a
plain-text output (not the JSON-section schema of ``memory/compactor.py``, which
targets episodic STM). Callers fall back to a structural summary when no provider
is available or the call fails — a checkpoint must never be blocked.

Pure except for the single provider call; no store/disk access.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ..llm import LLMMessage, LLMProvider
from ..runtime.events import Event, EventKind

log = logging.getLogger("eonlet.tasks.brief")

# Most-recent scope events fed to the brief. A checkpoint is recency-weighted
# ("where I left off"); the brief is also cumulative (``prior_brief``), so a
# long-running task's older turns survive in the rolling brief while the
# task-scoped tier-1 (ADR-0009 M4) prunes them from the live window.
_MAX_BRIEF_EVENTS = 120

BRIEF_SYSTEM_PROMPT = (
    "You write a concise RESUME BRIEF for a paused task so a later run can pick "
    "it up with full continuity.\n"
    "\n"
    "You receive the task's goal and the chronological events of its work so "
    "far. Write a short brief (at most ~200 words) capturing:\n"
    "- what has been done and the current state;\n"
    "- key decisions made and why (so a future run does not relitigate them);\n"
    "- the concrete next step(s) to resume from.\n"
    "\n"
    "Be faithful and specific — name files, values, and choices. Do not invent "
    "facts. No preamble, no JSON, no markdown headings — just the brief text."
)


def _ts(event: Event) -> str:
    if event.ts is None:
        return ""
    return datetime.fromtimestamp(event.ts / 1_000_000, tz=UTC).isoformat(timespec="seconds")


def _render(event: Event) -> str:
    """Compact one-line rendering of a task-scope event for the brief prompt."""
    p = event.payload
    ts = _ts(event)
    if event.kind == EventKind.USER_MESSAGE:
        return f"{ts} user: {p.get('content', '')}"
    if event.kind == EventKind.ASSISTANT_MESSAGE:
        body = str(p.get("content") or "")
        calls = p.get("tool_calls") or []
        if calls:
            names = ", ".join(c.get("name", "?") for c in calls)
            return f"{ts} assistant: {body}  [calls: {names}]"
        return f"{ts} assistant: {body}"
    if event.kind in (EventKind.TOOL_RESULT, EventKind.TOOL_ERROR):
        tag = "tool_error" if event.kind == EventKind.TOOL_ERROR else "tool_result"
        return f"{ts} {tag}: {p.get('output', '')}"
    return ""


def build_brief_prompt(goal: str, events: list[Event], prior_brief: str = "") -> str:
    """The user-message body sent to the brief LLM.

    ``prior_brief`` is the brief from an earlier compaction of the same task: the
    new brief is **cumulative** — it folds the prior brief plus the newly added
    events into one updated brief (ADR-0009 M4 reversible→irreversible cascade)."""
    tail = events[-_MAX_BRIEF_EVENTS:]
    lines = [ln for ln in (_render(e) for e in tail) if ln]
    body = "\n".join(lines) if lines else "(no new events)"
    prior = (
        f"Brief so far (extend, don't repeat):\n{prior_brief}\n\n" if prior_brief.strip() else ""
    )
    return f"Task goal: {goal}\n\n{prior}New work since the brief (chronological):\n{body}"


async def build_task_brief(
    provider: LLMProvider, *, goal: str, events: list[Event], prior_brief: str = ""
) -> str:
    """Compress a task's own-scope events (plus any ``prior_brief``) into a single
    cumulative resume brief. Raises on failure (the caller falls back)."""
    prompt = build_brief_prompt(goal, events, prior_brief)
    resp = await provider.complete(
        [LLMMessage(role="user", content=prompt)], system=BRIEF_SYSTEM_PROMPT, tools=None
    )
    return resp.content.strip()


# ── Down-tree decision trace (ADR-0009 M3) ───────────────────────────────────

TRACE_SYSTEM_PROMPT = (
    "You write a short CONTEXT HANDOFF for a subtask so it stays coherent with "
    "how the larger work was decomposed (without re-reading the parent's whole "
    "history).\n"
    "\n"
    "You receive the parent objective, the subtask, and the parent's recent "
    "reasoning/decisions. Summarize ONLY what the subtask needs to know to do its "
    "part coherently: the relevant decisions already made, constraints and "
    "conventions established, and the rationale behind them — so the subtask does "
    "not relitigate or contradict them.\n"
    "\n"
    "At most ~150 words. Be specific (names, choices, values). Omit anything the "
    "subtask doesn't need. No preamble, no JSON, no headings — just the handoff."
)


def build_trace_prompt(parent_goal: str, child_goal: str, events: list[Event]) -> str:
    """The user-message body sent to the decision-trace LLM."""
    tail = events[-_MAX_BRIEF_EVENTS:]
    lines = [ln for ln in (_render(e) for e in tail) if ln]
    body = "\n".join(lines) if lines else "(no recorded reasoning)"
    return (
        f"Parent objective: {parent_goal}\n"
        f"Subtask: {child_goal}\n\n"
        f"Parent's reasoning / decisions so far (chronological):\n{body}"
    )


async def build_decision_trace(
    provider: LLMProvider, *, parent_goal: str, child_goal: str, events: list[Event]
) -> str:
    """Compress a parent (or chat) scope into a subtask's down-tree decision
    trace. Raises on failure (the caller skips framing — goal-only)."""
    prompt = build_trace_prompt(parent_goal, child_goal, events)
    resp = await provider.complete(
        [LLMMessage(role="user", content=prompt)], system=TRACE_SYSTEM_PROMPT, tools=None
    )
    return resp.content.strip()


__all__ = [
    "BRIEF_SYSTEM_PROMPT",
    "TRACE_SYSTEM_PROMPT",
    "build_brief_prompt",
    "build_decision_trace",
    "build_task_brief",
    "build_trace_prompt",
]
