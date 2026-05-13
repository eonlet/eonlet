"""All CLI subcommands.

Grouped here to keep each command tight; the typer wiring is in ``main.py``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import anyio
from rich.table import Table

from .. import paths
from ..config import load_agent_config
from ..errors import EonletAlreadyExistsError
from ..runtime.definition import load_definition
from ..worker.ipc import IPCClient
from ..worker.lifecycle import process_alive, read_meta, read_pid, write_meta, write_status
from .util import console, effective_status, fail, resolve_eonlet_id

# ── init / version ───────────────────────────────────────────────────────────


def cmd_init(force: bool = False) -> None:
    """`eonlet init` — set up ~/.eonlet/."""
    home = paths.home()
    if home.exists() and not force:
        console.print(f"[yellow]{home} already exists. Use --force to top-up missing files.[/]")
    paths.ensure_home()
    cfg = paths.config_path()
    if not cfg.exists() or force:
        cfg.write_text(_default_config_yaml(), encoding="utf-8")
        console.print(f"wrote {cfg}")

    # Install bundled agent templates.
    bundled_root = _bundled_templates_dir()
    for name in ("assistant", "x-digest", "portfolio"):
        src = bundled_root / name
        if not src.exists():
            continue
        dest = paths.agent_definition_dir(name)
        if not dest.exists():
            shutil.copytree(src, dest)
            console.print(f"installed bundled agent → {dest}")
        else:
            console.print(f"{name} template already present at {dest}")

    console.print("\n[green]done.[/] Next:")
    console.print("  1. export ANTHROPIC_API_KEY=sk-ant-...")
    console.print("  2. eonlet create assistant --name=alice")
    console.print("  3. eonlet attach alice")


def cmd_version() -> None:
    from .. import __spec_version__, __version__

    console.print(f"eonlet {__version__}")
    console.print(f"spec {__spec_version__}")
    console.print(f"Python {sys.version.split()[0]} ({sys.platform})")


# ── def ls / validate ────────────────────────────────────────────────────────


def cmd_def_ls() -> None:
    root = paths.agents_dir()
    if not root.exists():
        fail("no ~/.eonlet/agents/ — run `eonlet init` first", code=3)
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("VERSION")
    table.add_column("TRIGGERS")
    table.add_column("TOOLS")
    table.add_column("DESCRIPTION")
    found = False
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        try:
            cfg = load_agent_config(d)
        except Exception as e:
            table.add_row(d.name, "?", "?", "?", f"[red]invalid: {e}[/]")
            found = True
            continue
        triggers = f"cron({len(cfg.triggers)})" if cfg.triggers else "-"
        n_tools = len(cfg.tools.builtin) + len(cfg.tools.custom)
        table.add_row(
            cfg.metadata.name,
            cfg.metadata.version,
            triggers,
            str(n_tools),
            cfg.metadata.description,
        )
        found = True
    if not found:
        console.print("[dim](no definitions installed)[/]")
        return
    console.print(table)


def cmd_def_validate(name_or_path: str) -> None:
    path = Path(name_or_path)
    if not path.exists():
        path = paths.agent_definition_dir(name_or_path)
    try:
        defn = load_definition(path)
    except Exception as e:
        fail(f"validation failed: {e}")
    console.print(f"[green]ok[/] — {defn.type} v{defn.config.metadata.version}")
    if defn.skills:
        console.print(f"  skills: {', '.join(defn.skills)}")
    if defn.custom_tool_paths:
        console.print(f"  custom tools: {len(defn.custom_tool_paths)}")


# ── lifecycle: create / ls / kill / rm / pause / resume ──────────────────────


def cmd_create(agent_type: str, name: str | None, no_start: bool, env_overrides: list[str]) -> None:
    paths.ensure_home()
    defn_path = paths.agent_definition_dir(agent_type)
    if not defn_path.exists():
        fail(f"no definition for type {agent_type!r}. Try `eonlet def ls`.", code=3)
    try:
        defn = load_definition(defn_path)
    except Exception as e:
        fail(f"definition is invalid: {e}")

    instance_name = name or _gen_instance_name(agent_type)
    eonlet_id = f"{agent_type}.{instance_name}"
    eonlet_root = paths.eonlet_dir(eonlet_id)
    if eonlet_root.exists():
        raise EonletAlreadyExistsError(f"{eonlet_id} already exists at {eonlet_root}")

    eonlet_root.mkdir(parents=True)
    paths.memory_dir(eonlet_id).mkdir(parents=True, exist_ok=True)
    paths.workspace_dir(eonlet_id).mkdir(parents=True, exist_ok=True)
    paths.logs_dir(eonlet_id).mkdir(parents=True, exist_ok=True)

    # Write a .env from instance overrides if any.
    if env_overrides:
        env_lines = "\n".join(env_overrides) + "\n"
        (eonlet_root / ".env").write_text(env_lines, encoding="utf-8")

    write_meta(
        eonlet_id,
        type_=agent_type,
        name=instance_name,
        definition=defn_path,
        version=defn.config.metadata.version,
    )
    write_status(eonlet_id, "created")

    # Required-env check.
    missing = [
        v
        for v in defn.config.env.required
        if not os.environ.get(v) and v not in _parse_env_lines(env_overrides)
    ]
    if missing:
        console.print(f"[yellow]warning: required env vars not set: {missing}[/]")

    console.print(f"[green]created[/] {eonlet_id} at {eonlet_root}")

    if no_start:
        return

    pid = _spawn_worker(eonlet_id)
    # Wait up to 5s for the runtime socket.
    sock_path = paths.runtime_sock(eonlet_id)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if sock_path.exists():
            break
        if not process_alive(pid):
            fail(
                f"worker exited before binding socket. Check {paths.current_log(eonlet_id)}", code=2
            )
        time.sleep(0.1)
    if not sock_path.exists():
        fail(f"timed out waiting for {sock_path}", code=2)
    console.print(f"{eonlet_id} ready (pid={pid})")


def _spawn_worker(eonlet_id: str) -> int:
    """Fork an ``eonlet-worker`` child, detached, with logs going to file."""
    log = paths.current_log(eonlet_id)
    log.parent.mkdir(parents=True, exist_ok=True)
    logf = log.open("ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "eonlet.worker.main", eonlet_id],
        stdout=logf,
        stderr=logf,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from CLI's terminal
    )
    return proc.pid


def cmd_ls(show_all: bool, status_filter: str | None) -> None:
    root = paths.eonlets_dir()
    if not root.exists():
        console.print("[dim](no eonlets)[/]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("STATUS")
    table.add_column("PID")
    table.add_column("MESSAGES")
    table.add_column("DEFINITION")
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        eid = d.name
        status = effective_status(eid)
        if status_filter and status != status_filter:
            continue
        if not show_all and status in {"dead"}:
            # default: hide dead
            pass  # we still want to show them — minimal MVP
        meta = read_meta(eid) or {}
        pid = read_pid(eid)
        msg_count = _count_messages(eid)
        table.add_row(eid, status, str(pid or "-"), str(msg_count), meta.get("type", "?"))
    console.print(table)


def _count_messages(eonlet_id: str) -> int:
    """Cheap: count user_message + assistant_message events in the DB."""
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return 0
    try:
        from ..runtime.store import EventStore

        store = EventStore(db)
        try:
            return store.count()
        finally:
            store.close()
    except Exception:
        return 0


def cmd_kill(eonlet_id: str, force: bool) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    pid = read_pid(eid)
    if pid is None or not process_alive(pid):
        console.print(f"{eid}: already not running")
        write_status(eid, "dead")
        return
    sig = signal.SIGKILL if force else signal.SIGTERM
    os.kill(pid, sig)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not process_alive(pid):
            console.print(f"{eid}: stopped")
            return
        time.sleep(0.1)
    if not force:
        console.print(f"{eid}: didn't exit in 5s, sending SIGKILL")
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    write_status(eid, "dead")


def cmd_rm(eonlet_id: str, with_data: bool, yes: bool) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    status = effective_status(eid)
    if status not in {"dead", "created"}:
        fail(f"{eid}: status is {status}, run `eonlet kill {eid}` first", code=4)
    root = paths.eonlet_dir(eid)
    if not yes:
        console.print(
            f"about to remove {root}" + (" (including memory + workspace)" if with_data else "")
        )
        try:
            ans = input("continue? [y/N] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() != "y":
            console.print("aborted")
            return
    if with_data:
        shutil.rmtree(root)
    else:
        # Preserve memory/ and workspace/.
        for entry in root.iterdir():
            if entry.name in {"memory", "workspace"}:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    console.print(f"removed {eid}")


def cmd_pause(eonlet_id: str) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    pid = read_pid(eid)
    if pid is None or not process_alive(pid):
        fail(f"{eid}: not running")
    os.kill(pid, signal.SIGSTOP)
    write_status(eid, "paused")
    console.print(f"{eid}: paused")


def cmd_resume(eonlet_id: str) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    pid = read_pid(eid)
    if pid is None:
        fail(f"{eid}: no pid file")
    os.kill(pid, signal.SIGCONT)
    write_status(eid, "running")
    console.print(f"{eid}: resumed")


# ── interact: attach / send ──────────────────────────────────────────────────


def cmd_attach(eonlet_id: str, readonly: bool) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    sock = paths.runtime_sock(eid)
    if not sock.exists():
        fail(f"{eid}: no runtime socket — is it running?", code=2)
    anyio.run(_attach_async, eid, str(sock), readonly)


async def _attach_async(eid: str, sock: str, readonly: bool) -> None:
    async with IPCClient(sock) as client, anyio.create_task_group() as tg:
        tg.start_soon(client.run)
        await client.request("session.start", {"client_id": "cli"})
        console.print(f"[dim]attached to {eid}. Ctrl+D to detach.[/]")
        tg.start_soon(_event_printer, client)
        if not readonly:
            tg.start_soon(_input_reader, client, tg.cancel_scope)


async def _event_printer(client: IPCClient) -> None:
    """Pretty-print server-pushed notifications: streamed token_delta plus
    discrete event blocks (tool_call/tool_result/permission/error).

    Streaming behavior: we emit ``token_delta`` inline (no newline) under a
    bold "assistant: " prefix on the first delta of a run; the run-end
    ``assistant_message`` event then closes the line with the tool-call list
    if any, otherwise just a blank line.
    """
    streaming = False  # True between first delta and next non-delta event

    def _start_assistant_line() -> None:
        nonlocal streaming
        if not streaming:
            # `end=""` keeps subsequent deltas on the same line.
            console.print("[bold cyan]assistant[/] ", end="")
            streaming = True

    def _end_assistant_line_if_open() -> None:
        nonlocal streaming
        if streaming:
            console.print("")  # newline
            streaming = False

    async for msg in client.notifications():
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "token_delta":
            _start_assistant_line()
            text = params.get("delta_text") or ""
            # Use raw print to bypass rich markup parsing on streamed bytes.
            sys.stdout.write(text)
            sys.stdout.flush()
            continue
        if method != "event":
            continue
        kind = params.get("kind")
        payload = params.get("payload") or {}
        if kind == "assistant_message":
            tcs = payload.get("tool_calls") or []
            if streaming:
                # Streamed text already shown; just close the line.
                _end_assistant_line_if_open()
                for tc in tcs:
                    console.print(f"  [dim]→ tool[/] {tc['name']}({tc['args']})")
            else:
                # No deltas (e.g. provider without streaming, or empty text).
                content = payload.get("content") or ""
                if content:
                    console.print(f"[bold cyan]assistant[/] {content}")
                for tc in tcs:
                    console.print(f"  [dim]→ tool[/] {tc['name']}({tc['args']})")
        elif kind == "tool_result":
            _end_assistant_line_if_open()
            snippet = (payload.get("output") or "")[:300]
            console.print(f"  [dim]← {payload.get('tool_name')}[/] {snippet}")
        elif kind == "tool_error":
            _end_assistant_line_if_open()
            console.print(f"  [red]← {payload.get('tool_name')} error[/] {payload.get('output')}")
        elif kind == "permission_denied":
            _end_assistant_line_if_open()
            console.print(f"  [yellow]denied[/] {payload}")
        elif kind == "error":
            _end_assistant_line_if_open()
            console.print(f"  [red]error[/] {payload}")


async def _input_reader(client: IPCClient, cancel: anyio.CancelScope) -> None:
    """Read user input from stdin; send as message.send requests."""
    while True:
        line = await anyio.to_thread.run_sync(_read_prompt, abandon_on_cancel=True)
        if line is None:
            console.print("[dim]detaching[/]")
            cancel.cancel()
            return
        line = line.strip()
        if not line:
            continue
        # Now safe to use request — the demuxer owns the read side.
        await client.request("message.send", {"content": line})


def _read_prompt() -> str | None:
    try:
        return input("\n> ")
    except (EOFError, KeyboardInterrupt):
        return None


def cmd_send(eonlet_id: str, message: str) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    sock = paths.runtime_sock(eid)
    if not sock.exists():
        fail(f"{eid}: no runtime socket — is it running?", code=2)
    anyio.run(_send_async, str(sock), message)


async def _send_async(sock: str, message: str) -> None:
    async with IPCClient(sock) as client, anyio.create_task_group() as tg:
        tg.start_soon(client.run)
        await client.request("session.start", {"client_id": "cli-send"})
        await client.request("message.send", {"content": message})
        # End-of-run = an assistant_message event with no tool_calls.
        async for msg in client.notifications():
            if msg.get("method") != "event":
                continue
            p = msg.get("params") or {}
            if p.get("kind") != "assistant_message":
                continue
            content = p.get("payload", {}).get("content") or ""
            if content:
                console.print(content)
            if not p.get("payload", {}).get("tool_calls"):
                tg.cancel_scope.cancel()
                return


# ── ps / tail / replay / export / import ────────────────────────────────────


def cmd_ps(show_all: bool) -> None:
    """`docker ps`-style listing.

    For each running eonlet we contact its IPC socket to ask for trigger
    schedule info (best-effort — if the worker is wedged we just show '?').
    For dead ones we read the meta files only.
    """
    root = paths.eonlets_dir()
    if not root.exists():
        console.print("[dim](no eonlets)[/]")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID")
    table.add_column("PID")
    table.add_column("STATUS")
    table.add_column("UPTIME")
    table.add_column("LAST EVENT")
    table.add_column("NEXT TRIGGER")
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        eid = d.name
        status = effective_status(eid)
        if not show_all and status in {"dead"}:
            continue
        pid = read_pid(eid)
        uptime = _uptime_for(eid, pid)
        last_event = _last_event_summary(eid)
        next_trigger = _next_trigger_summary(eid, status)
        table.add_row(eid, str(pid or "-"), status, uptime, last_event, next_trigger)
    console.print(table)


def _uptime_for(eonlet_id: str, pid: int | None) -> str:
    if pid is None:
        return "-"
    # Fall back to the pid file's mtime — close enough for an MVP "uptime".
    p = paths.pid_file(eonlet_id)
    if not p.exists():
        return "-"
    import datetime as _dt

    started = _dt.datetime.fromtimestamp(p.stat().st_mtime)
    return _short_duration((_dt.datetime.now() - started).total_seconds())


def _short_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, _ = divmod(s, 60)
    if m < 60:
        return f"{m}m"
    h, m2 = divmod(m, 60)
    if h < 24:
        return f"{h}h{m2:02d}m"
    d, h2 = divmod(h, 24)
    return f"{d}d{h2:02d}h"


def _last_event_summary(eonlet_id: str) -> str:
    """Show the most recent event kind + age."""
    db = paths.state_db(eonlet_id)
    if not db.exists():
        return "-"
    try:
        from ..runtime.store import EventStore

        store = EventStore(db)
        try:
            events = store.read(since=max(0, store.latest_id() - 1), limit=1)
        finally:
            store.close()
    except Exception:
        return "?"
    if not events:
        return "(idle)"
    last = events[-1]
    age = max(0, (time.time() * 1_000_000 - last.ts) / 1_000_000)
    return f"{str(last.kind).split('.')[-1]} ({_short_duration(age)} ago)"


def _next_trigger_summary(eonlet_id: str, status: str) -> str:
    if status != "running":
        return "-"
    sock = paths.runtime_sock(eonlet_id)
    if not sock.exists():
        return "-"

    async def go() -> str:
        try:
            async with IPCClient(str(sock)) as client, anyio.create_task_group() as tg:
                tg.start_soon(client.run)
                with anyio.move_on_after(1.5):
                    resp = await client.request("triggers.list", {})
                    tg.cancel_scope.cancel()
                    triggers = (resp or {}).get("triggers", [])
                    if not triggers:
                        return "(none)"
                    # Pick the soonest.
                    from datetime import datetime as _dt

                    soonest = min(
                        triggers,
                        key=lambda t: t.get("next_fire_at") or "9999",
                    )
                    nfa = soonest.get("next_fire_at")
                    if not nfa:
                        return str(soonest["id"])
                    dt = _dt.fromisoformat(nfa)
                    delta = (dt - _dt.now(dt.tzinfo)).total_seconds()
                    return f"{soonest['id']} in {_short_duration(max(0, delta))}"
                tg.cancel_scope.cancel()
            return "?"
        except Exception:
            return "?"

    result: str = anyio.run(go)
    return result


def cmd_tail(eonlet_id: str) -> None:
    """Live event stream from the worker. Each line is one event."""
    eid = resolve_eonlet_id(eonlet_id)
    sock = paths.runtime_sock(eid)
    if not sock.exists():
        fail(f"{eid}: no runtime socket — is it running?", code=2)

    async def go() -> None:
        async with IPCClient(str(sock)) as client, anyio.create_task_group() as tg:
            tg.start_soon(client.run)
            await client.request("session.start", {"client_id": "cli-tail"})
            async for msg in client.notifications():
                if msg.get("method") != "event":
                    continue
                p = msg.get("params") or {}
                kind = p.get("kind")
                eid_ = p.get("id") or "?"
                payload = p.get("payload") or {}
                snippet = _summarize_payload(kind, payload)
                console.print(f"[dim]#{eid_}[/] [bold]{kind}[/] {snippet}")

    try:
        anyio.run(go)
    except KeyboardInterrupt:
        return


def _summarize_payload(kind: str | None, payload: dict[str, Any]) -> str:
    if not kind:
        return ""
    if kind == "assistant_message":
        c = (payload.get("content") or "").replace("\n", " ")[:120]
        tcs = payload.get("tool_calls") or []
        return f"{c}" + (f"  (+{len(tcs)} tool calls)" if tcs else "")
    if kind in {"tool_call", "tool_result", "tool_error"}:
        return f"{payload.get('tool_name', '?')} {str(payload.get('output') or payload.get('args') or '')[:100]}"
    if kind == "user_message":
        return (payload.get("content") or "")[:120]
    return str(payload)[:120]


def cmd_replay(eonlet_id: str, from_: int | None, to: int | None) -> None:
    """Print events from state.db. Read-only — never re-executes anything."""
    eid = resolve_eonlet_id(eonlet_id)
    from ..runtime.store import EventStore

    db = paths.state_db(eid)
    if not db.exists():
        fail(f"{eid}: no state.db", code=3)
    store = EventStore(db)
    try:
        events = store.read(since=(from_ or 0) - 1 if from_ else 0)
    finally:
        store.close()
    if to is not None:
        events = [e for e in events if e.id is not None and e.id <= to]
    for e in events:
        ts = _iso_us(e.ts)
        summary = _summarize_payload(str(e.kind), e.payload)
        console.print(f"[dim]{ts}[/] #{e.id:>4} [bold]{e.kind}[/] {summary}")


def _iso_us(us: int) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(us / 1_000_000).isoformat(timespec="seconds")


def cmd_export(eonlet_id: str, output: Path) -> None:
    """Pack the eonlet's directory into a tar.gz, minus transient files."""
    import tarfile

    eid = resolve_eonlet_id(eonlet_id)
    root = paths.eonlet_dir(eid)
    if not root.exists():
        fail(f"{eid}: not found", code=3)
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    skip = {"runtime.sock", "pid", "heartbeat", "state.db-wal", "state.db-shm", "logs"}
    with tarfile.open(output, "w:gz") as tar:
        for item in root.rglob("*"):
            rel = item.relative_to(root)
            if rel.parts and rel.parts[0] in skip:
                continue
            tar.add(item, arcname=f"{eid}/{rel}", recursive=False)
    console.print(f"[green]exported[/] {eid} → {output}")


