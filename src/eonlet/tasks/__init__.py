"""Task / workflow state (ADR-0005).

Tasks are *things the agent will do*, not *things the agent knows* — so they
live beside ``schedule`` (the trigger/workflow surface), not under ``memory/``.
This package owns the ``tasks/todos.jsonl`` store, the ``tasks:`` config block,
and id minting; the ``task`` builtin tool and the runtime's ``<tasks>``
injection consume it.
"""

from __future__ import annotations

from .config import TasksConfig
from .ids import mint_task_id
from .store import Task, TaskStatus, TaskStore, todos_archive_path, todos_path

__all__ = [
    "Task",
    "TaskStatus",
    "TaskStore",
    "TasksConfig",
    "mint_task_id",
    "todos_archive_path",
    "todos_path",
]
