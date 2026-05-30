"""recall: search the event log and memory stores (MEMORY_SPEC §5.1 / ADR-0005).

Recall is an **explicit tool**: when the agent's compressed memory isn't
enough, it calls this to "leaf through the chat history." The tool reads from
the SQLite FTS5 index (events) and scans the knowledge tree / tasks directly.
When a knowledge file matches, recall returns its path so the agent can
``knowledge.open`` the full body.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...memory.knowledge import KnowledgeStore
from ...memory.recall import IndexedMsg, RecallIndex
from ...runtime.events import mem_recall_invoked
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

RecallScope = Literal["events", "knowledge", "tasks"]


def _default_include() -> list[RecallScope]:
    return ["events"]


class RecallArgs(BaseModel):
    mode: Literal["by_keyword", "by_date", "by_date_range", "around_event"]
    query: str | None = Field(default=None, description="Search term for mode='by_keyword'.")
    date: str | None = Field(
        default=None,
        description="YYYY-MM-DD (UTC) for mode='by_date'.",
    )
    date_range: tuple[str, str] | None = Field(
        default=None,
        description="(start, end) ISO datetimes for mode='by_date_range'.",
    )
    around_event_id: int | None = Field(
        default=None, description="Center event id for mode='around_event'."
    )
    context_radius: int = Field(default=5, ge=0, le=200)
    limit: int = Field(default=20, ge=1, le=500)
    include: list[RecallScope] = Field(default_factory=_default_include)


def _render_hits(label: str, hits: list[IndexedMsg]) -> str:
    if not hits:
        return f"## {label} — 0 hits\n"
    out = [f"## {label} — {len(hits)} hits"]
    for h in hits:
        head = f"### [{h.iso_ts} #{h.event_id}] {h.role} ({h.kind})"
        # Cap individual hit body so a single huge tool_result doesn't
        # swamp the recall window. The full event is still on disk; an
        # around_event call can retrieve more.
        body = h.content if len(h.content) <= 600 else h.content[:600] + " …(truncated)"
        out.append(head + "\n" + body)
    return "\n\n".join(out) + "\n"


@tool
class RecallTool:
    name = "recall"
    description = (
        "Search the eonlet's full event log and memory documents when "
        "summarized memory is not enough. Modes: 'by_keyword' (FTS over message "
        "text; requires query), 'by_date' (events on YYYY-MM-DD UTC; requires date), "
        "'by_date_range' (between two ISO datetimes; requires date_range), "
        "'around_event' (radius of events around an id; requires around_event_id). "
        "Use 'include' to also search the knowledge base / tasks; knowledge hits "
        "return file paths you can open with the knowledge tool."
    )
    input_schema = RecallArgs
    annotations = ToolAnnotations(read_only=True)

    async def __call__(self, args: RecallArgs, ctx: ToolContext) -> ToolResult:
        idx: RecallIndex | None = None
        if ctx.extra:
            maybe = ctx.extra.get("recall_index")
            if isinstance(maybe, RecallIndex):
                idx = maybe
        if idx is None:
            return ToolResult(content="recall: index not available in this context", is_error=True)

        sections: list[str] = []
        event_hits: list[IndexedMsg] = []
        total_hits = 0

        if "events" in args.include:
            try:
                if args.mode == "by_keyword":
                    if not args.query:
                        return ToolResult(
                            content="recall: 'query' required for mode=by_keyword",
                            is_error=True,
                        )
                    event_hits = idx.search_keyword(args.query, limit=args.limit)
                    sections.append(_render_hits(f'by_keyword "{args.query}"', event_hits))
                elif args.mode == "by_date":
                    if not args.date:
                        return ToolResult(
                            content="recall: 'date' required for mode=by_date", is_error=True
                        )
                    event_hits = idx.events_on_date(args.date, limit=args.limit)
                    sections.append(_render_hits(f"by_date {args.date}", event_hits))
                elif args.mode == "by_date_range":
                    if not args.date_range:
                        return ToolResult(
                            content="recall: 'date_range' required for mode=by_date_range",
                            is_error=True,
                        )
                    start, end = args.date_range
                    event_hits = idx.events_in_range(start, end, limit=args.limit)
                    sections.append(_render_hits(f"by_date_range {start} → {end}", event_hits))
                elif args.mode == "around_event":
                    if args.around_event_id is None:
                        return ToolResult(
                            content="recall: 'around_event_id' required for mode=around_event",
                            is_error=True,
                        )
                    event_hits = idx.around_event(args.around_event_id, radius=args.context_radius)
                    sections.append(
                        _render_hits(
                            f"around_event #{args.around_event_id} ±{args.context_radius}",
                            event_hits,
                        )
                    )
            except ValueError as e:
                return ToolResult(content=f"recall: {e}", is_error=True)
            total_hits += len(event_hits)

        if "knowledge" in args.include and args.mode == "by_keyword" and args.query:
            store = KnowledgeStore(ctx.memory_dir)
            q = args.query.lower()
            k_matches: list[tuple[str, str]] = []  # (path, snippet)
            for entry in await store.list_entries():
                body = await store.open(entry.path) or ""
                hay = f"{entry.title}\n{entry.hook}\n{body}".lower()
                if q in hay:
                    snippet = entry.hook or body.strip().splitlines()[0] if body.strip() else ""
                    k_matches.append((entry.path, snippet))
            if k_matches:
                lines = ["## knowledge hits"]
                for path, snippet in k_matches[: args.limit]:
                    lines.append(f"- {path} — {snippet}" if snippet else f"- {path}")
                lines.append("\nOpen any of these with the knowledge tool to read the full body.")
                sections.append("\n".join(lines) + "\n")
            else:
                sections.append("## knowledge hits — 0\n")
            total_hits += len(k_matches)

        if (
            "tasks" in args.include
            and args.mode == "by_keyword"
            and args.query
            and ctx.read_tasks is not None
        ):
            tasks = ctx.read_tasks().all_tasks()
            q = args.query.lower()
            matches_t = [t for t in tasks if q in t.content.lower() or q in t.goal.lower()]
            if matches_t:
                lines = ["## tasks hits"]
                for t in matches_t[: args.limit]:
                    lines.append(f"- [{t.status}] {t.id} — {t.content}")
                sections.append("\n".join(lines) + "\n")
            else:
                sections.append("## tasks hits — 0\n")
            total_hits += len(matches_t)

        if ctx.record_event is not None:
            await ctx.record_event(
                mem_recall_invoked(
                    mode=args.mode,
                    hits=total_hits,
                    query=args.query,
                    date=args.date,
                )
            )

        if not sections:
            return ToolResult(content="recall: no scopes requested")
        return ToolResult(content="\n".join(sections).rstrip() + "\n")
