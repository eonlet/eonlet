"""Tool protocol — what every builtin and custom tool implements.

Per TOOL_SPEC §1–§4. We use Pydantic for ``input_schema`` so the framework can
emit JSON Schema for the LLM and validate inputs uniformly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from ..runtime.events import Event
    from ..tasks import TaskForest
    from ..triggers.scheduler import CronScheduler
    from ..web import HTTPFetcher

import anyio
from pydantic import BaseModel, ConfigDict, Field

# ── Annotations ──────────────────────────────────────────────────────────────


class ToolAnnotations(BaseModel):
    model_config = ConfigDict(frozen=True)

    read_only: bool = False
    destructive: bool = False
    network: bool = False
    requires_confirmation: bool = False
    estimated_cost_usd: float | None = None
    estimated_duration_s: float | None = None
    idempotent: bool = True


# ── Context ──────────────────────────────────────────────────────────────────


EmitEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
RecordEvent = Callable[["Event"], Awaitable["Event"]]
# Read the live task-forest projection (ADR-0007). The runtime owns the forest
# (folded from the event log) and exposes it read-only to tools so the ``task``
# tool can answer ``list`` / validate parents / read current lifecycle without
# re-reading the store. ``None`` outside the agent loop.
ReadTasks = Callable[[], "TaskForest"]


@dataclass(slots=True)
class ToolContext:
    """Passed to every tool call.

    Not a Pydantic model because some fields (CancelScope, env dict) are runtime
    objects that don't validate. Treat as a frozen-ish bag.
    """

    eonlet_id: str
    workspace: Path
    memory_dir: Path
    skills: dict[str, Any]  # name -> Skill (for load_skill)
    env: dict[str, str]
    # Workflow state dir (tasks/). Set by the worker; the `task` tool falls back
    # to the memory-dir sibling when None (standalone tests).
    tasks_dir: Path | None = None
    # Read-only accessor for the live task forest (ADR-0007). Set by the runtime;
    # ``None`` outside the agent loop (the `task` tool then can't list/transition).
    read_tasks: ReadTasks | None = None
    # The task this run is scoped to (ADR-0007 M2). Set by the scheduler while a
    # task-scoped run is in flight; the `task` tool's done/cancel/update/add then
    # default to it, so the agent says "done" without restating the id. ``None``
    # for ordinary interactive/cron turns.
    current_task_id: str | None = None
    # Anti-runaway caps for `task` creation (ADR-0007 M4). 0 = unlimited. Set by
    # the runtime from tasks.scheduling; bound subtree depth / children-per-node.
    max_task_depth: int = 0
    max_task_fanout: int = 0
    cancel_scope: anyio.CancelScope | None = None
    emit_event: EmitEvent | None = None
    # Append a memory/lifecycle event to the agent's store. Tools that mutate
    # persistent state (notes/todos/remember/forget) call this so the event
    # log stays complete. Set by the runtime; ``None`` outside the agent loop.
    record_event: RecordEvent | None = None
    trigger_context: dict[str, Any] | None = None
    scheduler: CronScheduler | None = None  # set by worker; lets schedule tool mutate triggers
    # Outbound HTTP client singleton — set by the worker for web_fetch /
    # web_search. ``None`` in standalone tool tests; tools that need it
    # assert and raise a project error on the ``None`` path.
    http_fetcher: HTTPFetcher | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ── Result ───────────────────────────────────────────────────────────────────


class ToolResult(BaseModel):
    """What the LLM sees back."""

    content: str
    is_error: bool = False
    structured_output: dict[str, Any] | None = None
    artifacts: list[str] = Field(default_factory=list)


# ── Tool protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class Tool(Protocol):
    """Structural contract for all tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    annotations: ClassVar[ToolAnnotations]

    async def __call__(self, args: BaseModel, ctx: ToolContext) -> ToolResult: ...


# ── @tool decorator ──────────────────────────────────────────────────────────


def tool(cls: type) -> type:
    """Class decorator: validate and register a tool implementation.

    The actual registration is done lazily by ``get_registry()`` discovering the
    class — the decorator just marks it. We keep it explicit (and side-effect
    free at import time) so custom tools can be imported in test contexts.
    """
    required = ("name", "description", "input_schema", "annotations")
    missing = [a for a in required if not hasattr(cls, a)]
    if missing:
        raise TypeError(f"@tool {cls.__name__}: missing class attrs {missing}")
    cls.__eonlet_tool__ = True  # type: ignore[attr-defined]
    # Self-register to the global registry so a single import call is enough.
    from .registry import get_registry

    get_registry().register(cls())
    return cls