def cmd_import(archive: Path, as_name: str | None) -> None:
    """Restore an eonlet's directory from an `eonlet export` archive."""
    import tarfile

    archive = archive.expanduser().resolve()
    if not archive.exists():
        fail(f"archive not found: {archive}", code=3)
    paths.ensure_home()
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if not names:
            fail("archive is empty", code=2)
        # First path segment is the original id; allow renaming.
        original = names[0].split("/")[0]
        new_id = as_name or original
        target = paths.eonlet_dir(new_id)
        if target.exists():
            fail(f"{new_id} already exists at {target}; remove it first or use --as", code=4)
        # Extract under a temp dir, then move (so we can rename top-level path).
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            # filter='data' opts into the Python 3.12+ safe-extract policy.
            tar.extractall(td, filter="data")
            src = Path(td) / original
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, target)
    console.print(f"[green]imported[/] {archive.name} → {target}")


# ── fire / doctor ────────────────────────────────────────────────────────────


def cmd_fire(eonlet_id: str, trigger_id: str, message: str | None) -> None:
    """Manually fire a configured cron trigger (TRIGGER_SPEC §5)."""
    eid = resolve_eonlet_id(eonlet_id)
    sock = paths.runtime_sock(eid)
    if not sock.exists():
        fail(f"{eid}: no runtime socket — is it running?", code=2)

    async def go() -> None:
        async with IPCClient(str(sock)) as client, anyio.create_task_group() as tg:
            tg.start_soon(client.run)
            resp = await client.request(
                "trigger.fire",
                {"trigger_id": trigger_id, "message": message},
            )
            if not resp or not resp.get("ok"):
                fail(f"fire failed: {resp.get('error') if resp else 'unknown'}")
            console.print(f"[green]fired[/] {eid}/{trigger_id}")
            tg.cancel_scope.cancel()

    anyio.run(go)


