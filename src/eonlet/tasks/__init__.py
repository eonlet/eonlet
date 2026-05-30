"""Task / workflow state — an event-sourced hierarchical forest (ADR-0007).

Tasks are *things the agent will do*, not *things the agent knows* — so they
live beside ``schedule`` (the trigger/workflow surface), not under ``memory/``.

As of ADR-0007 the task source of truth is the **event log**: the live forest
is a ``fold`` of the task event family (``TASK_CREATED`` / ``UPDATED`` /
``TRANSITIONED`` / ``CHECKPOINTED`` / ``DELETED``), exactly as ``AgentState`` is
a fold of conversation events. The old ``tasks/todos.jsonl`` store is retired.
This package owns the projection (:mod:`forest`), the ``tasks:`` config block,
and id minting; the ``task`` builtin tool and the runtime's ``<tasks>``
injection consume it.
"""

from __future__ import annotations

from .config import TasksConfig
from .forest import (
    Task,
    TaskForest,
    TaskOrigin,
    TaskStatus,
    can_transition,
    fold_tasks,
    reduce_task,
)
from .ids import mint_task_id

__all__ = [
    "Task",
    "TaskForest",
    "TaskOrigin",
    "TaskStatus",
    "TasksConfig",
    "can_transition",
    "fold_tasks",
    "mint_task_id",
    "reduce_task",
]
