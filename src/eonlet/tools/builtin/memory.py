"""memory: agent-facing control over short/long memory (MEMORY_SPEC §5.5).

Actions:

- ``show``         — render current STM / LTM / notes / todos for inspection
- ``compact``      — force a tier-1 compaction pass right now
- ``compact_ltm``  — tier-3 (LTM forgetting); placeholder until P5 ships
- ``pause``        — disable auto-compaction for this session
- ``resume``       — re-enable auto-compaction

``compact`` runs synchronously and emits ``mem_compacted`` on success.
``pause``/``resume`` emit ``mem_paused`` / ``mem_resumed``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ...memory.compactor import LLMCompactor
from ...memory.notes import NotesStore
from ...memory.paths import long_term_path, short_term_path
from ...memory.tier1 import run_tier1
from ...memory.tier3 import run_tier3
from ...memory.todos import TodosStore
from ...runtime.events import mem_paused, mem_resumed
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

MemoryStoreName = Literal["stm", "ltm", "notes", "todos", "all"]


class MemoryArgs(BaseModel):
    action: Literal["show", "compact", "compact_ltm", "pause", "resume"]
    store: MemoryStoreName = Field(
        default="all",
        description="For action='show': which memory store(s) to print.",
    )


async def _render_show(args: MemoryArgs, ctx: ToolContext) -> str:
    chunks: list[str] = []
    md = ctx.memory_dir

    if args.store in ("stm", "all"):
        path = short_term_path(md)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        chunks.append("## short_term\n" + (text or "(empty)\n"))
    if args.store in ("ltm", "all"):
        path = long_term_path(md)
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        chunks.append("## long_term\n" + (text or "(empty)\n"))
    if args.store in ("notes", "all"):
        notes = await NotesStore(md).list_notes()
        if notes:
            lines = ["## notes"]
            for n in notes:
                head = f"### [{n.id}] {n.title or '(untitled)'}"
                if n.tags:
                    head += "  (tags: " + ", ".join(n.tags) + ")"
                lines.append(head)
                if n.body:
                    lines.append(n.body)
            chunks.append("\n".join(lines))
        else:
            chunks.append("## notes\n(empty)\n")
    if args.store in ("todos", "all"):
        todos = await TodosStore(md).list_todos(status="all")
        if todos:
            lines = ["## todos"]
            for t in todos:
                icon = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}[t.status]
                head = f"{icon} {t.id}"
                if t.due:
                    head += f"  (due: {t.due})"
                lines.append(f"{head} — {t.content}")
            chunks.append("\n".join(lines))
        else:
            chunks.append("## todos\n(empty)\n")
    return "\n\n".join(chunks).rstrip() + "\n"


@tool
class MemoryTool:
    name = "memory"
    description = (
        "Inspect and control the eonlet's memory subsystem. Actions: "
        "'show' (print STM/LTM/notes/todos — pick which via 'store'), "
        "'compact' (force a tier-1 working→STM compaction pass), "
        "'compact_ltm' (force a tier-3 LTM forgetting/pruning pass), "
        "'pause' (disable auto-compaction for this session), "
        "'resume' (re-enable auto-compaction)."
    )
    input_schema = MemoryArgs
    annotations = ToolAnnotations(destructive=False)

    async def __call__(self, args: MemoryArgs, ctx: ToolContext) -> ToolResult:
        runtime = _runtime_from_ctx(ctx)

        if args.action == "show":
            text = await _render_show(args, ctx)
            return ToolResult(content=text)

        if args.action == "compact":
            if runtime is None:
                return ToolResult(
                    content="memory compact: no live runtime in this context",
                    is_error=True,
                )
            cfg = runtime.definition.config.memory
            if not cfg.enabled:
                return ToolResult(
                    content="memory compact: subsystem disabled in agent.yaml",
                    is_error=True,
                )
            compactor = _build_compactor(runtime, cfg.compaction_model)
            outcome = await run_tier1(
                memory_dir=ctx.memory_dir,
                store=runtime.store,
                cfg=cfg,
                compactor=compactor,
                record_event=ctx.record_event,
            )
            if outcome.error:
                return ToolResult(content=f"memory compact: {outcome.error}", is_error=True)
            if not outcome.ran:
                return ToolResult(
                    content="memory compact: nothing to do (no events past watermark)"
                )
            return ToolResult(
                content=(
                    f"compacted: {outcome.tokens_before}→{outcome.tokens_after} tokens, "
                    f"{outcome.sections_added} STM sections, watermark→{outcome.boundary_event_id}"
                ),
                structured_output={
                    "sections_added": outcome.sections_added,
                    "boundary_event_id": outcome.boundary_event_id,
                    "tokens_before": outcome.tokens_before,
                    "tokens_after": outcome.tokens_after,
                },
            )

        if args.action == "compact_ltm":
            if runtime is None:
                return ToolResult(
                    content="memory compact_ltm: no live runtime in this context",
                    is_error=True,
                )
            cfg = runtime.definition.config.memory
            if not cfg.enabled:
                return ToolResult(
                    content="memory compact_ltm: subsystem disabled in agent.yaml",
                    is_error=True,
                )
            t3 = await run_tier3(
                memory_dir=ctx.memory_dir,
                cfg=cfg,
                provider=_build_provider(runtime, cfg.compaction_model),
                record_event=ctx.record_event,
            )
            if t3.error:
                return ToolResult(content=f"memory compact_ltm: {t3.error}", is_error=True)
            if not t3.ran:
                return ToolResult(content="memory compact_ltm: nothing to do (LTM under budget)")
            return ToolResult(
                content=(
                    f"LTM compacted: kept {t3.kept_count}, dropped {t3.dropped_count} bullets"
                ),
                structured_output={
                    "kept_count": t3.kept_count,
                    "dropped_count": t3.dropped_count,
                },
            )

        if args.action == "pause":
            if runtime is None:
                return ToolResult(
                    content="memory pause: no live runtime in this context", is_error=True
                )
            runtime.auto_compact_enabled = False
            if ctx.record_event is not None:
                await ctx.record_event(mem_paused())
            return ToolResult(content="auto-compaction paused")

        if args.action == "resume":
            if runtime is None:
                return ToolResult(
                    content="memory resume: no live runtime in this context", is_error=True
                )
            runtime.auto_compact_enabled = True
            if ctx.record_event is not None:
                await ctx.record_event(mem_resumed())
            return ToolResult(content="auto-compaction resumed")

        return ToolResult(content=f"memory: unknown action {args.action!r}", is_error=True)


# ── helpers ────────────────────────────────────────────────────────────────


def _runtime_from_ctx(ctx: ToolContext) -> Any | None:
    """Pull the live AgentRuntime out of ToolContext.extra if available."""
    if not ctx.extra:
        return None
    return ctx.extra.get("runtime")


def _build_provider(runtime: Any, model: str) -> Any:
    """Return an LLMProvider for *model*, reusing the agent's provider when possible."""
    from ...config import load_global_config
    from ...llm import resolve_model

    if runtime.provider.model == model:
        return runtime.provider
    return resolve_model(model, load_global_config())


def _build_compactor(runtime: Any, model: str) -> LLMCompactor:
    """Build an LLMCompactor backed by the appropriate provider."""
    return LLMCompactor(_build_provider(runtime, model))
