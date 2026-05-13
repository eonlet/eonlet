"""Tool protocol — what every builtin and custom tool implements.

Per TOOL_SPEC §1–§4. We use Pydantic for ``input_schema`` so the framework can
emit JSON Schema for the LLM and validate inputs uniformly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    ClassVar,
    Protocol,
    runtime_checkable,
)

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


@dataclass(slots=True)
class ToolContext:
    """Passed to every tool call.

    Not a Pydantic model because some fields (CancelScope, env dict) are runtime
    objects that don't validate. Treat as a frozen-ish bag.
    """

    eonlet_id: str
    workspace: Path
    memory_dir: Path
    notes_files: list[str]  # whitelisted by agent.yaml.memory.notes_files
    skills: dict[str, Any]  # name -> Skill (for load_skill)
    env: dict[str, str]
    cancel_scope: anyio.CancelScope | None = None
    emit_event: EmitEvent | None = None
    trigger_context: dict[str, Any] | None = None
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
