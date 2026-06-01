"""task: hierarchical, event-sourced action-item management (ADR-0007).

Tasks are workflow state — things the agent will *do* — and live beside
``schedule``, not in memory. As of ADR-0007 the task source of truth is the
**event log**: this tool mutates only by emitting task events (``record_event``)
and reads the live forest projection via ``ctx.read_tasks``. There is no JSONL
store to double-write.

Actions: ``add`` (optional ``parent_id`` for a subtask, ``priority``, ``goal``),
``list`` (status filter, rendered as a tree), ``done`` / ``cancel`` (lifecycle
transition by id), ``update`` (edit content/goal/priority/due/tags), ``delete``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ...runtime.events import task_created, task_deleted, task_transitioned, task_updated
from ...tasks import Task, TaskForest, can_transition, creation_guard_error, mint_task_id
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class TaskArgs(BaseModel):
    action: Literal["add", "list", "done", "cancel", "update", "delete"]
    id: str | None = Field(
        default=None, description="Task id (required for done/cancel/update/delete)."
    )
    content: str | None = Field(default=None, description="Body text.")
    goal: str | None = Field(
        default=None, description="Durable objective (used to rebuild context on resume)."
    )
    result: str | None = Field(
        default=None, description="For action='done': a short result/outcome summary."
    )
    parent_id: str | None = Field(
        default=None, description="For action='add': attach as a subtask of this task id."
    )
    priority: int | None = Field(
        default=None, description="Scheduling priority; higher runs first. Default 0."
    )
    due: str | None = Field(
        default=None,
        description="Optional ISO-8601 due date (e.g. '2026-05-30T18:00:00+08:00').",
    )
    tags: list[str] = Field(default_factory=list)
    schedule: str | None = Field(
        default=None,
        description=(
            "For action='add': a cron expression to run this task on a recurring "
            "schedule. Each fire hatches a fresh task instance. Requires timezone."
        ),
    )
    timezone: str | None = Field(
        default=None, description="IANA tz for 'schedule' (e.g. 'Asia/Shanghai')."
    )
    status: Literal["pending", "active", "suspended", "blocked", "done", "cancelled", "all"] = (
        Field(
            default="pending",
            description="For action='list': which status to return ('all' for everything).",
        )
    )


async def _schedule_task(args: TaskArgs, ctx: ToolContext) -> ToolResult:
    """Register a recurring task-template trigger (ADR-0007 M3)."""
    from ...config import CronTrigger
    from ...errors import ConfigError
    from ...triggers.dynamic_store import mint_dynamic_id

    if ctx.scheduler is None:
        return ToolResult(content="task add: scheduling unavailable here", is_error=True)
    if not args.timezone:
        return ToolResult(content="task add: 'timezone' is required with 'schedule'", is_error=True)
    template = {
        "content": args.content,
        "goal": args.goal or "",
        "priority": args.priority or 0,
        "due": args.due,
        "tags": args.tags,
    }
    # model_validate (not the kwargs ctor) so the extra ``task_template`` field
    # — allowed at runtime by CronTrigger's extra="allow" — type-checks cleanly.
    trig = CronTrigger.model_validate(
        {
            "id": mint_dynamic_id(),
            "schedule": args.schedule,
            "timezone": args.timezone,
            "message": "(scheduled task)",
            "task_template": template,
        }
    )
    try:
        await ctx.scheduler.add_dynamic(trig, created_by="agent")
    except ConfigError as e:
        return ToolResult(content=f"task add: {e}", is_error=True)
    return ToolResult(
        content=f"scheduled {trig.id} ({args.schedule})", structured_output={"trigger_id": trig.id}
    )


def _render_tree(forest: TaskForest, status_filter: str) -> str:
    """Indented tree of tasks matching the status filter (ancestors shown for context)."""
    icon = {
        "pending": "[ ]",
        "active": "[*]",
        "suspended": "[~]",
        "blocked": "[!]",
        "done": "[x]",
        "cancelled": "[-]",
    }
    keep = {t.id for t in forest.by_status(status_filter)}  # type: ignore[arg-type]
    if status_filter != "all":
        # Keep ancestors of matches so the tree stays connected/readable.
        for tid in list(keep):
            cur = forest.get(tid)
            while cur is not None and cur.parent_id is not None:
                keep.add(cur.parent_id)
                cur = forest.get(cur.parent_id)
    lines: list[str] = []
    for t, depth in forest.dfs():
        if t.id not in keep:
            continue
        indent = "  " * depth
        head = f"{indent}{icon.get(t.status, '[?]')} {t.id}"
        if t.priority:
            head += f"  (p{t.priority})"
        if t.due:
            head += f"  (due: {t.due})"
        if t.tags:
            head += "  (tags: " + ", ".join(t.tags) + ")"
        body = (t.goal or t.content).strip()
        lines.append(f"{head} — {body}" if body else head)
    return "\n".join(lines)


@tool
class TaskTool:
    name = "task"
    description = (
        "Hierarchical action-item tracker. Tasks form a tree (add with parent_id) "
        "and carry a priority. Actions: "
        "'add' (content required; optional parent_id/priority/goal/due/tags), "
        "'list' (status filter: pending|active|suspended|blocked|done|cancelled|all; "
        "rendered as a tree), "
        "'done' (mark → done by id), 'cancel' (mark → cancelled by id), "
        "'update' (id + any of content/goal/priority/due/tags), 'delete' (id)."
    )
    input_schema = TaskArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: TaskArgs, ctx: ToolContext) -> ToolResult:
        if ctx.record_event is None:
            return ToolResult(content="task: no event sink (not in an agent run)", is_error=True)
        forest = ctx.read_tasks() if ctx.read_tasks is not None else None

        if args.action == "add":
            if not args.content:
                return ToolResult(content="task add: 'content' is required", is_error=True)
            # A scheduled task registers a recurring template trigger instead of
            # creating a task now; each fire hatches a fresh instance (ADR-0007).
            if args.schedule:
                return await _schedule_task(args, ctx)
            # Inside a task-scoped run, a new task without an explicit parent is a
            # subtask of the task being worked on (the decomposition signal).
            parent_id = args.parent_id or ctx.current_task_id
            if parent_id and forest is not None and forest.get(parent_id) is None:
                return ToolResult(
                    content=f"task add: no such parent task: {parent_id}", is_error=True
                )
            # Anti-runaway depth / fan-out caps (ADR-0007 M4).
            if forest is not None:
                guard = creation_guard_error(
                    forest,
                    parent_id,
                    max_depth=ctx.max_task_depth,
                    max_fanout=ctx.max_task_fanout,
                )
                if guard is not None:
                    return ToolResult(content=f"task add: {guard}", is_error=True)
            new_id = mint_task_id()
            await ctx.record_event(
                task_created(
                    id=new_id,
                    content=args.content,
                    goal=args.goal or "",
                    priority=args.priority or 0,
                    parent_id=parent_id,
                    origin="agent",
                    due=args.due,
                    tags=args.tags,
                )
            )
            return ToolResult(content=f"added {new_id}", structured_output={"id": new_id})

        if args.action == "list":
            if forest is None:
                return ToolResult(content="task list: unavailable outside the agent run")
            rendered = _render_tree(forest, args.status)
            if not rendered:
                return ToolResult(content=f"(no {args.status} tasks)")
            return ToolResult(content=rendered)

        if args.action in ("done", "cancel"):
            tid = args.id or ctx.current_task_id
            if not tid:
                return ToolResult(content=f"task {args.action}: 'id' is required", is_error=True)
            task = forest.get(tid) if forest is not None else None
            if task is None:
                return ToolResult(content=f"no such task: {tid}", is_error=True)
            dst = "done" if args.action == "done" else "cancelled"
            if task.status == dst:
                return ToolResult(content=f"{tid} already {dst}")
            if not can_transition(task.status, dst):
                return ToolResult(
                    content=f"task {args.action}: cannot move {tid} from {task.status} to {dst}",
                    is_error=True,
                )
            await ctx.record_event(
                task_transitioned(
                    id=task.id,
                    from_state=task.status,
                    to_state=dst,
                    reason=f"tool:{args.action}",
                    result=args.result if args.action == "done" else None,
                )
            )
            return ToolResult(content=f"{args.action} {task.id}")

        if args.action == "update":
            tid = args.id or ctx.current_task_id
            if not tid:
                return ToolResult(content="task update: 'id' is required", is_error=True)
            if (
                args.content is None
                and args.goal is None
                and args.priority is None
                and args.due is None
                and not args.tags
            ):
                return ToolResult(
                    content="task update: provide at least one of content/goal/priority/due/tags",
                    is_error=True,
                )
            if forest is not None and forest.get(tid) is None:
                return ToolResult(content=f"no such task: {tid}", is_error=True)
            await ctx.record_event(
                task_updated(
                    id=tid,
                    content=args.content,
                    goal=args.goal,
                    priority=args.priority,
                    due=args.due,
                    tags=args.tags or None,
                )
            )
            return ToolResult(content=f"updated {tid}")

        if args.action == "delete":
            if not args.id:
                return ToolResult(content="task delete: 'id' is required", is_error=True)
            if forest is not None and forest.get(args.id) is None:
                return ToolResult(content=f"no such task: {args.id}", is_error=True)
            await ctx.record_event(task_deleted(id=args.id))
            return ToolResult(content=f"deleted {args.id}")

        return ToolResult(content=f"task: unknown action {args.action!r}", is_error=True)


__all__ = ["Task", "TaskArgs", "TaskTool"]
