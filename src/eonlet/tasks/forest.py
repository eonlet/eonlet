"""The task forest — an event-sourced projection (ADR-0007, M1).

Tasks are no longer a flat ``todos.jsonl`` store. They are a ``fold`` of the
task event family, exactly as ``AgentState`` is a fold of the conversation
events (``runtime/state.py``). The event log is the single source of truth
(Invariant #1); this module rebuilds the live forest from it.

A task is a node in a tree (``parent_id`` + creation order); independent root
trees form a forest, traversed depth-first. Lifecycle is
``pending → active → suspended / blocked → done / cancelled``; the manual
``task`` tool drives the pending/done/cancelled subset, the scheduler (M2) the
rest.

The reducer is **total and defensive**: an event that references a missing
node, a duplicate id, or an illegal lifecycle transition is logged and skipped,
never fatal to replay (mirrors ``Task.from_json`` defensiveness in the old
store).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..runtime.events import Event

log = logging.getLogger(__name__)

TaskStatus = Literal["pending", "active", "suspended", "blocked", "done", "cancelled"]
TaskOrigin = Literal["user", "agent", "trigger"]

_TERMINAL: frozenset[str] = frozenset({"done", "cancelled"})

# Permissive among the non-terminal states (the scheduler needs most of them);
# the one hard rule is that terminal states are sticky.
_ALLOWED: dict[str, frozenset[str]] = {
    "pending": frozenset({"active", "suspended", "blocked", "done", "cancelled"}),
    "active": frozenset({"pending", "suspended", "blocked", "done", "cancelled"}),
    "suspended": frozenset({"pending", "active", "blocked", "done", "cancelled"}),
    "blocked": frozenset({"pending", "active", "suspended", "done", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    """True if a task may move from ``src`` to ``dst``. ``src == dst`` is a no-op."""
    if src == dst:
        return True
    return dst in _ALLOWED.get(src, frozenset())


def is_terminal(status: str) -> bool:
    """True for ``done`` / ``cancelled`` — states a task never leaves."""
    return status in _TERMINAL


def _iso(ts_us: int) -> str:
    return datetime.fromtimestamp(ts_us / 1_000_000).astimezone().isoformat(timespec="seconds")


@dataclass(slots=True)
class Task:
    """One node in the forest. Child ordering is held by the forest, not here."""

    id: str
    content: str
    goal: str = ""
    status: TaskStatus = "pending"
    priority: int = 0
    parent_id: str | None = None
    origin: TaskOrigin = "agent"
    progress_summary: str = ""
    result: str = ""
    created_at: str = ""
    done_at: str | None = None
    due: str | None = None
    tags: list[str] = field(default_factory=list)
    # Optional cron/at spec — stored from M1, the trigger bridge wires it in M3.
    schedule: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "goal": self.goal,
            "status": self.status,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "origin": self.origin,
            "progress_summary": self.progress_summary,
            "result": self.result,
            "created_at": self.created_at,
            "done_at": self.done_at,
            "due": self.due,
            "tags": list(self.tags),
            "schedule": self.schedule,
        }


class TaskForest:
    """Live projection: nodes by id (insertion = creation order) + child links."""

    def __init__(self) -> None:
        self._nodes: dict[str, Task] = {}
        self._children: dict[str, list[str]] = {}

    # ── reads ────────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._nodes

    def get(self, task_id: str) -> Task | None:
        return self._nodes.get(task_id)

    def all_tasks(self) -> list[Task]:
        return list(self._nodes.values())

    def children(self, task_id: str) -> list[Task]:
        return [self._nodes[c] for c in self._children.get(task_id, []) if c in self._nodes]

    def is_leaf(self, task_id: str) -> bool:
        return not self.children(task_id)

    def depth(self, task_id: str) -> int:
        """1-based depth of a node (a root is depth 1). 0 if unknown."""
        node = self.get(task_id)
        if node is None:
            return 0
        d = 1
        seen: set[str] = set()
        while node.parent_id is not None and node.parent_id not in seen:
            parent = self.get(node.parent_id)
            if parent is None:
                break
            seen.add(node.parent_id)
            d += 1
            node = parent
        return d

    def _is_root(self, t: Task) -> bool:
        # A node is a root if it has no parent, or its parent has been deleted
        # (orphans surface as roots rather than vanishing).
        return t.parent_id is None or t.parent_id not in self._nodes

    def roots(self) -> list[Task]:
        roots = [t for t in self._nodes.values() if self._is_root(t)]
        return sorted(roots, key=lambda t: (-t.priority, t.created_at, t.id))

    def by_status(self, status: TaskStatus | Literal["all"]) -> list[Task]:
        if status == "all":
            return self.all_tasks()
        return [t for t in self._nodes.values() if t.status == status]

    def pending_leaves(self) -> list[Task]:
        """Pending tasks with no children — the actionable work items.

        Higher priority first; ties broken by creation order. This is what the
        ``<tasks>`` injection surfaces.
        """
        leaves = [t for t in self._nodes.values() if t.status == "pending" and self.is_leaf(t.id)]
        return sorted(leaves, key=lambda t: (-t.priority, t.created_at, t.id))

    def dfs(self) -> Iterator[tuple[Task, int]]:
        """Pre-order (task, depth) over the whole forest, priority-ordered roots."""

        def walk(node: Task, depth: int) -> Iterator[tuple[Task, int]]:
            yield node, depth
            for child in self.children(node.id):
                yield from walk(child, depth + 1)

        for root in self.roots():
            yield from walk(root, 0)

    # ── mutation (reducer-internal) ──────────────────────────────────────────

    def _add(self, task: Task) -> None:
        if task.id in self._nodes:
            log.warning("task forest: duplicate TASK_CREATED for %s; ignoring", task.id)
            return
        self._nodes[task.id] = task
        self._children.setdefault(task.id, [])
        if task.parent_id is not None:
            self._children.setdefault(task.parent_id, []).append(task.id)

    def _remove(self, task_id: str) -> None:
        task = self._nodes.pop(task_id, None)
        if task is None:
            return
        # Detach from parent's child list; children of the removed node are left
        # in place and surface as roots (see ``_is_root``).
        if task.parent_id is not None:
            siblings = self._children.get(task.parent_id)
            if siblings and task_id in siblings:
                siblings.remove(task_id)
        self._children.pop(task_id, None)


def reduce_task(forest: TaskForest, event: Event) -> TaskForest:
    """Apply one event to the forest in place, returning it (mirrors state.reduce)."""
    from ..runtime.events import EventKind

    kind = event.kind
    p = event.payload

    if kind == EventKind.TASK_CREATED:
        forest._add(
            Task(
                id=str(p["id"]),
                content=str(p.get("content", "")),
                goal=str(p.get("goal", "")),
                status="pending",
                priority=int(p.get("priority", 0) or 0),
                parent_id=(str(p["parent_id"]) if p.get("parent_id") else None),
                origin=_origin(p.get("origin")),
                created_at=_iso(event.ts),
                due=_opt_str(p.get("due")),
                tags=_str_list(p.get("tags")),
                schedule=_opt_str(p.get("schedule")),
            )
        )
    elif kind == EventKind.TASK_UPDATED:
        t = forest.get(str(p.get("id", "")))
        if t is None:
            log.warning("task forest: TASK_UPDATED for missing %s; ignoring", p.get("id"))
            return forest
        if p.get("content") is not None:
            t.content = str(p["content"])
        if p.get("goal") is not None:
            t.goal = str(p["goal"])
        if p.get("priority") is not None:
            t.priority = int(p["priority"])
        if p.get("due") is not None:
            t.due = _opt_str(p["due"])
        if p.get("tags") is not None:
            t.tags = _str_list(p["tags"])
    elif kind == EventKind.TASK_TRANSITIONED:
        t = forest.get(str(p.get("id", "")))
        if t is None:
            log.warning("task forest: TASK_TRANSITIONED for missing %s; ignoring", p.get("id"))
            return forest
        dst = str(p.get("to_state", ""))
        if dst not in _ALLOWED:
            log.warning("task forest: unknown to_state %r for %s; ignoring", dst, t.id)
            return forest
        if not can_transition(t.status, dst):
            log.warning(
                "task forest: illegal transition %s→%s for %s; ignoring", t.status, dst, t.id
            )
            return forest
        t.status = dst  # type: ignore[assignment]
        if dst == "done":
            t.done_at = _iso(event.ts)
        result = p.get("result")
        if result is not None:
            t.result = str(result)
    elif kind == EventKind.TASK_CHECKPOINTED:
        t = forest.get(str(p.get("id", "")))
        if t is None:
            log.warning("task forest: TASK_CHECKPOINTED for missing %s; ignoring", p.get("id"))
            return forest
        t.progress_summary = str(p.get("progress_summary", ""))
    elif kind == EventKind.TASK_DELETED:
        forest._remove(str(p.get("id", "")))
    # Any non-task event is a no-op.
    return forest


def fold_tasks(events: list[Event]) -> TaskForest:
    """Rebuild the forest from a full event list (mirrors state.fold)."""
    forest = TaskForest()
    for ev in events:
        reduce_task(forest, ev)
    return forest


def _origin(v: object) -> TaskOrigin:
    s = str(v) if v is not None else "agent"
    return s if s in ("user", "agent", "trigger") else "agent"  # type: ignore[return-value]


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s or None


def _str_list(v: object) -> list[str]:
    return [str(x) for x in v] if isinstance(v, list) else []