def cmd_doctor() -> None:
    """Self-checks per CLI_REFERENCE §System."""
    checks: list[tuple[str, bool, str]] = []
    # 1. Home is writable.
    home = paths.home()
    try:
        paths.ensure_home()
        testfile = home / ".doctor_probe"
        testfile.write_text("x")
        testfile.unlink()
        checks.append(("eonlet home writable", True, str(home)))
    except Exception as e:
        checks.append(("eonlet home writable", False, str(e)))

    # 2. API keys present (warn-only — neither is hard-required).
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    checks.append(("ANTHROPIC_API_KEY set", has_anthropic, ""))
    checks.append(("OPENAI_API_KEY set", has_openai, ""))

    # 3. SQLite WAL works.
    try:
        from ..runtime.store import EventStore

        probe = home / ".doctor.db"
        store = EventStore(probe)
        store.close()
        probe.unlink(missing_ok=True)
        for sfx in ("-wal", "-shm"):
            (home / f".doctor.db{sfx}").unlink(missing_ok=True)
        checks.append(("sqlite WAL", True, ""))
    except Exception as e:
        checks.append(("sqlite WAL", False, str(e)))

    # 4. Cron parser works.
    try:
        from croniter import croniter as _cr

        _cr("0 8 * * *").get_next()
        checks.append(("cron parser", True, ""))
    except Exception as e:
        checks.append(("cron parser", False, str(e)))

    # 5. All installed definitions validate.
    failed_defs: list[str] = []
    for d in sorted(paths.agents_dir().iterdir()) if paths.agents_dir().exists() else []:
        if not d.is_dir():
            continue
        try:
            load_agent_config(d)
        except Exception as e:
            failed_defs.append(f"{d.name}: {e}")
    checks.append(
        (
            "definitions validate",
            not failed_defs,
            "; ".join(failed_defs) if failed_defs else "",
        )
    )

    # 6. No orphan sockets (worker dead but sock file present).
    orphans: list[str] = []
    for d in sorted(paths.eonlets_dir().iterdir()) if paths.eonlets_dir().exists() else []:
        sock = paths.runtime_sock(d.name)
        if sock.exists() and not process_alive(read_pid(d.name)):
            orphans.append(d.name)
    checks.append(
        (
            "no orphan sockets",
            not orphans,
            f"orphaned: {', '.join(orphans)}" if orphans else "",
        )
    )

    for label, ok, detail in checks:
        mark = "[green]✓[/]" if ok else "[red]✗[/]"
        suffix = f"  [dim]{detail}[/]" if detail else ""
        console.print(f"{mark} {label}{suffix}")


