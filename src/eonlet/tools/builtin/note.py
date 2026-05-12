"""note: action-style notes management.

Per MEMORY_SPEC §5.3. Replaces the legacy ``notes_read`` / ``notes_append``
tools. Notes are user-curated explicit knowledge — the auto-compaction
pipeline never deletes them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...memory.ids import mint_note_id
from ...memory.notes import Note, NotesStore
from ...runtime.events import mem_note_added, mem_note_deleted, mem_note_updated
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class NoteArgs(BaseModel):
    action: Literal["add", "list", "get", "update", "delete"]
    id: str | None = Field(
        default=None,
        description="Note id (required for get/update/delete).",
    )
    title: str | None = Field(default=None, description="Optional title for action='add'.")
    content: str | None = Field(default=None, description="Body text (required for add/update).")
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "For 'add': tags attached to the note. "
            "For 'list': filter — return notes that share at least one tag."
        ),
    )


def _render(n: Note) -> str:
    head = f"[{n.id}]"
    if n.title:
        head += f" {n.title}"
    if n.tags:
        head += "  (tags: " + ", ".join(n.tags) + ")"
    if n.created_at:
        head += f"  @ {n.created_at}"
    return head + ("\n" + n.body if n.body else "")


@tool
class NoteTool:
    name = "note"
    description = (
        "User-curated persistent notes (never auto-deleted). Actions: "
        "'add' (content required; optional title/tags), "
        "'list' (optional tags filter), "
        "'get' (by id), "
        "'update' (id + content), "
        "'delete' (id)."
    )
    input_schema = NoteArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: NoteArgs, ctx: ToolContext) -> ToolResult:
        store = NotesStore(ctx.memory_dir)

        if args.action == "add":
            if not args.content:
                return ToolResult(content="note add: 'content' is required", is_error=True)
            note_id = mint_note_id()
            try:
                note = await store.add(
                    id=note_id, content=args.content, title=args.title, tags=args.tags
                )
            except ValueError as e:
                return ToolResult(content=f"note add: {e}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(mem_note_added(id=note.id, title=note.title, tags=note.tags))
            return ToolResult(content=f"added {note.id}", structured_output={"id": note.id})

        if args.action == "list":
            tags = args.tags or None
            notes = await store.list_notes(tags=tags)
            if not notes:
                return ToolResult(content="(no notes)")
            body = "\n\n".join(_render(n) for n in notes)
            return ToolResult(content=body)

        if args.action == "get":
            if not args.id:
                return ToolResult(content="note get: 'id' is required", is_error=True)
            got = await store.get(id=args.id)
            if got is None:
                return ToolResult(content=f"no such note: {args.id}", is_error=True)
            return ToolResult(content=_render(got))

        if args.action == "update":
            if not args.id or args.content is None:
                return ToolResult(
                    content="note update: 'id' and 'content' are required", is_error=True
                )
            try:
                await store.update(id=args.id, content=args.content)
            except KeyError:
                return ToolResult(content=f"no such note: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(mem_note_updated(id=args.id))
            return ToolResult(content=f"updated {args.id}")

        if args.action == "delete":
            if not args.id:
                return ToolResult(content="note delete: 'id' is required", is_error=True)
            removed = await store.delete(id=args.id)
            if not removed:
                return ToolResult(content=f"no such note: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(mem_note_deleted(id=args.id))
            return ToolResult(content=f"deleted {args.id}")

        # Should be unreachable thanks to Literal[...]
        return ToolResult(content=f"note: unknown action {args.action!r}", is_error=True)
