"""schedule: in-conversation management of cron + one-shot triggers.

Per ADR-0002. Actions: ``list`` / ``add`` / ``add_once`` / ``remove`` /
``set_enabled`` / ``clear``. Static triggers (declared in ``agent.yaml``) are
read-only here: they can be enabled/disabled in-process but never removed.
Dynamic triggers (recurring or one-shot) are persisted to
``<eonlet_dir>/dynamic_triggers.json``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ...config import CronTrigger, OnceTrigger, parse_duration
from ...errors import ConfigError
from ...triggers.dynamic_store import mint_dynamic_id
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class ScheduleArgs(BaseModel):
    """Flat schema; the action discriminator picks which other fields apply."""

    action: Literal["list", "add", "add_once", "remove", "set_enabled", "clear"]
    # add (cron)
    schedule: str | None = Field(
        default=None, description="Cron expression (required for action='add')."
    )
    # add + add_once
    timezone: str | None = Field(
        default=None,
        description=("IANA tz, e.g. 'Asia/Shanghai'. Required for 'add' and 'add_once'."),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Prompt the agent receives when the trigger fires "
            "(required for 'add' and 'add_once'). Must not start with '<trigger '."
        ),
    )
    # add_once: pick exactly one
    fire_at: str | None = Field(
        default=None,
        description=(
            "ISO-8601 datetime with tz offset, e.g. "
            "'2026-05-20T09:00:00+08:00'. For action='add_once'; alternative to 'in_'."
        ),
    )
    in_: str | None = Field(
        default=None,
        alias="in",
        description=(
            "Relative duration ('30s', '5m', '2h', '1d') from now. For "
            "action='add_once'; alternative to 'fire_at'."
        ),
    )
    grace_period: str = Field(default="1h", description="e.g. '15m', '1h', '6h'.")
    # remove / set_enabled
    trigger_id: str | None = Field(
        default=None, description="Trigger id (required for action='remove' or 'set_enabled')."
    )
    # set_enabled
    enabled: bool | None = Field(
        default=None, description="True to enable, False to disable (action='set_enabled')."
    )
    # clear
    confirm: bool = Field(
        default=False, description="Must be true for action='clear' to actually wipe."
    )


@tool
class ScheduleTool:
    name = "schedule"
    description = (
        "Manage the eonlet's triggers from a conversation. Actions: "
        "'list' (read static + dynamic; read-only), "
        "'add' (recurring cron trigger; needs schedule, timezone, message), "
        "'add_once' (one-shot timer; needs timezone, message, and either "
        "fire_at (ISO datetime with tz) or in (e.g. '30m')), "
        "'remove' (delete a dynamic trigger by id; static IDs are protected), "
        "'set_enabled' (toggle a trigger by id), "
        "'clear' (drop all dynamic triggers; requires confirm=true). "
        "Static triggers declared in agent.yaml cannot be removed."
    )
    input_schema = ScheduleArgs
    annotations = ToolAnnotations(destructive=True)

    async def __call__(self, args: ScheduleArgs, ctx: ToolContext) -> ToolResult:
        sched = ctx.scheduler
        if sched is None:
            return ToolResult(content="schedule: no scheduler attached to this run", is_error=True)
        try:
            if args.action == "list":
                return self._list(sched)
            if args.action == "add":
                return await self._add(args, sched)
            if args.action == "add_once":
                return await self._add_once(args, sched)
            if args.action == "remove":
                return await self._remove(args, sched)
            if args.action == "set_enabled":
                return await self._set_enabled(args, sched)
            if args.action == "clear":
                return await self._clear(args, sched)
        except ConfigError as e:
            return ToolResult(content=f"schedule.{args.action}: {e}", is_error=True)
        return ToolResult(content=f"unknown action: {args.action!r}", is_error=True)

    # ── action handlers ─────────────────────────────────────────────────────

    def _list(self, sched: object) -> ToolResult:
        rows = sched.serializable()  # type: ignore[attr-defined]
        # Compact, model-friendly output: pretty JSON. The CLI uses a table
        # for its `/trigger` slash command; the LLM is happier reading JSON.
        return ToolResult(
            content=json.dumps(rows, ensure_ascii=False, indent=2),
            structured_output={"triggers": rows},
        )

    async def _add(self, args: ScheduleArgs, sched: object) -> ToolResult:
        if not (args.schedule and args.timezone and args.message):
            return ToolResult(
                content="schedule.add: 'schedule', 'timezone', and 'message' are required",
                is_error=True,
            )
        if args.message.lstrip().startswith("<trigger "):
            return ToolResult(
                content=(
                    "schedule.add: 'message' must not start with '<trigger ' "
                    "(reserved for the system trigger envelope)"
                ),
                is_error=True,
            )
        trig = CronTrigger(
            id=mint_dynamic_id(),
            schedule=args.schedule,
            timezone=args.timezone,
            message=args.message,
            grace_period=args.grace_period,
            enabled=True,
        )
        rec = await sched.add_dynamic(trig, created_by="agent")  # type: ignore[attr-defined]
        return ToolResult(
            content=f"added dynamic trigger {rec.trig.id} ({rec.trig.schedule} {rec.trig.timezone})",
            structured_output={"trigger_id": rec.trig.id},
        )

    async def _add_once(self, args: ScheduleArgs, sched: object) -> ToolResult:
        if not (args.timezone and args.message):
            return ToolResult(
                content="schedule.add_once: 'timezone' and 'message' are required",
                is_error=True,
            )
        if args.message.lstrip().startswith("<trigger "):
            return ToolResult(
                content=(
                    "schedule.add_once: 'message' must not start with '<trigger ' "
                    "(reserved for the system trigger envelope)"
                ),
                is_error=True,
            )
        if bool(args.fire_at) == bool(args.in_):
            return ToolResult(
                content="schedule.add_once: provide exactly one of 'fire_at' or 'in'",
                is_error=True,
            )
        if args.in_ is not None:
            try:
                seconds = parse_duration(args.in_)
            except ConfigError as e:
                return ToolResult(content=f"schedule.add_once: {e}", is_error=True)
            fire_at_iso = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
        else:
            fire_at_iso = args.fire_at or ""
        trig = OnceTrigger(
            id=mint_dynamic_id(),
            fire_at=fire_at_iso,
            timezone=args.timezone,
            message=args.message,
            grace_period=args.grace_period,
            enabled=True,
        )
        rec = await sched.add_once_dynamic(trig, created_by="agent")  # type: ignore[attr-defined]
        return ToolResult(
            content=f"added one-shot trigger {rec.trig.id} (fires at {rec.trig.fire_at})",
            structured_output={"trigger_id": rec.trig.id, "fire_at": rec.trig.fire_at},
        )

    async def _remove(self, args: ScheduleArgs, sched: object) -> ToolResult:
        if not args.trigger_id:
            return ToolResult(content="schedule.remove: 'trigger_id' required", is_error=True)
        removed = await sched.remove_dynamic(args.trigger_id)  # type: ignore[attr-defined]
        if not removed:
            return ToolResult(
                content=f"schedule.remove: no such dynamic trigger: {args.trigger_id}",
                is_error=True,
            )
        return ToolResult(content=f"removed {args.trigger_id}")

    async def _set_enabled(self, args: ScheduleArgs, sched: object) -> ToolResult:
        if not args.trigger_id or args.enabled is None:
            return ToolResult(
                content="schedule.set_enabled: 'trigger_id' and 'enabled' required",
                is_error=True,
            )
        ok = await sched.set_enabled(args.trigger_id, args.enabled)  # type: ignore[attr-defined]
        if not ok:
            return ToolResult(
                content=f"schedule.set_enabled: no such trigger: {args.trigger_id}",
                is_error=True,
            )
        return ToolResult(content=f"{'enabled' if args.enabled else 'disabled'} {args.trigger_id}")

    async def _clear(self, args: ScheduleArgs, sched: object) -> ToolResult:
        if not args.confirm:
            return ToolResult(
                content="schedule.clear: pass confirm=true to actually drop all dynamic triggers",
                is_error=True,
            )
        n = await sched.clear_dynamic()  # type: ignore[attr-defined]
        return ToolResult(content=f"cleared {n} dynamic trigger(s)")
