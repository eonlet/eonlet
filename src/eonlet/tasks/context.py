"""Per-task prompt assembly for scheduler-driven runs (ADR-0007, M2).

When the scheduler dispatches a task, the agent needs to know *what* it is
working on. We don't open a separate long-lived conversation per task (ADR §4);
instead we assemble a focused kickoff message from the live forest — goal +
parent chain + (for a synthesis turn) the children's results. The decision
trace and resume brief travel in the system prompt (``<task_context>`` /
``<task_progress>``, rebuilt per turn), and the knowledge index is injected by
the memory preamble — so this only carries the per-dispatch specifics.

Pure function over the forest — no I/O, no LLM.
"""

from __future__ import annotations

from .forest import TaskForest, is_terminal

# Tasks-scoped runs are framed with this marker so they're distinguishable from
# ordinary user turns in the log (mirrors the ``<trigger …>`` convention).
TASK_PROMPT_TAG = "<task"


def _parent_chain(forest: TaskForest, task_id: str) -> list[str]:
    """Goals of the ancestors, nearest parent last."""
    chain: list[str] = []
    cur = forest.get(task_id)
    seen: set[str] = set()
    while cur is not None and cur.parent_id is not None and cur.parent_id not in seen:
        seen.add(cur.parent_id)
        parent = forest.get(cur.parent_id)
        if parent is None:
            break
        chain.append(parent.goal or parent.content)
        cur = parent
    chain.reverse()
    return chain


def build_task_prompt(forest: TaskForest, task_id: str) -> str:
    """Return the kickoff message seeding a task-scoped run.

    Carries the how-to-finish instruction, the goal, the parent chain, and —
    when all children are terminal — their results for the synthesis turn.
    The down-tree decision trace (``framing``) and the resume brief
    (``progress_summary``) are *not* repeated here: the runtime injects them
    into the system prompt (``<task_context>`` / ``<task_progress>``), which is
    rebuilt every turn — so they never go stale, and repeated pauses don't
    accumulate contradictory copies in the scope window.
    """
    t = forest.get(task_id)
    if t is None:
        return f'{TASK_PROMPT_TAG} id="{task_id}">\n(task no longer exists)\n</task>'

    lines = [f'{TASK_PROMPT_TAG} id="{t.id}">']
    lines.append(
        "You are now working on this task. When it is complete, call "
        'task(action="done") with a short result. If it should be broken into '
        'smaller steps, call task(action="add", content=...) for each subtask — '
        "they run before this task resumes. You need not restate the id."
    )
    lines.append("")
    lines.append(f"Goal: {t.goal or t.content}")
    if t.goal and t.content and t.content != t.goal:
        lines.append(f"Detail: {t.content}")

    chain = _parent_chain(forest, t.id)
    if chain:
        lines.append("Parent context: " + " > ".join(chain))

    children = forest.children(t.id)
    if children and all(is_terminal(c.status) for c in children):
        lines.append("")
        lines.append("Subtask results (synthesize these into the task's result):")
        for c in children:
            outcome = c.result or f"({c.status})"
            lines.append(f"- {c.goal or c.content}: {outcome}")

    lines.append("</task>")
    return "\n".join(lines)
