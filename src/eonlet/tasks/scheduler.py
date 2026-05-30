"""Task scheduling — selection + post-run classification (ADR-0007, M2).

The agent is a single human-like worker: at most one task runs at a time, and
the scheduler decides *which*. This module is the **deterministic core** — pure
functions over a :class:`~eonlet.tasks.forest.TaskForest`, no I/O, no LLM. The
worker loop (M2 integration) calls :func:`next_runnable` to pick the task,
drives a task-scoped agent run, then calls :func:`classify_post_run` to decide
the next lifecycle move.

Runnability
-----------
A node is **runnable** this beat when it is either:

- a ``pending`` **leaf** — execute it (do the concrete work), or
- a ``blocked`` node whose children are **all terminal** — execute it (the
  synthesis turn that folds the children's results into a result).

Selection walks the forest depth-first, highest-priority root tree first and
highest-priority sibling first, returning the first runnable node. The active
root→leaf path is the *spine*; exactly one spine runs at a time (the worker's
single-consumer loop already guarantees serial execution).
"""

from __future__ import annotations

from enum import StrEnum

from .forest import Task, TaskForest, is_terminal


def _ordered_children(forest: TaskForest, task_id: str) -> list[Task]:
    return sorted(forest.children(task_id), key=lambda t: (-t.priority, t.created_at, t.id))


def _runnable_in_subtree(forest: TaskForest, node: Task, exclude_id: str | None) -> Task | None:
    if node.id == exclude_id:
        # Skip the excluded task and its whole subtree (used by preemption so a
        # running task isn't "preempted" by one of its own subtasks).
        return None
    if is_terminal(node.status):
        return None
    children = _ordered_children(forest, node.id)
    if not children:
        # Leaf: only pending leaves are runnable. active/suspended/blocked leaves
        # are not picked up here (suspended resumes are a manual / M3 concern).
        return node if node.status == "pending" else None
    # Has children — descend depth-first to the highest-priority runnable one.
    for child in children:
        found = _runnable_in_subtree(forest, child, exclude_id)
        if found is not None:
            return found
    # No runnable descendant. If every child is terminal, this node is ready for
    # its synthesis turn (covers both the normal `blocked` parent and the
    # defensive `pending` parent case).
    if node.status in ("blocked", "pending") and all(is_terminal(c.status) for c in children):
        return node
    return None


def next_runnable(forest: TaskForest, *, exclude_id: str | None = None) -> Task | None:
    """The single task the agent should work on next, or ``None`` if idle.

    Highest-priority root tree first; within a tree, depth-first through
    highest-priority siblings to the first runnable node. ``exclude_id`` skips a
    task and its subtree (preemption asks "what else is runnable besides me?").
    """
    for root in forest.roots():  # already priority-ordered
        found = _runnable_in_subtree(forest, root, exclude_id)
        if found is not None:
            return found
    return None


def preemptor(forest: TaskForest, current: Task) -> Task | None:
    """A runnable task that should preempt ``current``, or ``None``.

    A contender preempts only if it is **strictly higher priority** than the
    running task and lies outside the running task's own subtree. (Equal
    priority never preempts — that would let two same-priority tasks thrash.)
    """
    contender = next_runnable(forest, exclude_id=current.id)
    if contender is None:
        return None
    return contender if contender.priority > current.priority else None


def synthesis_ready(forest: TaskForest, task_id: str) -> bool:
    """True if ``task_id`` is a parent whose children are all terminal."""
    children = forest.children(task_id)
    return bool(children) and all(is_terminal(c.status) for c in children)


class PostRun(StrEnum):
    """What a task-scoped run did, inferred from the forest afterward."""

    DONE = "done"  # agent marked the task done/cancelled (terminal)
    DECOMPOSED = "decomposed"  # agent added children → block on them
    YIELDED = "yielded"  # ran but neither finished nor decomposed → checkpoint+suspend
    GONE = "gone"  # task was deleted mid-run


def classify_post_run(forest: TaskForest, task_id: str) -> PostRun:
    """Inspect the forest after a task-scoped run to decide the next move.

    The agent signals its outcome through the ordinary ``task`` tool during the
    run; the scheduler reads the resulting state rather than relying on a return
    value.
    """
    t = forest.get(task_id)
    if t is None:
        return PostRun.GONE
    if is_terminal(t.status):
        return PostRun.DONE
    if forest.children(task_id):
        return PostRun.DECOMPOSED
    return PostRun.YIELDED
