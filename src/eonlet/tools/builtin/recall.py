"""recall: search the event log and memory documents (MEMORY_SPEC §5.1).

Recall is an **explicit tool**: when the agent's compressed memory isn't
enough, it calls this to "leaf through the chat history." The tool reads
from the SQLite FTS5 index (events) and the in-memory document stores
(notes/todos). Memory documents (STM/LTM) are wired in P4/P5.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...memory.notes import NotesStore
from ...memory.recall import IndexedMsg, RecallIndex
from ...memory.todos import TodosStore
from ...runtime.events import mem_recall_invoked
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

RecallScope = Literal["events", "notes", "todos", "memory"]


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
        "Use 'include' to also search notes/todos. Memory documents (STM/LTM) "
        "land in a later release."
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

        if "notes" in args.include and args.mode == "by_keyword" and args.query:
            notes = await NotesStore(ctx.memory_dir).list_notes()
            q = args.query.lower()
            matches = [n for n in notes if q in (n.title or "").lower() or q in n.body.lower()]
            if matches:
                lines = ["## notes hits"]
                for n in matches[: args.limit]:
                    head = f"### [{n.id}] {n.title or '(untitled)'}"
                    if n.tags:
                        head += "  (tags: " + ", ".join(n.tags) + ")"
                    lines.append(head + "\n" + n.body)
                sections.append("\n\n".join(lines) + "\n")
            else:
                sections.append("## notes hits — 0\n")
            total_hits += len(matches)

        if "todos" in args.include and args.mode == "by_keyword" and args.query:
            todos = await TodosStore(ctx.memory_dir).list_todos(status="all")
            q = args.query.lower()
            matches_t = [t for t in todos if q in t.content.lower()]
            if matches_t:
                lines = ["## todos hits"]
                for t in matches_t[: args.limit]:
                    lines.append(f"- [{t.status}] {t.id} — {t.content}")
                sections.append("\n".join(lines) + "\n")
            else:
                sections.append("## todos hits — 0\n")
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
