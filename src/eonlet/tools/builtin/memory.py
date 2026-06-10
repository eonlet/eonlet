"""memory: agent-facing control over short/long memory (MEMORY_SPEC §5.5).

Actions:

- ``show``            — render current STM / LTM for inspection
- ``compact``         — force a tier-1 compaction pass right now
- ``compact_ltm``     — force a tier-3 (episodic-LTM forgetting) pass
- ``propose_compact`` — propose a semantic compaction to the user (ADR-0006)
- ``pause``           — disable auto-compaction for this session
- ``resume``          — re-enable auto-compaction

``compact`` runs synchronously and emits ``mem_compacted`` on success.
``propose_compact`` blocks on the user's consent (decision broker) unless the
agent runs in ``yolo`` mode, then emits ``mem_compact_proposed`` plus
``mem_compact_approved``/``mem_compact_declined``.
``pause``/``resume`` emit ``mem_paused`` / ``mem_resumed``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ...memory.compactor import LLMCompactor
from ...memory.injection import chat_scope_only, working_window_token_estimate
from ...memory.paths import long_term_path, short_term_path
from ...memory.tier1 import run_tier1
from ...memory.tier3 import run_tier3
from ...memory.watermark import read_watermark
from ...runtime.events import (
    EventKind,
    mem_compact_approved,
    mem_compact_declined,
    mem_compact_proposed,
    mem_paused,
    mem_resumed,
    now_us,
)
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

MemoryStoreName = Literal["stm", "ltm", "all"]


class MemoryArgs(BaseModel):
    action: Literal["show", "compact", "compact_ltm", "pause", "resume", "propose_compact"]
    store: MemoryStoreName = Field(
        default="all",
        description="For action='show': which memory store(s) to print.",
    )
    boundary_event_id: int | None = Field(
        default=None,
        description=(
            "For action='propose_compact': fold everything up to and including this "
            "event id into short-term memory, keeping more recent events raw. Pick the "
            "point where the conversation moved on from older, now-irrelevant context."
        ),
    )
    reason: str | None = Field(
        default=None,
        description="For action='propose_compact': why the older context can be folded away.",
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
    return "\n\n".join(chunks).rstrip() + "\n"


@tool
class MemoryTool:
    name = "memory"
    description = (
        "Inspect and control the eonlet's memory subsystem. Actions: "
        "'show' (print STM/LTM — pick which via 'store'), "
        "'compact' (force a tier-1 working→STM compaction pass), "
        "'compact_ltm' (force a tier-3 LTM forgetting/pruning pass), "
        "'propose_compact' (propose folding away older context up to "
        "'boundary_event_id' with a 'reason'; the user must approve before it "
        "happens — use when the conversation has clearly moved on), "
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

        if args.action == "propose_compact":
            return await _handle_propose_compact(args, ctx, runtime)

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


# ── propose_compact (ADR-0006 M3) ────────────────────────────────────────────


def _propose_guards(runtime: Any, ctx: ToolContext) -> tuple[int, str | None]:
    """Evaluate the floor + cooldown guards.

    Returns ``(working_tokens, blocked_reason)``. ``blocked_reason`` is ``None``
    when a proposal is allowed, otherwise a short human reason the model can act
    on (e.g. raise the boundary, or just wait).
    """
    ep = runtime.definition.config.memory.episodic
    watermark = read_watermark(ctx.memory_dir)
    # Semantic-compaction proposals concern the chat conversation, so the
    # working-memory estimate counts chat-scope turns only (ADR-0009 §5).
    events = chat_scope_only(runtime.store.read(since=watermark))
    working = working_window_token_estimate(events, watermark=0)
    if working < ep.propose_floor_tokens:
        return working, (
            f"working memory is only {working} tokens (< floor {ep.propose_floor_tokens}); "
            "nothing worth folding yet"
        )
    compactions = [e for e in events if e.kind == EventKind.MEM_COMPACTED]
    if compactions:
        last = max(compactions, key=lambda e: e.id or 0)
        elapsed = (now_us() - last.ts) / 1_000_000
        if elapsed < ep.propose_min_interval_seconds:
            return working, (
                f"only {int(elapsed)}s since the last compaction "
                f"(cooldown {ep.propose_min_interval_seconds}s)"
            )
        turns = sum(
            1 for e in events if e.kind == EventKind.USER_MESSAGE and (e.id or 0) > (last.id or 0)
        )
        if turns < ep.propose_min_turns:
            return working, (
                f"only {turns} turn(s) since the last compaction "
                f"(cooldown {ep.propose_min_turns} turns)"
            )
    return working, None


async def _handle_propose_compact(
    args: MemoryArgs, ctx: ToolContext, runtime: Any | None
) -> ToolResult:
    if runtime is None:
        return ToolResult(content="propose_compact: no live runtime in this context", is_error=True)
    cfg = runtime.definition.config.memory
    if not cfg.enabled:
        return ToolResult(
            content="propose_compact: subsystem disabled in agent.yaml", is_error=True
        )
    if not cfg.episodic.propose_semantic:
        return ToolResult(
            content="propose_compact: disabled (memory.episodic.propose_semantic=false)"
        )

    boundary = args.boundary_event_id
    reason = (args.reason or "").strip()
    watermark = read_watermark(ctx.memory_dir)
    latest = runtime.store.latest_id()
    if boundary is None or not (watermark < boundary <= latest):
        return ToolResult(
            content=(
                f"propose_compact: boundary_event_id must be an event id in "
                f"({watermark}, {latest}] — got {boundary!r}"
            ),
            is_error=True,
        )

    working, blocked = _propose_guards(runtime, ctx)
    if blocked is not None:
        return ToolResult(content=f"propose_compact: not now — {blocked}")

    mode = runtime.gate.mode
    broker = runtime.decision_broker
    # Interactive-only unless yolo: in a headless/cron run there is no one to
    # approve, so the proposal is a clean no-op (no events recorded).
    if mode != "yolo" and (broker is None or not broker.has_listener()):
        return ToolResult(content="propose_compact: no interactive session attached; skipped")

    if ctx.record_event is not None:
        await ctx.record_event(
            mem_compact_proposed(boundary_event_id=boundary, reason=reason, working_tokens=working)
        )

    if mode == "yolo":
        approved, rule = True, "yolo"
    else:
        choice = await broker.ask(
            kind="compaction",
            prompt=f"Fold away the conversation up to #{boundary}? {reason}".strip(),
            options=["approve", "deny"],
            payload={"boundary_event_id": boundary, "reason": reason},
            decline="deny",
        )
        approved, rule = (choice == "approve"), "user"

    if not approved:
        if ctx.record_event is not None:
            await ctx.record_event(mem_compact_declined(boundary_event_id=boundary, rule=rule))
        return ToolResult(content="propose_compact: declined — keeping the full window")

    if ctx.record_event is not None:
        await ctx.record_event(mem_compact_approved(boundary_event_id=boundary, rule=rule))
    compactor = _build_compactor(runtime, cfg.compaction_model)
    outcome = await run_tier1(
        memory_dir=ctx.memory_dir,
        store=runtime.store,
        cfg=cfg,
        compactor=compactor,
        record_event=ctx.record_event,
        boundary=boundary,
    )
    if outcome.error:
        return ToolResult(content=f"propose_compact: {outcome.error}", is_error=True)
    if not outcome.ran:
        return ToolResult(content="propose_compact: approved but nothing to compact")
    return ToolResult(
        content=(
            f"compacted up to #{outcome.boundary_event_id}: "
            f"{outcome.tokens_before}→{outcome.tokens_after} tokens, "
            f"{outcome.sections_added} STM sections"
        ),
        structured_output={
            "approved": True,
            "rule": rule,
            "boundary_event_id": outcome.boundary_event_id,
            "sections_added": outcome.sections_added,
            "tokens_before": outcome.tokens_before,
            "tokens_after": outcome.tokens_after,
        },
    )


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
