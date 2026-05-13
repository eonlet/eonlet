"""notes_read / notes_append: bounded to the eonlet's memory_dir."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class NotesReadArgs(BaseModel):
    file: str = Field(default="notes.md", description="Filename in memory/. No path traversal.")


@tool
class NotesReadTool:
    name = "notes_read"
    description = "Read one of the eonlet's notes files from memory/."
    input_schema = NotesReadArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: NotesReadArgs, ctx: ToolContext) -> ToolResult:
        err = _check_whitelist(args.file, ctx)
        if err:
            return err
        p = ctx.memory_dir / args.file
        if not p.exists():
            return ToolResult(content="(empty)")
        return ToolResult(content=p.read_text(encoding="utf-8"))


class NotesAppendArgs(BaseModel):
    file: str = Field(default="notes.md")
    content: str
    with_timestamp: bool = True


@tool
class NotesAppendTool:
    name = "notes_append"
    description = (
        "Append to one of the eonlet's notes files (memory/). Optional timestamped header."
    )
    input_schema = NotesAppendArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: NotesAppendArgs, ctx: ToolContext) -> ToolResult:
        err = _check_whitelist(args.file, ctx)
        if err:
            return err
        p = ctx.memory_dir / args.file
        p.parent.mkdir(parents=True, exist_ok=True)
        body = args.content
        if args.with_timestamp:
            stamp = time.strftime("## %Y-%m-%d %H:%M\n")
            body = stamp + body.rstrip() + "\n\n"
        with p.open("a", encoding="utf-8") as f:
            n = f.write(body)
        return ToolResult(content=f"appended {n} bytes to {p.name}")


def _check_whitelist(name: str, ctx: ToolContext) -> ToolResult | None:
    if "/" in name or ".." in name:
        return ToolResult(content=f"invalid notes filename: {name!r}", is_error=True)
    if ctx.notes_files and name not in ctx.notes_files:
        return ToolResult(
            content=(
                f"notes file {name!r} not declared in agent.yaml.memory.notes_files "
                f"(allowed: {ctx.notes_files})"
            ),
            is_error=True,
        )
    return None
