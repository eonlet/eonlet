"""remember: write a bullet directly into LTM (MEMORY_SPEC §5.2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel

from ...memory.ltm import LTMStore
from ...runtime.events import mem_remember
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class RememberArgs(BaseModel):
    content: str
    category: Literal["user", "feedback", "project", "reference", "fact"] = "fact"


@tool
class RememberTool:
    name = "remember"
    description = (
        "Write a bullet directly into long-term memory (LTM). "
        "Use this to preserve user preferences, feedback, project decisions, "
        "reference pointers, or factual observations. "
        "category: 'user' | 'feedback' | 'project' | 'reference' | 'fact'. "
        "('episodic' is reserved for auto-compaction — use 'fact' when unsure.)"
    )
    input_schema = RememberArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: RememberArgs, ctx: ToolContext) -> ToolResult:
        runtime = ctx.extra.get("runtime") if ctx.extra else None
        if runtime is not None:
            cfg = runtime.definition.config.memory
            if not cfg.enabled:
                return ToolResult(
                    content="remember: memory subsystem disabled in agent.yaml",
                    is_error=True,
                )

        today = datetime.now(UTC).date().isoformat()
        store = LTMStore(ctx.memory_dir)
        await store.append_bullet(
            section=args.category,
            content=args.content,
            src="explicit",
            ts=today,
        )

        preview = args.content[:120]
        if ctx.record_event is not None:
            await ctx.record_event(
                mem_remember(section=args.category, content_preview=preview, ts=today)
            )

        return ToolResult(
            content=f"remembered under [{args.category}]: {preview}",
            structured_output={"section": args.category, "ts": today},
        )
