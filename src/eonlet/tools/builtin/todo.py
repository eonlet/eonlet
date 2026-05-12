"""todo: action-style TODO management.

Per MEMORY_SPEC §5.4. TODOs have structured state (pending/done/cancelled)
and optional due dates, persisted as JSONL in ``memory/todos.jsonl``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...memory.ids import mint_todo_id
from ...memory.todos import Todo, TodosStore
from ...runtime.events import mem_todo_added, mem_todo_deleted, mem_todo_updated
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class TodoArgs(BaseModel):
    action: Literal["add", "list", "done", "update", "delete"]
    id: str | None = Field(default=None, description="TODO id (required for done/update/delete).")
    content: str | None = Field(default=None, description="Body text.")
    due: str | None = Field(
        default=None,
        description="Optional ISO-8601 due date (e.g. '2026-05-30T18:00:00+08:00').",
    )
    tags: list[str] = Field(default_factory=list)
    status: Literal["pending", "done", "cancelled", "all"] = Field(
        default="pending",
        description="For action='list': which status to return ('all' to list everything).",
    )


def _render(t: Todo) -> str:
    icon = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}[t.status]
    head = f"{icon} {t.id}"
    if t.due:
        head += f"  (due: {t.due})"
    if t.tags:
        head += "  (tags: " + ", ".join(t.tags) + ")"
    return head + "\n    " + t.content.replace("\n", "\n    ")


@tool
class TodoTool:
    name = "todo"
    description = (
        "Action-item tracker with structured state. Actions: "
        "'add' (content required; optional due/tags), "
        "'list' (status filter: pending|done|cancelled|all), "
        "'done' (mark pending → done by id), "
        "'update' (id + any of content/due/tags), "
        "'delete' (id)."
    )
    input_schema = TodoArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: TodoArgs, ctx: ToolContext) -> ToolResult:
        store = TodosStore(ctx.memory_dir)

        if args.action == "add":
            if not args.content:
                return ToolResult(content="todo add: 'content' is required", is_error=True)
            todo_id = mint_todo_id()
            try:
                todo = await store.add(
                    id=todo_id, content=args.content, due=args.due, tags=args.tags
                )
            except ValueError as e:
                return ToolResult(content=f"todo add: {e}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(
                    mem_todo_added(id=todo.id, content=todo.content, due=todo.due, tags=todo.tags)
                )
            return ToolResult(content=f"added {todo.id}", structured_output={"id": todo.id})

        if args.action == "list":
            todos = await store.list_todos(status=args.status)
            if not todos:
                return ToolResult(content=f"(no {args.status} todos)")
            return ToolResult(content="\n".join(_render(t) for t in todos))

        if args.action == "done":
            if not args.id:
                return ToolResult(content="todo done: 'id' is required", is_error=True)
            try:
                todo = await store.mark_done(id=args.id)
            except KeyError:
                return ToolResult(content=f"no such todo: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(
                    mem_todo_updated(id=todo.id, status=todo.status, done_at=todo.done_at)
                )
            return ToolResult(content=f"done {todo.id}")

        if args.action == "update":
            if not args.id:
                return ToolResult(content="todo update: 'id' is required", is_error=True)
            if args.content is None and args.due is None and not args.tags:
                return ToolResult(
                    content="todo update: provide at least one of content/due/tags",
                    is_error=True,
                )
            try:
                todo = await store.update(
                    id=args.id,
                    content=args.content,
                    due=args.due,
                    tags=args.tags or None,
                )
            except KeyError:
                return ToolResult(content=f"no such todo: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(mem_todo_updated(id=todo.id, status=todo.status))
            return ToolResult(content=f"updated {todo.id}")

        if args.action == "delete":
            if not args.id:
                return ToolResult(content="todo delete: 'id' is required", is_error=True)
            removed = await store.delete(id=args.id)
            if not removed:
                return ToolResult(content=f"no such todo: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(mem_todo_deleted(id=args.id))
            return ToolResult(content=f"deleted {args.id}")

        return ToolResult(content=f"todo: unknown action {args.action!r}", is_error=True)
