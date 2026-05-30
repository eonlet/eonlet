"""Schema for the top-level ``tasks:`` block in ``agent.yaml`` (ADR-0005).

Was ``memory.todos`` before tasks were moved out of memory. The legacy block
is rejected by ``MemoryConfig`` (extra='forbid'); this is its new home.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TasksConfig(BaseModel):
    """Pending-task injection and archival policy."""

    model_config = ConfigDict(extra="forbid")

    inject_pending: bool = True
    # 0 disables archival; otherwise done items older than N days are moved to
    # tasks/todos.archive.jsonl on the periodic sweep.
    archive_done_after_days: int = Field(default=30, ge=0)
