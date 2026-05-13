"""``sleep`` tool — pause execution, capped at 5 minutes."""

from __future__ import annotations

import anyio
from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class SleepArgs(BaseModel):
    seconds: float = Field(ge=0, le=300)


@tool
class SleepTool:
    name = "sleep"
    description = "Pause execution for N seconds (useful for retry backoff). Capped at 300s."
    input_schema = SleepArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: SleepArgs, ctx: ToolContext) -> ToolResult:
        await anyio.sleep(args.seconds)
        return ToolResult(content=f"slept {args.seconds}s")
