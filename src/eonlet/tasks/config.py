"""Schema for the top-level ``tasks:`` block in ``agent.yaml`` (ADR-0005/0007).

Was ``memory.todos`` before tasks were moved out of memory. The legacy block
is rejected by ``MemoryConfig`` (extra='forbid'); this is its new home.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SchedulingConfig(BaseModel):
    """The task-scheduler controls (ADR-0007).

    ``enabled`` gates whether the worker drives tasks autonomously (M2). The
    guard fields are validated here but only enforced in M3/M4 — they are inert
    until then, declared now so configs don't have to change later.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    # Preemption policy (M3): off | ask (DecisionBroker consent) | auto_by_priority.
    preempt: Literal["off", "ask", "auto_by_priority"] = "ask"
    # Anti-runaway guards (enforced M4).
    max_tree_depth: int = Field(default=5, ge=1)
    max_fanout: int = Field(default=12, ge=1)
    max_suspended: int = Field(default=8, ge=0)
    # 0 = inherit the agent's run budget; otherwise cap one task-scoped run.
    per_task_budget_tokens: int = Field(default=0, ge=0)
    # Anti-thrash window for preemption switches (M3).
    preempt_cooldown: str = "5m"


class TasksConfig(BaseModel):
    """Pending-task injection, archival policy, and scheduling controls."""

    model_config = ConfigDict(extra="forbid")

    inject_pending: bool = True
    # 0 disables archival; otherwise done items older than N days are moved to
    # tasks/todos.archive.jsonl on the periodic sweep.
    archive_done_after_days: int = Field(default=30, ge=0)
    scheduling: SchedulingConfig = Field(default_factory=SchedulingConfig)