# ── logs / inspect ───────────────────────────────────────────────────────────


def cmd_logs(eonlet_id: str, follow: bool, tail: int | None) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    log = paths.current_log(eid)
    if not log.exists():
        fail(f"no log at {log}", code=3)
    if tail is not None:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines()[-tail:]:
            console.print(line)
        if not follow:
            return
    # follow: poll-tail
    with log.open("r", encoding="utf-8", errors="replace") as f:
        f.seek(0, 2)
        try:
            while True:
                chunk = f.read()
                if chunk:
                    console.print(chunk, end="")
                else:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            return


def cmd_inspect(eonlet_id: str) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    meta = read_meta(eid) or {}
    payload = {
        "id": eid,
        "status": effective_status(eid),
        "pid": read_pid(eid),
        "meta": meta,
        "message_count": _count_messages(eid),
        "memory": [str(p.name) for p in sorted(paths.memory_dir(eid).glob("*")) if p.is_file()],
        "workspace_files": [
            str(p.relative_to(paths.workspace_dir(eid)))
            for p in paths.workspace_dir(eid).rglob("*")
            if p.is_file()
        ][:30],
    }
    console.print_json(data=payload)


# ── helpers ──────────────────────────────────────────────────────────────────


def _gen_instance_name(agent_type: str) -> str:
    import secrets

    return secrets.token_hex(3)


def _parse_env_lines(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _bundled_templates_dir() -> Path:
    """Where templates live inside the installed package.

    For the dev tree (no install yet), point at ``<repo>/agents`` so the
    bundled examples can be installed via ``eonlet init``.
    """
    pkg_root = Path(__file__).resolve().parents[1]
    pkg_local = pkg_root / "templates"
    if pkg_local.exists():
        return pkg_local
    # Dev fallback: repo-level agents/ directory.
    return pkg_root.parent.parent / "agents"


def _default_config_yaml() -> str:
    return (
        "# ~/.eonlet/config.yaml — global defaults\n"
        "defaults:\n"
        "  model: claude-sonnet-4-6\n"
        "  permissions:\n"
        "    mode: ask\n"
        "  budget:\n"
        "    daily_usd: 5.0\n"
        "providers:\n"
        "  anthropic:\n"
        "    api_key_env: ANTHROPIC_API_KEY\n"
        "  openai:\n"
        "    api_key_env: OPENAI_API_KEY\n"
        "    base_url_env: OPENAI_BASE_URL\n"
        "logging:\n"
        "  level: info\n"
    )
