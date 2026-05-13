"""``load_skill`` — pull a skill's full markdown body into context."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class LoadSkillArgs(BaseModel):
    name: str = Field(description="Skill filename without .md")


@tool
class LoadSkillTool:
    name = "load_skill"
    description = "Load a skill's full content into the conversation."
    input_schema = LoadSkillArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: LoadSkillArgs, ctx: ToolContext) -> ToolResult:
        skill = ctx.skills.get(args.name)
        if skill is None:
            available = ", ".join(sorted(ctx.skills)) or "(none)"
            return ToolResult(
                content=f"unknown skill: {args.name!r}. Available: {available}",
                is_error=True,
            )
        body = getattr(skill, "body", "")
        return ToolResult(
            content=body,
            structured_output={"skill_name": args.name, "size": len(body)},
        )
