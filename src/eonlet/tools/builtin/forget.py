"""forget: delete bullets from LTM (MEMORY_SPEC §5.6).

Without ``confirm=True`` returns a dry-run preview; with ``confirm=True``
performs the deletion and emits ``mem_ltm_forgotten`` (M-I7 — the dropped
content digest is preserved in the event log even after deletion).
"""

from __future__ import annotations

from pydantic import BaseModel

from ...memory.ltm import LTMStore
from ...runtime.events import mem_ltm_forgotten
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class ForgetArgs(BaseModel):
    target: str
    confirm: bool = False


@tool
class ForgetTool:
    name = "forget"
    description = (
        "Delete bullets from long-term memory (LTM). "
        "Without confirm=True: dry-run preview showing what would be deleted. "
        "With confirm=True: performs the deletion. "
        "'target' can be 'category:N' (e.g. 'fact:0') to match by index, "
        "or any text to match by partial content (case-insensitive)."
    )
    input_schema = ForgetArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: ForgetArgs, ctx: ToolContext) -> ToolResult:
        runtime = ctx.extra.get("runtime") if ctx.extra else None
        if runtime is not None:
            cfg = runtime.definition.config.memory
            if not cfg.enabled:
                return ToolResult(
                    content="forget: memory subsystem disabled in agent.yaml",
                    is_error=True,
                )

        store = LTMStore(ctx.memory_dir)
        matches = store.find_bullets(args.target)
        if not matches:
            return ToolResult(content=f"forget: no LTM bullets matched {args.target!r}")

        if not args.confirm:
            lines = [f"Dry run — would delete {len(matches)} bullet(s):"]
            for b in matches:
                lines.append(f"  [{b.section}] {b.content[:80]}")
            lines.append("\nCall with confirm=True to proceed.")
            return ToolResult(content="\n".join(lines))

        # Capture counts before mutation for the event payload.
        all_before = store.read_bullets()
        dropped_digest = [
            {"section": b.section, "preview": b.content[:80], "reason": "forget"} for b in matches
        ]
        removed = await store.delete_bullets(matches)
        kept_count = len(all_before) - removed

        if ctx.record_event is not None:
            await ctx.record_event(
                mem_ltm_forgotten(
                    kept_count=kept_count,
                    dropped_count=removed,
                    dropped_digest=dropped_digest,
                    cause="forget",
                )
            )

        return ToolResult(
            content=f"deleted {removed} bullet(s) from LTM",
            structured_output={"dropped_count": removed, "dropped": dropped_digest},
        )
