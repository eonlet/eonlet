"""task: action-style task / action-item management (ADR-0005).

Tasks are workflow state — things the agent will *do* — and live beside
``schedule``, not in memory. Replaces the old ``todo`` tool (same state
machine: pending/done/cancelled, due dates, tags), persisted as JSONL in
``tasks/todos.jsonl``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ...runtime.events import task_added, task_deleted, task_updated
from ...tasks import Task, TaskStore, mint_task_id
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class TaskArgs(BaseModel):
    action: Literal["add", "list", "done", "cancel", "update", "delete"]
    id: str | None = Field(
        default=None, description="Task id (required for done/cancel/update/delete)."
    )
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


def _tasks_dir(ctx: ToolContext) -> Path:
    # The worker sets ctx.tasks_dir explicitly; fall back to the eonlet-dir
    # sibling of memory/ when running outside the full runtime.
    return ctx.tasks_dir if ctx.tasks_dir is not None else ctx.memory_dir.parent / "tasks"


def _render(t: Task) -> str:
    icon = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}[t.status]
    head = f"{icon} {t.id}"
    if t.due:
        head += f"  (due: {t.due})"
    if t.tags:
        head += "  (tags: " + ", ".join(t.tags) + ")"
    return head + "\n    " + t.content.replace("\n", "\n    ")


@tool
class TaskTool:
    name = "task"
    description = (
        "Action-item tracker with structured state. Actions: "
        "'add' (content required; optional due/tags), "
        "'list' (status filter: pending|done|cancelled|all), "
        "'done' (mark pending → done by id), "
        "'cancel' (mark → cancelled by id), "
        "'update' (id + any of content/due/tags), "
        "'delete' (id)."
    )
    input_schema = TaskArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: TaskArgs, ctx: ToolContext) -> ToolResult:
        store = TaskStore(_tasks_dir(ctx))

        if args.action == "add":
            if not args.content:
                return ToolResult(content="task add: 'content' is required", is_error=True)
            try:
                task = await store.add(
                    id=mint_task_id(), content=args.content, due=args.due, tags=args.tags
                )
            except ValueError as e:
                return ToolResult(content=f"task add: {e}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(
                    task_added(id=task.id, content=task.content, due=task.due, tags=task.tags)
                )
            return ToolResult(content=f"added {task.id}", structured_output={"id": task.id})

        if args.action == "list":
            tasks = await store.list_tasks(status=args.status)
            if not tasks:
                return ToolResult(content=f"(no {args.status} tasks)")
            return ToolResult(content="\n".join(_render(t) for t in tasks))

        if args.action in ("done", "cancel"):
            if not args.id:
                return ToolResult(content=f"task {args.action}: 'id' is required", is_error=True)
            try:
                if args.action == "done":
                    task = await store.mark_done(id=args.id)
                else:
                    task = await store.mark_cancelled(id=args.id)
            except KeyError:
                return ToolResult(content=f"no such task: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(
                    task_updated(id=task.id, status=task.status, done_at=task.done_at)
                )
            return ToolResult(content=f"{args.action} {task.id}")

        if args.action == "update":
            if not args.id:
                return ToolResult(content="task update: 'id' is required", is_error=True)
            if args.content is None and args.due is None and not args.tags:
                return ToolResult(
                    content="task update: provide at least one of content/due/tags",
                    is_error=True,
                )
            try:
                task = await store.update(
                    id=args.id, content=args.content, due=args.due, tags=args.tags or None
                )
            except KeyError:
                return ToolResult(content=f"no such task: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(task_updated(id=task.id, status=task.status))
            return ToolResult(content=f"updated {task.id}")

        if args.action == "delete":
            if not args.id:
                return ToolResult(content="task delete: 'id' is required", is_error=True)
            removed = await store.delete(id=args.id)
            if not removed:
                return ToolResult(content=f"no such task: {args.id}", is_error=True)
            if ctx.record_event is not None:
                await ctx.record_event(task_deleted(id=args.id))
            return ToolResult(content=f"deleted {args.id}")

        return ToolResult(content=f"task: unknown action {args.action!r}", is_error=True)
