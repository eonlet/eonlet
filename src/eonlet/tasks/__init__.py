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

from .config import SchedulingConfig, TasksConfig
from .forest import (
    Task,
    TaskForest,
    TaskOrigin,
    TaskStatus,
    can_transition,
    fold_tasks,
    is_terminal,
    reduce_task,
)
from .ids import mint_task_id
from .scheduler import (
    PostRun,
    classify_post_run,
    creation_guard_error,
    next_runnable,
    preemptor,
)

__all__ = [
    "PostRun",
    "SchedulingConfig",
    "Task",
    "TaskForest",
    "TaskOrigin",
    "TaskStatus",
    "TasksConfig",
    "can_transition",
    "classify_post_run",
    "creation_guard_error",
    "fold_tasks",
    "is_terminal",
    "mint_task_id",
    "next_runnable",
    "preemptor",
    "reduce_task",
]
