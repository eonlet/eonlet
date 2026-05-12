"""Runtime status report for a single eonlet instance.

``collect(eonlet_id)`` assembles a ``StatusReport`` from disk + optional IPC.
``render(report, console)`` prints it as a Rich layout.
``--json`` callers use ``report.model_dump()`` directly.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, Field
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import paths
from ..memory.config import MemoryConfig
from ..memory.tokens import estimate
from ..worker.ipc import IPCClient
from ..worker.lifecycle import process_alive, read_meta, read_pid, read_status

# ── Section models ────────────────────────────────────────────────────────────


class IdentitySection(BaseModel):
    id: str
    name: str
    agent_type: str
    definition_path: str
    created_at: str
    spec_version: str


class ProcessSection(BaseModel):
    status: str
    pid: int | None
    uptime_s: float | None
    heartbeat_age_s: float | None
    alive: bool


class TokenSection(BaseModel):
    tokens_in_total: int = 0
    tokens_out_total: int = 0
    cost_usd_total: float = 0.0
    cost_usd_today: float = 0.0
    last_turn_tokens_in: int | None = None
    last_turn_tokens_out: int | None = None
    last_turn_model: str | None = None
    turn_count: int = 0


class MemoryTierInfo(BaseModel):
    estimated_tokens: int = 0
    budget_tokens: int = 0
    count: int = 0  # messages / sections / bullets / notes


class MemorySection(BaseModel):
    enabled: bool = True
    compact_paused: bool = False
    working: MemoryTierInfo = Field(default_factory=MemoryTierInfo)
    stm: MemoryTierInfo = Field(default_factory=MemoryTierInfo)
    ltm: MemoryTierInfo = Field(default_factory=MemoryTierInfo)
    notes: MemoryTierInfo = Field(default_factory=MemoryTierInfo)
    todos_active: int = 0
    todos_done: int = 0
    todos_cancelled: int = 0


class TriggerInfo(BaseModel):
    id: str
    schedule: str | None = None
    last_fired_at: str | None = None
    next_fire_at: str | None = None
    total_fires: int = 0
    consecutive_failures: int = 0


class TriggerSection(BaseModel):
    triggers: list[TriggerInfo] = Field(default_factory=list)
    source: str = "offline"  # "live" | "offline" | "unavailable"


class ActivityEvent(BaseModel):
    id: int
    kind: str
    age_s: float
    preview: str


class ActivitySection(BaseModel):
    events: list[ActivityEvent] = Field(default_factory=list)


class StatusReport(BaseModel):
    identity: IdentitySection
    process: ProcessSection
    tokens: TokenSection
    memory: MemorySection
    triggers: TriggerSection
    activity: ActivitySection


# ── Collection ────────────────────────────────────────────────────────────────


def collect(eonlet_id: str) -> StatusReport:
    meta = read_meta(eonlet_id) or {}
    return StatusReport(
        identity=_collect_identity(eonlet_id, meta),
        process=_collect_process(eonlet_id),
        tokens=_collect_tokens(eonlet_id),
        memory=_collect_memory(eonlet_id, meta),
        triggers=_collect_triggers(eonlet_id),
        activity=_collect_activity(eonlet_id),
    )


def _collect_identity(eonlet_id: str, meta: dict[str, Any]) -> IdentitySection:
    return IdentitySection(
        id=eonlet_id,
        name=meta.get("name", eonlet_id.split(".")[-1]),
        agent_type=meta.get("type", eonlet_id.split(".")[0]),
        definition_path=meta.get("definition_path", "-"),
        created_at=meta.get("created_at", "-"),
        spec_version=meta.get("spec_version", "-"),
    )


def _collect_process(eonlet_id: str) -> ProcessSection:
    pid = read_pid(eonlet_id)
    alive = process_alive(pid)
    status = read_status(eonlet_id)

    uptime_s: float | None = None
    pid_file = paths.pid_file(eonlet_id)
    if pid_file.exists():
        uptime_s = time.time() - pid_file.stat().st_mtime

    heartbeat_age_s: float | None = None
    hb = paths.heartbeat_file(eonlet_id)
    if hb.exists():
        try:
            last_hb = float(hb.read_text(encoding="utf-8").strip())
            heartbeat_age_s = time.time() - last_hb
        except ValueError:
            pass

    return ProcessSection(
        status=status,
        pid=pid,
        uptime_s=uptime_s,
        heartbeat_age_s=heartbeat_age_s,
        alive=alive,
    )


def _collect_tokens(eonlet_id: str) -> TokenSection:
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return TokenSection()

    try:
        # Import here to avoid circular; store is only needed on demand.
        try:
            import apsw as _sqlite

            conn = _sqlite.Connection(str(db))
        except ImportError:
            import sqlite3 as _sqlite  # type: ignore[no-redef]

            conn = _sqlite.connect(str(db), isolation_level=None)  # type: ignore[attr-defined]

        today_start_us = _today_start_us()

        row_total = next(
            conn.execute(
                "SELECT COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
                " COALESCE(SUM(cost_usd),0), COUNT(*)"
                " FROM events WHERE kind='assistant_message'"
            )
        )
        row_today = next(
            conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0)"
                " FROM events WHERE kind='assistant_message' AND ts >= ?",
                (today_start_us,),
            )
        )
        last_row = next(
            conn.execute(
                "SELECT tokens_in, tokens_out, payload FROM events"
                " WHERE kind='assistant_message' ORDER BY id DESC LIMIT 1"
            ),
            None,
        )
        conn.close()

        last_in: int | None = None
        last_out: int | None = None
        last_model: str | None = None
        if last_row:
            last_in = last_row[0]
            last_out = last_row[1]
            try:
                import msgpack

                payload = msgpack.unpackb(bytes(last_row[2]), raw=False)
                last_model = payload.get("model")
            except Exception:
                pass

        return TokenSection(
            tokens_in_total=int(row_total[0]),
            tokens_out_total=int(row_total[1]),
            cost_usd_total=float(row_total[2]),
            cost_usd_today=float(row_today[0]),
            last_turn_tokens_in=last_in,
            last_turn_tokens_out=last_out,
            last_turn_model=last_model,
            turn_count=int(row_total[3]),
        )
    except Exception:
        return TokenSection()


def _today_start_us() -> int:
    now = datetime.now(tz=UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(today.timestamp() * 1_000_000)


def _collect_memory(eonlet_id: str, meta: dict[str, Any]) -> MemorySection:
    mem_dir = paths.memory_dir(eonlet_id)
    defn_path_str = meta.get("definition_path", "")
    mem_cfg = MemoryConfig()
    if defn_path_str:
        try:
            from ..config import load_agent_config

            cfg = load_agent_config(Path(defn_path_str))
            mem_cfg = cfg.memory
        except Exception:
            pass

    section = MemorySection(enabled=mem_cfg.enabled)

    # working memory: estimate from state.db messages post-last-compaction watermark
    section.working = _collect_working(eonlet_id, mem_cfg)

    # STM
    stm_path = mem_dir / "short_term.md"
    if stm_path.exists():
        try:
            from ..memory.stm import parse as stm_parse

            text = stm_path.read_text(encoding="utf-8")
            sections = stm_parse(text)
            section.stm = MemoryTierInfo(
                estimated_tokens=estimate(text),
                budget_tokens=mem_cfg.conversation.short_term_tokens,
                count=len(sections),
            )
        except Exception:
            section.stm = MemoryTierInfo(
                estimated_tokens=estimate(stm_path.read_text(encoding="utf-8")),
                budget_tokens=mem_cfg.conversation.short_term_tokens,
            )

    # LTM
    ltm_path = mem_dir / "long_term.md"
    if ltm_path.exists():
        try:
            from ..memory.ltm import LTMStore

            store = LTMStore(mem_dir)
            bullets = store.read_bullets()
            text = ltm_path.read_text(encoding="utf-8")
            section.ltm = MemoryTierInfo(
                estimated_tokens=estimate(text),
                budget_tokens=mem_cfg.conversation.long_term_tokens,
                count=len(bullets),
            )
        except Exception:
            text = ltm_path.read_text(encoding="utf-8")
            section.ltm = MemoryTierInfo(
                estimated_tokens=estimate(text),
                budget_tokens=mem_cfg.conversation.long_term_tokens,
            )

    # Notes
    notes_path = mem_dir / "notes.md"
    if notes_path.exists():
        try:
            from ..memory.notes import NotesStore

            ns = NotesStore(mem_dir)
            notes_list, _ = ns._read_all()
            text = notes_path.read_text(encoding="utf-8")
            section.notes = MemoryTierInfo(
                estimated_tokens=estimate(text),
                budget_tokens=mem_cfg.notes.max_tokens,
                count=len(notes_list),
            )
        except Exception:
            text = notes_path.read_text(encoding="utf-8")
            section.notes = MemoryTierInfo(
                estimated_tokens=estimate(text),
                budget_tokens=mem_cfg.notes.max_tokens,
            )

    # Todos
    todos_jsonl = mem_dir / "todos.jsonl"
    if todos_jsonl.exists():
        try:
            import json

            for line in todos_jsonl.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                st = obj.get("status", "pending")
                if st == "pending":
                    section.todos_active += 1
                elif st == "done":
                    section.todos_done += 1
                elif st == "cancelled":
                    section.todos_cancelled += 1
        except Exception:
            pass

    # compact paused: check last mem_paused vs mem_resumed event
    section.compact_paused = _is_compact_paused(eonlet_id)

    return section


def _collect_working(eonlet_id: str, mem_cfg: MemoryConfig) -> MemoryTierInfo:
    """Estimate working memory token usage from recent events."""
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return MemoryTierInfo(budget_tokens=mem_cfg.conversation.working_memory_tokens)
    try:
        try:
            import apsw as _sqlite

            conn = _sqlite.Connection(str(db))
        except ImportError:
            import sqlite3 as _sqlite  # type: ignore[no-redef]

            conn = _sqlite.connect(str(db), isolation_level=None)  # type: ignore[attr-defined]

        # Find last compaction boundary (watermark event_id)
        watermark_row = next(
            conn.execute(
                "SELECT payload FROM events WHERE kind='mem_compacted' ORDER BY id DESC LIMIT 1"
            ),
            None,
        )
        watermark_id = 0
        if watermark_row:
            try:
                import msgpack

                payload = msgpack.unpackb(bytes(watermark_row[0]), raw=False)
                watermark_id = payload.get("boundary_event_id", 0)
            except Exception:
                pass

        # Count and estimate messages after the watermark
        rows = list(
            conn.execute(
                "SELECT kind, payload FROM events WHERE id > ?"
                " AND kind IN ('user_message','assistant_message','tool_result','tool_error')"
                " ORDER BY id ASC",
                (watermark_id,),
            )
        )
        conn.close()

        total_tokens = 0
        msg_count = 0
        for _kind, payload_blob in rows:
            try:
                import msgpack

                payload = msgpack.unpackb(bytes(payload_blob), raw=False)
                content = payload.get("content", "") or ""
                total_tokens += estimate(content) + 4
                msg_count += 1
            except Exception:
                pass

        return MemoryTierInfo(
            estimated_tokens=total_tokens,
            budget_tokens=mem_cfg.conversation.working_memory_tokens,
            count=msg_count,
        )
    except Exception:
        return MemoryTierInfo(budget_tokens=mem_cfg.conversation.working_memory_tokens)


def _is_compact_paused(eonlet_id: str) -> bool:
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return False
    try:
        try:
            import apsw as _sqlite

            conn = _sqlite.Connection(str(db))
        except ImportError:
            import sqlite3 as _sqlite  # type: ignore[no-redef]

            conn = _sqlite.connect(str(db), isolation_level=None)  # type: ignore[attr-defined]

        row = next(
            conn.execute(
                "SELECT kind FROM events WHERE kind IN ('mem_paused','mem_resumed')"
                " ORDER BY id DESC LIMIT 1"
            ),
            None,
        )
        conn.close()
        return row is not None and row[0] == "mem_paused"
    except Exception:
        return False


def _collect_triggers(eonlet_id: str) -> TriggerSection:
    """Try live IPC first; fall back to event store trigger_state table."""
    sock = paths.runtime_sock(eonlet_id)
    if sock.exists():
        result = anyio.run(_fetch_triggers_live, str(sock))
        if result is not None:
            return result

    # Offline: read trigger_state from sqlite
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return TriggerSection(source="unavailable")
    try:
        try:
            import apsw as _sqlite

            conn = _sqlite.Connection(str(db))
        except ImportError:
            import sqlite3 as _sqlite  # type: ignore[no-redef]

            conn = _sqlite.connect(str(db), isolation_level=None)  # type: ignore[attr-defined]

        rows = list(
            conn.execute(
                "SELECT trigger_id, last_fired_at, total_fires, consecutive_failures"
                " FROM trigger_state ORDER BY trigger_id"
            )
        )
        conn.close()
        triggers = [
            TriggerInfo(
                id=row[0],
                last_fired_at=_us_to_iso(row[1]) if row[1] else None,
                total_fires=row[2] or 0,
                consecutive_failures=row[3] or 0,
            )
            for row in rows
        ]
        return TriggerSection(triggers=triggers, source="offline")
    except Exception:
        return TriggerSection(source="unavailable")


async def _fetch_triggers_live(sock_path: str) -> TriggerSection | None:
    try:
        async with IPCClient(sock_path) as client, anyio.create_task_group() as tg:
            tg.start_soon(client.run)
            with anyio.move_on_after(2.0):
                resp = await client.request("triggers.list", {})
                tg.cancel_scope.cancel()
                raw_triggers = (resp or {}).get("triggers", [])
                triggers = [
                    TriggerInfo(
                        id=t.get("id", "?"),
                        schedule=t.get("schedule"),
                        last_fired_at=t.get("last_fired_at"),
                        next_fire_at=t.get("next_fire_at"),
                        total_fires=t.get("total_fires", 0),
                        consecutive_failures=t.get("consecutive_failures", 0),
                    )
                    for t in raw_triggers
                ]
                return TriggerSection(triggers=triggers, source="live")
            tg.cancel_scope.cancel()
        return None
    except Exception:
        return None


def _collect_activity(eonlet_id: str, n: int = 10) -> ActivitySection:
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return ActivitySection()
    try:
        from ..runtime.store import EventStore

        store = EventStore(db)
        try:
            latest = store.latest_id()
            events = store.read(since=max(0, latest - 200), limit=None)
        finally:
            store.close()

        now_us = time.time() * 1_000_000
        result: list[ActivityEvent] = []
        for ev in reversed(events[-n:]):
            preview = _event_preview(ev.kind, ev.payload)
            result.append(
                ActivityEvent(
                    id=ev.id or 0,
                    kind=str(ev.kind),
                    age_s=max(0.0, (now_us - ev.ts) / 1_000_000),
                    preview=preview,
                )
            )
        return ActivitySection(events=result)
    except Exception:
        return ActivitySection()


def _event_preview(kind: Any, payload: dict[str, Any]) -> str:
    k = str(kind)
    if "user_message" in k:
        content = payload.get("content", "")
        return f'"{content[:60]}{"…" if len(content) > 60 else ""}"'
    if "assistant_message" in k:
        tin = payload.get("tokens_in") or payload.get("input_tokens")
        tout = payload.get("tokens_out") or payload.get("output_tokens")
        parts = []
        if tin:
            parts.append(f"in:{tin}")
        if tout:
            parts.append(f"out:{tout}")
        content = payload.get("content", "")
        if content and not parts:
            return f'"{content[:60]}{"…" if len(content) > 60 else ""}"'
        return "  ".join(parts) if parts else ""
    if "tool_call" in k:
        return str(payload.get("tool_name", ""))
    if "tool_result" in k or "tool_error" in k:
        return str(payload.get("tool_name", ""))
    if "mem_compacted" in k:
        before = payload.get("tokens_before", "?")
        after = payload.get("tokens_after", "?")
        return f"tier1  {before}→{after} tokens"
    if "mem_ltm_promoted" in k:
        n = len(payload.get("additions", []))
        return f"tier2  +{n} LTM bullets"
    if "trigger_fired" in k:
        return str(payload.get("trigger_id", ""))
    if "session_started" in k or "session_ended" in k:
        return str(payload.get("reason", "") or payload.get("mode", ""))
    return ""


def _us_to_iso(us: int | None) -> str | None:
    if us is None:
        return None
    try:
        return datetime.fromtimestamp(us / 1_000_000, tz=UTC).isoformat()
    except Exception:
        return None


# ── Rendering ─────────────────────────────────────────────────────────────────


def render(report: StatusReport, console: Console) -> None:
    _render_header(report.identity, report.process, console)
    _render_process(report.process, console)
    _render_tokens(report.tokens, console)
    _render_memory(report.memory, console)
    _render_triggers(report.triggers, console)
    _render_activity(report.activity, console)


def _render_header(identity: IdentitySection, process: ProcessSection, console: Console) -> None:
    status_color = {
        "running": "green",
        "created": "blue",
        "paused": "yellow",
        "dead": "red",
    }.get(process.status, "dim")

    lines = [
        f"[bold]{identity.id}[/]   "
        f"[{status_color}]{process.status}[/{status_color}]   "
        f"[dim]{identity.agent_type}[/]",
        f"[dim]def   {identity.definition_path}[/]",
        f"[dim]created  {identity.created_at}[/]",
    ]
    console.print(Panel("\n".join(lines), padding=(0, 1)))


def _render_process(p: ProcessSection, console: Console) -> None:
    console.print("\n[bold]PROCESS[/]")
    parts: list[str] = [
        f"  status  [{'green' if p.alive else 'dim'}]{p.status}[/]",
        f"  pid  {p.pid or '-'}",
    ]
    if p.uptime_s is not None:
        parts.append(f"  uptime  {_human_duration(p.uptime_s)}")
    if p.heartbeat_age_s is not None:
        color = "yellow" if p.heartbeat_age_s > 60 else "dim"
        parts.append(f"  heartbeat  [{color}]{_human_duration(p.heartbeat_age_s)} ago[/]")
    console.print("  ".join(parts))


def _render_tokens(t: TokenSection, console: Console) -> None:
    console.print("\n[bold]TOKENS[/]")

    table = Table(box=None, show_header=True, header_style="dim", pad_edge=False, padding=(0, 2))
    table.add_column("")
    table.add_column("today", justify="right")
    table.add_column("total", justify="right")

    # We don't have a per-day breakdown for in/out — show total for both cols
    table.add_row(
        "  in",
        "-",
        f"{t.tokens_in_total:,}",
    )
    table.add_row(
        "  out",
        "-",
        f"{t.tokens_out_total:,}",
    )
    table.add_row(
        "  cost",
        f"${t.cost_usd_today:.4f}",
        f"${t.cost_usd_total:.4f}",
    )
    table.add_row("  turns", "-", str(t.turn_count))
    console.print(table)

    if t.last_turn_tokens_in is not None or t.last_turn_tokens_out is not None:
        parts = ["  last turn"]
        if t.last_turn_tokens_in is not None:
            parts.append(f"in: {t.last_turn_tokens_in:,}")
        if t.last_turn_tokens_out is not None:
            parts.append(f"out: {t.last_turn_tokens_out:,}")
        if t.last_turn_model:
            parts.append(f"[dim]({t.last_turn_model})[/]")
        console.print("   ".join(parts))


def _render_memory(m: MemorySection, console: Console) -> None:
    enabled_str = "[green]on[/]" if m.enabled else "[dim]off[/]"
    compact_str = "[yellow]paused[/]" if m.compact_paused else "[green]on[/]"
    console.print(f"\n[bold]MEMORY[/]  (enabled: {enabled_str}  auto-compact: {compact_str})")

    def tier_row(label: str, info: MemoryTierInfo, unit: str) -> None:
        pct = (info.estimated_tokens / info.budget_tokens * 100) if info.budget_tokens else 0.0
        bar = _bar(pct)
        count_str = f"  ({info.count} {unit})" if info.count else ""
        console.print(
            f"  {label:<8}  {info.estimated_tokens:>6,} / {info.budget_tokens:,} tokens"
            f"  {bar}  {pct:4.0f}%{count_str}"
        )

    tier_row("working", m.working, "msgs")
    tier_row("STM", m.stm, "sections")
    tier_row("LTM", m.ltm, "bullets")
    tier_row("notes", m.notes, "notes")

    todo_parts = [f"  todos    {m.todos_active} active"]
    if m.todos_done:
        todo_parts.append(f"  {m.todos_done} done")
    if m.todos_cancelled:
        todo_parts.append(f"  {m.todos_cancelled} cancelled")
    console.print(" / ".join(todo_parts))


def _render_triggers(t: TriggerSection, console: Console) -> None:
    source_tag = f"[dim]({t.source})[/]"
    console.print(f"\n[bold]TRIGGERS[/]  {source_tag}")
    if not t.triggers:
        console.print("  [dim](none)[/]")
        return

    now = datetime.now(tz=UTC)
    for tr in t.triggers:
        sched = f"  [{tr.schedule}]" if tr.schedule else ""
        next_str = ""
        if tr.next_fire_at:
            try:
                dt = datetime.fromisoformat(tr.next_fire_at)
                delta = (dt - now).total_seconds()
                next_str = f"  next in {_human_duration(max(0, delta))}"
            except Exception:
                next_str = f"  next {tr.next_fire_at}"
        fail_str = (
            f"  [yellow]failures: {tr.consecutive_failures}[/]" if tr.consecutive_failures else ""
        )
        console.print(f"  [cyan]{tr.id}[/]{sched}{next_str}  fires: {tr.total_fires}{fail_str}")


def _render_activity(a: ActivitySection, console: Console) -> None:
    console.print("\n[bold]RECENT ACTIVITY[/]")
    if not a.events:
        console.print("  [dim](no events)[/]")
        return

    table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column("kind", style="cyan", no_wrap=True)
    table.add_column("age", justify="right", style="dim", no_wrap=True)
    table.add_column("preview")

    for ev in a.events:
        kind = ev.kind.split(".")[-1] if "." in ev.kind else ev.kind
        table.add_row(f"  {kind}", _human_duration(ev.age_s) + " ago", ev.preview)

    console.print(table)


# ── Formatting helpers ────────────────────────────────────────────────────────


def _human_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, rem = divmod(s, 60)
    if m < 60:
        return f"{m}m{rem:02d}s"
    h, m2 = divmod(m, 60)
    if h < 24:
        return f"{h}h{m2:02d}m"
    d, h2 = divmod(h, 24)
    return f"{d}d{h2:02d}h"


def _bar(pct: float, width: int = 10) -> str:
    filled = int(min(pct, 100) / 100 * width)
    color = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    bar = "█" * filled + "░" * (width - filled)
    return f"[{color}]{bar}[/]"
