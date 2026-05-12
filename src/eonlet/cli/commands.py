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
    console.print("  1. export OPENROUTER_API_KEY=sk-ant-...")
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


# ── lifecycle: create / ls / start / stop / rm / pause / resume ──────────────


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
    # An eonlet "exists" iff meta.json is present — that's the only file we
    # treat as a definitive marker. The root dir may exist with stale memory/
    # or workspace/ from a prior `rm --keep-data`, which is fine to reuse.
    if paths.meta_file(eonlet_id).exists():
        raise EonletAlreadyExistsError(f"{eonlet_id} already exists at {eonlet_root}")

    eonlet_root.mkdir(parents=True, exist_ok=True)
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
        fail(
            f"timed out waiting for {sock_path}. Check worker log: {paths.current_log(eonlet_id)}",
            code=2,
        )
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


def cmd_start(eonlet_id: str) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    if read_meta(eid) is None:
        fail(f"{eid}: not found — use `eonlet create` to create it first", code=4)
    pid = read_pid(eid)
    if pid is not None and process_alive(pid):
        console.print(f"{eid}: already running (pid={pid})")
        return
    write_status(eid, "created")
    pid = _spawn_worker(eid)
    sock_path = paths.runtime_sock(eid)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if sock_path.exists():
            break
        if not process_alive(pid):
            fail(f"worker exited before binding socket. Check {paths.current_log(eid)}", code=2)
        time.sleep(0.1)
    if not sock_path.exists():
        fail(
            f"timed out waiting for {sock_path}. Check worker log: {paths.current_log(eid)}",
            code=2,
        )
    console.print(f"{eid} started (pid={pid})")


def cmd_stop(eonlet_id: str, force: bool) -> None:
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


def cmd_rm(eonlet_id: str, keep_data: bool, yes: bool) -> None:
    eid = resolve_eonlet_id(eonlet_id)
    status = effective_status(eid)
    if status not in {"dead", "created"}:
        fail(f"{eid}: status is {status}, run `eonlet stop {eid}` first", code=4)
    root = paths.eonlet_dir(eid)
    if not yes:
        if keep_data:
            console.print(f"about to remove {root} (keeping memory/ and workspace/)")
        else:
            console.print(f"about to remove {root} (including memory + workspace)")
        try:
            ans = input("continue? [y/N] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() != "y":
            console.print("aborted")
            return
    if keep_data:
        for entry in root.iterdir():
            if entry.name in {"memory", "workspace"}:
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        shutil.rmtree(root)
    console.print(f"removed {eid}")


def cmd_recreate(
    eonlet_id: str,
    keep_data: bool,
    no_start: bool,
    yes: bool,
) -> None:
    """Tear down and re-create an eonlet in place — same type, same name."""
    eid = resolve_eonlet_id(eonlet_id)
    meta = read_meta(eid)
    if meta is None:
        fail(f"{eid}: no meta.json — nothing to recreate", code=4)
    agent_type = meta["type"]
    instance_name = meta["name"]

    if not yes:
        action = (
            "wipe and recreate" if not keep_data else "recreate (preserving memory/ + workspace/)"
        )
        console.print(f"about to {action} {eid}")
        try:
            ans = input("continue? [y/N] ")
        except EOFError:
            ans = "n"
        if ans.strip().lower() != "y":
            console.print("aborted")
            return

    status = effective_status(eid)
    if status not in {"dead", "created"}:
        console.print(f"{eid}: status is {status}, stopping first")
        cmd_stop(eid, force=False)

    # cmd_rm with yes=True since we already confirmed above.
    cmd_rm(eid, keep_data=keep_data, yes=True)
    cmd_create(agent_type, instance_name, no_start=no_start, env_overrides=[])


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
        info = await client.request("session.start", {"client_id": "cli"})
        _print_attach_banner(eid, readonly, info)
        tg.start_soon(_event_printer, client)
        if not readonly:
            tg.start_soon(_input_reader, client, eid, tg.cancel_scope)


def _print_attach_banner(eid: str, readonly: bool, info: dict[str, Any] | None) -> None:
    """Slim, Claude-Code-style header. One line of identity + status, then the
    recent conversation. No bordered panel — terminal real-estate is precious.
    """
    state = (info or {}).get("state") or {}

    # Status pill: ● busy(<activity>) | ○ idle
    if state.get("is_running"):
        activity = state.get("current_activity") or "working"
        status_pill = f"[bold yellow]●[/] [yellow]busy[/] [dim]({activity})[/]"
    else:
        status_pill = "[dim green]○ idle[/]"

    model = state.get("model") or "?"
    nmsg = state.get("message_count", "?")
    ro = "  [yellow](read-only)[/]" if readonly else ""

    console.print()
    console.print(
        f"[bold cyan]●[/] [bold]{eid}[/]  [dim]· {model} · {nmsg} msgs[/]  {status_pill}{ro}"
    )
    console.print("[dim]   type /help for commands · Ctrl+D to detach[/]")
    _print_recent_history(state.get("recent_messages") or [])


def _print_recent_history(messages: list[dict[str, Any]]) -> None:
    """Render the last few messages so a re-attaching user has context.

    Matches the Claude-Code-style transcript: ``>`` for user input lines
    (mirroring the prompt), ``●`` for assistant turns, ``⎿`` for tool-call
    sub-branches. Content is shown in full (rich will wrap it); only huge tool
    outputs are capped at ~800 chars so the banner stays readable.
    """
    from rich.markup import escape

    if not messages:
        return
    console.print()
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").rstrip()
        if role == "user":
            # Mirror the input prompt — same chevron, same color.
            console.print(f"[bold cyan]>[/] {escape(content)}")
        elif role == "assistant":
            if content:
                console.print(f"[bold cyan]●[/] {escape(content)}")
            for tc in m.get("tool_calls") or []:
                args_str = _fmt_args_short(tc.get("args"))
                console.print(
                    f"  [dim cyan]⎿[/] [bold]{escape(str(tc.get('name')))}[/]({args_str})"
                )
        elif role == "tool":
            tag = "[red]✗[/]" if m.get("is_error") else "[green]✓[/]"
            if len(content) > 800:
                content = content[:800] + "\n    (… truncated)"
            indented = escape(content).replace("\n", "\n    ")
            console.print(f"    {tag} {indented}")
    console.print()


def _fmt_args_short(args: Any, limit: int = 160) -> str:
    from rich.markup import escape

    if not isinstance(args, dict):
        s = repr(args)
    else:
        s = ", ".join(f"{k}={_short(v, 40)}" for k, v in args.items())
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return escape(s)


def _print_help() -> None:
    from rich.table import Table

    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("/help", "show this help")
    t.add_row("/quit, /exit", "detach (same as Ctrl+D)")
    t.add_row("/clear", "clear the screen")
    t.add_row("/status", "show eonlet status banner")
    t.add_row("/triggers, /trigger", "list scheduled triggers")
    t.add_row(
        "/trigger add <cron> <tz> <msg…>",
        "add a recurring trigger (cron has 5 fields)",
    )
    t.add_row(
        "/trigger once <ISO+tz> <tz> <msg…>",
        "add a one-shot trigger at an absolute time",
    )
    t.add_row(
        "/trigger in <30m|2h|1d> <tz> <msg…>",
        "add a one-shot trigger N from now",
    )
    t.add_row("/trigger rm <id>", "remove a dynamic trigger")
    t.add_row("/trigger on|off <id>", "enable / disable a trigger")
    t.add_row("/trigger clear yes", "drop all dynamic triggers")
    t.add_row("/note add <text>", "create a persistent note")
    t.add_row("/note list [tag]", "list notes (optional tag filter)")
    t.add_row("/note get <id>", "show one note in full")
    t.add_row("/note rm <id>", "delete a note")
    t.add_row("/todo add <text>", "add a pending todo")
    t.add_row("/todo list [status]", "list todos (pending|done|cancelled|all)")
    t.add_row("/todo done <id>", "mark a todo done")
    t.add_row("/todo rm <id>", "delete a todo")
    t.add_row("/compact", "force a tier-1 compaction pass right now")
    t.add_row("/compact off|on", "toggle auto-compaction for this session")
    t.add_row("/memory show [store]", "print stm|ltm|notes|todos|all")
    console.print(t)


async def _event_printer(client: IPCClient) -> None:
    """Pretty-print server-pushed notifications.

    Streamed ``token_delta`` notifications are *buffered*, not echoed live.
    Live-streaming under prompt_toolkit's ``patch_stdout`` proved unreliable
    (multibyte text + concurrent prompt redraws caused visibly truncated
    lines). Instead we show a single dim "● thinking..." marker while the
    model is generating; once the final ``assistant_message`` event arrives,
    its authoritative ``content`` is printed in one ``console.print`` call —
    correct every time.

    Tool-related events are still rendered as they happen, so the user sees
    progress for long tool calls.
    """
    from rich.markup import escape

    thinking_shown = False  # True after we've drawn the "● thinking..." line
    stream_buf: list[str] = []  # accumulator for token_delta — used as fallback

    def _ensure_thinking_line() -> None:
        nonlocal thinking_shown
        if not thinking_shown:
            console.print("\n[bold cyan]●[/] [dim]thinking…[/]")
            thinking_shown = True

    def _reset_stream() -> None:
        nonlocal thinking_shown
        thinking_shown = False
        stream_buf.clear()

    def _fmt_args(args: Any, limit: int = 120) -> str:
        if isinstance(args, dict):
            s = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
        else:
            s = repr(args)
        if len(s) > limit:
            s = s[: limit - 1] + "…"
        return escape(s)

    async for msg in client.notifications():
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "token_delta":
            # Buffer only — no live echo (see docstring).
            text = params.get("delta_text") or ""
            stream_buf.append(text)
            _ensure_thinking_line()
            continue
        if method != "event":
            continue
        kind = params.get("kind")
        payload = params.get("payload") or {}
        if kind == "user_message":
            # Show thinking indicator as soon as the worker accepts the
            # message — before the first token arrives.
            _ensure_thinking_line()
        elif kind == "assistant_message":
            content = payload.get("content") or ""
            tcs = payload.get("tool_calls") or []
            # Print the authoritative content from the event payload.
            if content:
                console.print(f"\n[bold cyan]●[/] {escape(content)}")
            elif not tcs and stream_buf:
                # Defensive: event has no content but we did receive deltas —
                # render what we got rather than dropping it.
                console.print(f"\n[bold cyan]●[/] {escape(''.join(stream_buf))}")
            for tc in tcs:
                console.print(
                    f"  [dim cyan]⎿[/] [bold]{escape(str(tc['name']))}[/]"
                    f"({_fmt_args(tc.get('args'))})"
                )
            _reset_stream()
        elif kind == "tool_call":
            # Live "starting" hint for long-running tools. We don't repeat the
            # name here since assistant_message already announced it via ⎿.
            console.print(f"    [dim]⋯ running {escape(str(payload.get('tool_name')))}…[/]")
        elif kind == "tool_result":
            snippet = (payload.get("output") or "").rstrip()
            if len(snippet) > 600:
                snippet = snippet[:600] + "…"
            snippet = escape(snippet).replace("\n", "\n    ")
            console.print(f"    [green]✓[/] {snippet}")
        elif kind == "tool_error":
            output = escape(str(payload.get("output") or "")).replace("\n", "\n    ")
            console.print(f"    [red]✗[/] {output}")
        elif kind == "permission_denied":
            console.print(f"    [yellow]⛔ denied[/] {escape(str(payload))}")
        elif kind == "error":
            console.print(f"    [bold red]error[/] {escape(str(payload))}")
        elif kind == "mem_compacted":
            tok_b = payload.get("tokens_before", 0)
            tok_a = payload.get("tokens_after", 0)
            sections = payload.get("sections_added", 0)
            console.print(
                f"  [dim]◈ memory compressed · {sections} section(s) · {tok_b}→{tok_a} tok[/]"
            )
        elif kind == "mem_ltm_promoted":
            n = len(payload.get("additions") or [])
            console.print(f"  [dim]◈ {n} fact(s) promoted to long-term memory[/]")
        elif kind == "mem_ltm_forgotten":
            dropped = payload.get("dropped_count", 0)
            console.print(
                f"  [dim]◈ long-term memory pruned · {dropped} entr{'y' if dropped == 1 else 'ies'} removed[/]"
            )


def _short(v: Any, limit: int = 60) -> str:
    s = str(v).replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


async def _input_reader(client: IPCClient, eid: str, cancel: anyio.CancelScope) -> None:
    """Read user input via prompt_toolkit; send as message.send. Slash commands
    are handled locally."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.patch_stdout import patch_stdout

    hist_path = paths.eonlet_dir(eid) / ".history"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    session: PromptSession[str] = PromptSession(
        history=FileHistory(str(hist_path)),
        auto_suggest=AutoSuggestFromHistory(),
    )
    # Claude-Code-style prompt: just a bold cyan chevron. The agent's name is
    # already shown in the attach banner; repeating it on every prompt line
    # confuses "who am I talking to" with "who is typing".
    prompt_text = ANSI("\n\x1b[1;36m>\x1b[0m ")

    while True:
        try:
            with patch_stdout(raw=True):
                line = await session.prompt_async(prompt_text)
        except (EOFError, KeyboardInterrupt):
            console.print("[dim]detaching[/]")
            cancel.cancel()
            return
        line = (line or "").strip()
        if not line:
            continue
        if line.startswith("/"):
            done = await _handle_slash(line, client, eid, cancel)
            if done:
                return
            continue
        await client.request("message.send", {"content": line})


async def _handle_slash(line: str, client: IPCClient, eid: str, cancel: anyio.CancelScope) -> bool:
    """Return True if the caller should exit the input loop."""
    cmd, _, rest = line[1:].partition(" ")
    cmd = cmd.lower()
    rest = rest.strip()
    if cmd in {"quit", "exit", "q"}:
        console.print("[dim]detaching[/]")
        cancel.cancel()
        return True
    if cmd == "help":
        _print_help()
        return False
    if cmd == "clear":
        console.clear()
        return False
    if cmd == "status":
        info = await client.request("session.start", {"client_id": "cli"})
        _print_attach_banner(eid, readonly=False, info=info)
        return False
    if cmd in {"triggers", "trigger"}:
        await _handle_trigger_slash(rest, client)
        return False
    if cmd in {"note", "notes"}:
        await _handle_note_slash(rest, client)
        return False
    if cmd in {"todo", "todos"}:
        await _handle_todo_slash(rest, client)
        return False
    if cmd == "compact":
        await _handle_compact_slash(rest, client)
        return False
    if cmd == "memory":
        await _handle_memory_slash(rest, client)
        return False
    console.print(f"[yellow]unknown command:[/] {line}  (try /help)")
    return False


async def _handle_trigger_slash(rest: str, client: IPCClient) -> None:
    """Dispatch `/trigger ...` subcommands.

    Forms:
      /trigger                          — list all
      /trigger ls                       — list all
      /trigger add <cron> <tz> <msg…>   — add dynamic trigger
      /trigger rm <id>                  — remove dynamic trigger
      /trigger on  <id>                 — enable
      /trigger off <id>                 — disable
      /trigger clear                    — drop all dynamic triggers (asks confirm)
    """
    from rich.table import Table

    sub, _, srest = rest.partition(" ")
    sub = sub.lower()
    srest = srest.strip()

    if not sub or sub == "ls" or sub == "list":
        info = await client.request("triggers.list", {})
        trigs = (info or {}).get("triggers") or []
        if not trigs:
            console.print("[dim](no triggers)[/]")
            return
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID")
        t.add_column("KIND")
        t.add_column("MODE")
        t.add_column("SCHEDULE")
        t.add_column("TZ")
        t.add_column("ENABLED")
        t.add_column("NEXT")
        for tr in trigs:
            t.add_row(
                str(tr.get("id", "?")),
                str(tr.get("kind", "?")),
                str(tr.get("mode", "cron")),
                str(tr.get("schedule", "?")),
                str(tr.get("timezone", "?")),
                "yes" if tr.get("enabled") else "no",
                str(tr.get("next_fire_at") or "—"),
            )
        console.print(t)
        return

    if sub == "add":
        # Format: <cron-with-5-fields> <tz> <message...>. Cron has spaces, so
        # split off exactly the first 5 + tz.
        parts = srest.split()
        if len(parts) < 7:
            console.print("[yellow]usage:[/] /trigger add <m h dom mon dow> <tz> <message…>")
            return
        cron_expr = " ".join(parts[:5])
        tz = parts[5]
        message = " ".join(parts[6:])
        resp = await client.request(
            "triggers.add",
            {"schedule": cron_expr, "timezone": tz, "message": message},
        )
        if resp.get("ok"):
            console.print(f"[green]added[/] {resp.get('trigger_id')}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub == "once":
        # /trigger once <ISO-datetime-with-tz-offset> <tz> <message…>
        parts = srest.split()
        if len(parts) < 3:
            console.print("[yellow]usage:[/] /trigger once <ISO-datetime+tz> <tz> <message…>")
            return
        fire_at, tz, *msg_parts = parts
        resp = await client.request(
            "triggers.add_once",
            {"fire_at": fire_at, "timezone": tz, "message": " ".join(msg_parts)},
        )
        if resp.get("ok"):
            console.print(
                f"[green]added[/] {resp.get('trigger_id')} → fires at {resp.get('fire_at')}"
            )
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub == "in":
        # /trigger in <duration> <tz> <message…>  (e.g. /trigger in 30m UTC Stretch your legs)
        parts = srest.split()
        if len(parts) < 3:
            console.print("[yellow]usage:[/] /trigger in <duration like 30m|2h|1d> <tz> <message…>")
            return
        dur, tz, *msg_parts = parts
        resp = await client.request(
            "triggers.add_once",
            {"in": dur, "timezone": tz, "message": " ".join(msg_parts)},
        )
        if resp.get("ok"):
            console.print(
                f"[green]added[/] {resp.get('trigger_id')} → fires at {resp.get('fire_at')}"
            )
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub == "rm" or sub == "remove":
        if not srest:
            console.print("[yellow]usage:[/] /trigger rm <id>")
            return
        resp = await client.request("triggers.remove", {"trigger_id": srest})
        if resp.get("ok"):
            console.print(f"[green]removed[/] {srest}")
        elif "error" in resp:
            console.print(f"[red]error:[/] {resp['error']}")
        else:
            console.print(f"[yellow]not found:[/] {srest}")
        return

    if sub in {"on", "enable"}:
        if not srest:
            console.print("[yellow]usage:[/] /trigger on <id>")
            return
        resp = await client.request("triggers.set_enabled", {"trigger_id": srest, "enabled": True})
        if resp.get("ok"):
            console.print(f"[green]enabled[/] {srest}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'not found')}")
        return

    if sub in {"off", "disable"}:
        if not srest:
            console.print("[yellow]usage:[/] /trigger off <id>")
            return
        resp = await client.request("triggers.set_enabled", {"trigger_id": srest, "enabled": False})
        if resp.get("ok"):
            console.print(f"[green]disabled[/] {srest}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'not found')}")
        return

    if sub == "clear":
        if srest.strip().lower() != "yes":
            console.print(
                "[yellow]/trigger clear yes[/] to drop ALL dynamic triggers "
                "(static triggers from agent.yaml are kept)"
            )
            return
        resp = await client.request("triggers.clear", {})
        console.print(f"[green]cleared[/] {resp.get('cleared', 0)} dynamic trigger(s)")
        return

    console.print(f"[yellow]unknown trigger subcommand:[/] {sub}  (try /trigger)")


async def _handle_note_slash(rest: str, client: IPCClient) -> None:
    """Dispatch ``/note ...`` subcommands.

    Forms:
      /note                        — list all
      /note list [tag]             — list (optional tag filter)
      /note add <text>             — add a note
      /note get <id>               — show one
      /note rm <id>                — delete
    """
    from rich.table import Table

    sub, _, srest = rest.partition(" ")
    sub = sub.lower()
    srest = srest.strip()

    if not sub or sub in {"ls", "list"}:
        params: dict[str, Any] = {}
        if srest:
            params["tags"] = [srest]
        resp = await client.request("memory.note.list", params)
        notes = (resp or {}).get("notes") or []
        if not notes:
            console.print("[dim](no notes)[/]")
            return
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID")
        t.add_column("TITLE")
        t.add_column("TAGS")
        t.add_column("CREATED")
        for n in notes:
            t.add_row(
                str(n.get("id", "?")),
                str(n.get("title") or ""),
                ", ".join(n.get("tags") or []),
                str(n.get("created_at") or ""),
            )
        console.print(t)
        return

    if sub == "add":
        if not srest:
            console.print("[yellow]usage:[/] /note add <text>")
            return
        resp = await client.request("memory.note.add", {"content": srest})
        if resp.get("ok"):
            console.print(f"[green]added[/] {resp.get('id')}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub == "get":
        if not srest:
            console.print("[yellow]usage:[/] /note get <id>")
            return
        resp = await client.request("memory.note.get", {"id": srest})
        if not resp.get("ok"):
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
            return
        n = resp["note"]
        header = f"[bold]{n['id']}[/]"
        if n.get("title"):
            header += f" — {n['title']}"
        if n.get("tags"):
            header += "  (tags: " + ", ".join(n["tags"]) + ")"
        console.print(header)
        if n.get("body"):
            console.print(n["body"])
        return

    if sub in {"rm", "delete", "del"}:
        if not srest:
            console.print("[yellow]usage:[/] /note rm <id>")
            return
        resp = await client.request("memory.note.delete", {"id": srest})
        if resp.get("ok"):
            console.print(f"[green]deleted[/] {srest}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    console.print(f"[yellow]unknown note subcommand:[/] {sub}  (try /help)")


async def _handle_todo_slash(rest: str, client: IPCClient) -> None:
    """Dispatch ``/todo ...`` subcommands.

    Forms:
      /todo                        — list pending
      /todo list [status]          — list with status filter
      /todo add <text>             — add pending todo
      /todo done <id>              — mark done
      /todo rm <id>                — delete
    """
    from rich.table import Table

    sub, _, srest = rest.partition(" ")
    sub = sub.lower()
    srest = srest.strip()

    if not sub or sub in {"ls", "list"}:
        status = srest or "pending"
        if status not in ("pending", "done", "cancelled", "all"):
            console.print(f"[yellow]bad status:[/] {status}")
            return
        resp = await client.request("memory.todo.list", {"status": status})
        todos = (resp or {}).get("todos") or []
        if not todos:
            console.print(f"[dim](no {status} todos)[/]")
            return
        t = Table(show_header=True, header_style="bold")
        t.add_column("ID")
        t.add_column("STATUS")
        t.add_column("DUE")
        t.add_column("CONTENT")
        for td in todos:
            t.add_row(
                str(td.get("id", "?")),
                str(td.get("status", "?")),
                str(td.get("due") or "—"),
                str(td.get("content", "")),
            )
        console.print(t)
        return

    if sub == "add":
        if not srest:
            console.print("[yellow]usage:[/] /todo add <text>")
            return
        resp = await client.request("memory.todo.add", {"content": srest})
        if resp.get("ok"):
            console.print(f"[green]added[/] {resp.get('id')}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub == "done":
        if not srest:
            console.print("[yellow]usage:[/] /todo done <id>")
            return
        resp = await client.request("memory.todo.done", {"id": srest})
        if resp.get("ok"):
            console.print(f"[green]done[/] {srest}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    if sub in {"rm", "delete", "del"}:
        if not srest:
            console.print("[yellow]usage:[/] /todo rm <id>")
            return
        resp = await client.request("memory.todo.delete", {"id": srest})
        if resp.get("ok"):
            console.print(f"[green]deleted[/] {srest}")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return

    console.print(f"[yellow]unknown todo subcommand:[/] {sub}  (try /help)")


async def _handle_compact_slash(rest: str, client: IPCClient) -> None:
    """``/compact`` family.

    Forms:
      /compact            — force tier-1 now
      /compact off        — disable auto-compaction (session)
      /compact on         — enable auto-compaction
    """
    arg = rest.strip().lower()
    if arg in {"off", "pause"}:
        resp = await client.request("memory.pause", {})
        if resp.get("ok"):
            console.print("[green]auto-compaction paused[/] (session)")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return
    if arg in {"on", "resume"}:
        resp = await client.request("memory.resume", {})
        if resp.get("ok"):
            console.print("[green]auto-compaction resumed[/]")
        else:
            console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return
    if arg and arg not in {"now", "run"}:
        console.print(f"[yellow]unknown /compact argument:[/] {arg}  (try /help)")
        return
    resp = await client.request("memory.compact", {})
    if not resp.get("ok"):
        console.print(f"[red]compact failed:[/] {resp.get('error', 'unknown error')}")
        return
    if not resp.get("ran"):
        console.print("[dim]compact: nothing to do (no events past watermark)[/]")
        return
    console.print(
        f"[green]compacted[/] "
        f"{resp.get('tokens_before', 0)}→{resp.get('tokens_after', 0)} tokens, "
        f"{resp.get('sections_added', 0)} STM sections, "
        f"watermark→{resp.get('boundary_event_id', '?')}"
    )


async def _handle_memory_slash(rest: str, client: IPCClient) -> None:
    """``/memory show [store]`` — render one or all memory stores."""
    sub, _, srest = rest.partition(" ")
    sub = sub.lower()
    srest = srest.strip()
    if sub != "show":
        console.print("[yellow]usage:[/] /memory show [stm|ltm|notes|todos|all]")
        return
    store = srest or "all"
    if store not in {"stm", "ltm", "notes", "todos", "all"}:
        console.print(f"[yellow]unknown store:[/] {store}")
        return
    resp = await client.request("memory.show", {"store": store})
    if not resp.get("ok"):
        console.print(f"[red]error:[/] {resp.get('error', 'failed')}")
        return
    auto = "on" if resp.get("auto_compact_enabled") else "off"
    console.print(f"[dim]auto-compact: {auto}[/]")
    if "stm" in resp:
        console.print("[bold]short_term[/]")
        console.print(resp["stm"] or "[dim](empty)[/]")
    if "ltm" in resp:
        console.print("[bold]long_term[/]")
        console.print(resp["ltm"] or "[dim](empty)[/]")
    if "notes" in resp:
        console.print("[bold]notes[/]")
        for n in resp["notes"] or []:
            head = f"[{n['id']}] {n.get('title') or ''}"
            if n.get("tags"):
                head += "  (tags: " + ", ".join(n["tags"]) + ")"
            console.print(head)
            if n.get("body"):
                console.print(n["body"])
        if not resp["notes"]:
            console.print("[dim](empty)[/]")
    if "todos" in resp:
        console.print("[bold]todos[/]")
        for t in resp["todos"] or []:
            icon = {"pending": "[ ]", "done": "[x]", "cancelled": "[-]"}.get(t["status"], "[?]")
            console.print(f"{icon} {t['id']} — {t.get('content', '')}")
        if not resp["todos"]:
            console.print("[dim](empty)[/]")


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
    """One-line summary for ``replay --compact`` and ``tail`` streams.

    Intentionally truncates — for the full picture, use the default (verbose)
    replay format or ``--format jsonl``.
    """
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
    if kind in {"permission_granted", "permission_denied", "permission_requested"}:
        return f"{payload.get('tool_name', '?')} rule={payload.get('rule', '?')}"
    return str(payload)[:120]


def cmd_replay(
    eonlet_id: str,
    from_: int | None,
    to: int | None,
    *,
    fmt: str = "human",
    compact: bool = False,
    head: int | None = None,
    tail: int | None = None,
) -> None:
    """Print events from state.db. Read-only — never re-executes anything.

    Three formats:

    - ``human`` (default) — block-per-event, full content, no truncation.
      The intent is "everything the LLM saw, exactly as it saw it" so a
      truncated tool_result in the rendered log can never hide a truncated
      tool_result in the actual conversation.
    - ``jsonl`` — one JSON event per line; for diffing / piping into ``jq``.
    - ``json`` — single JSON array; convenient for ``cat | jq``.

    ``--compact`` keeps the old one-line-per-event rendering for grep/scan.
    """
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
    if head is not None:
        events = events[:head]
    if tail is not None:
        events = events[-tail:]

    if fmt == "jsonl":
        for e in events:
            console.print_json(data=_event_to_dict(e), indent=None)
        return
    if fmt == "json":
        import json as _json

        console.print(
            _json.dumps([_event_to_dict(e) for e in events], indent=2, ensure_ascii=False)
        )
        return

    # human
    if compact:
        for e in events:
            ts = _iso_us(e.ts)
            summary = _summarize_payload(str(e.kind), e.payload)
            console.print(f"[dim]{ts}[/] #{e.id:>4} [bold]{e.kind}[/] {summary}")
        return

    _render_human(events)


def _event_to_dict(e: Any) -> dict[str, Any]:
    """Stable serialization for jsonl/json formats. ts as ISO + raw us."""
    return {
        "id": e.id,
        "ts": _iso_us(e.ts, micros=True),
        "ts_us": e.ts,
        "kind": str(e.kind),
        "parent_id": e.parent_id,
        "trigger_id": e.trigger_id,
        "cost_usd": e.cost_usd,
        "tokens_in": e.tokens_in,
        "tokens_out": e.tokens_out,
        "payload": e.payload,
    }


def _iso_us(us: int, *, micros: bool = False) -> str:
    import datetime as _dt

    spec = "microseconds" if micros else "seconds"
    return _dt.datetime.fromtimestamp(us / 1_000_000).isoformat(timespec=spec)


# ── Verbose human rendering for `eonlet replay` ──────────────────────────────


def _render_human(events: list[Any]) -> None:
    """One block per event. Full content. No fixed-length truncation."""
    n = len(events)
    if n == 0:
        console.print("[dim](no events)[/]")
        return
    first_ts = _iso_us(events[0].ts, micros=True)
    last_ts = _iso_us(events[-1].ts, micros=True)
    console.print(
        f"[dim]── {n} event(s) · "
        f"id [{events[0].id}..{events[-1].id}] · "
        f"{first_ts} → {last_ts} ──[/]"
    )
    for e in events:
        _render_event(e)


def _render_event(e: Any) -> None:
    kind = str(e.kind)
    ts = _iso_us(e.ts, micros=True)
    # Header line — kind in bold, plus any of the optional metadata that's set.
    bits: list[str] = []
    if e.parent_id is not None:
        bits.append(f"parent=#{e.parent_id}")
    if e.trigger_id:
        bits.append(f"trigger={e.trigger_id}")
    if e.tokens_in is not None or e.tokens_out is not None:
        bits.append(f"tokens {e.tokens_in or 0}→{e.tokens_out or 0}")
    if e.cost_usd is not None:
        bits.append(f"cost=${e.cost_usd:.4f}")
    extra = ("  " + "  ".join(bits)) if bits else ""
    console.print(f"\n[bold cyan]─── #{e.id}[/]  [dim]{ts}[/]  [bold]{kind}[/]{extra}")

    body = _render_payload(kind, e.payload)
    if body:
        console.print(body)


def _render_payload(kind: str, payload: dict[str, Any]) -> str:
    """Per-kind verbose body. Never truncates."""
    p = payload or {}

    if kind == "user_message":
        return _indent(p.get("content") or "(empty)")

    if kind == "assistant_message":
        out: list[str] = []
        content = p.get("content") or ""
        if content:
            out.append(_indent(content))
        tcs = p.get("tool_calls") or []
        if tcs:
            out.append(f"  [dim]tool_calls ({len(tcs)}):[/]")
            for i, tc in enumerate(tcs, 1):
                tn = tc.get("tool_name") or tc.get("name") or "?"
                cid = tc.get("call_id") or tc.get("id") or ""
                out.append(f"    [bold][{i}][/] {tn}  [dim]call_id={cid}[/]")
                args = tc.get("args") or tc.get("input") or {}
                out.append(_pretty_args(args, indent=8))
        return "\n".join(out) or _indent("(empty)")

    if kind == "tool_call":
        out = [
            f"  [dim]tool[/] = {p.get('tool_name', '?')}",
            f"  [dim]call_id[/] = {p.get('call_id', '?')}",
            "  [dim]args:[/]",
            _pretty_args(p.get("args") or {}, indent=4),
        ]
        return "\n".join(out)

    if kind in {"tool_result", "tool_error"}:
        out = [
            f"  [dim]tool[/] = {p.get('tool_name', '?')}",
            f"  [dim]call_id[/] = {p.get('call_id', '?')}",
        ]
        if "output" in p:
            out.append("  [dim]output:[/]")
            out.append(_indent(str(p["output"]), prefix="    │ "))
        return "\n".join(out)

    if kind in {"permission_requested", "permission_granted", "permission_denied"}:
        rows = [f"  {k}: {v}" for k, v in p.items()]
        return "\n".join(rows) if rows else ""

    if kind in {"trigger_fired", "trigger_completed", "trigger_failed", "trigger_skipped"}:
        rows = [f"  {k}: {v}" for k, v in p.items()]
        return "\n".join(rows) if rows else ""

    # Fallback: pretty dict
    if p:
        return _pretty_args(p, indent=2)
    return ""


def _pretty_args(args: dict[str, Any] | Any, *, indent: int = 4) -> str:
    """Render tool args as `key: value`, multiline-content as a fenced block.

    String values containing newlines or longer than 80 chars are shown as a
    block underneath the key so the full content is preserved verbatim.
    """
    pad = " " * indent
    if not isinstance(args, dict):
        return _indent(str(args), prefix=pad)
    lines: list[str] = []
    for k, v in args.items():
        if isinstance(v, str) and ("\n" in v or len(v) > 80):
            lines.append(f"{pad}{k}:")
            lines.append(_indent(v, prefix=pad + "  │ "))
        elif isinstance(v, dict | list):
            import json as _json

            rendered = _json.dumps(v, indent=2, ensure_ascii=False)
            lines.append(f"{pad}{k}:")
            lines.append(_indent(rendered, prefix=pad + "  "))
        else:
            lines.append(f"{pad}{k}: {v}")
    return "\n".join(lines)


def _indent(text: str, *, prefix: str = "    ") -> str:
    if not text:
        return prefix + "(empty)"
    return "\n".join(prefix + line for line in text.splitlines())


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


# ── go ───────────────────────────────────────────────────────────────────────


def cmd_go(eonlet_id: str) -> None:
    """`eonlet go` — spawn an interactive shell inside the eonlet's instance directory."""
    import tempfile
    from pathlib import Path as _Path

    eid = resolve_eonlet_id(eonlet_id)
    eonlet_dir = paths.eonlet_dir(eid)
    if not eonlet_dir.exists():
        fail(f"{eid}: instance directory does not exist", code=3)

    shell = os.environ.get("SHELL", "/bin/sh")
    shell_name = _Path(shell).name
    prompt_tag = f"(eonlet:{eid})"

    console.rule(f"[bold cyan]{prompt_tag}[/]  [dim]{eonlet_dir}[/]")
    console.print("[dim]Type 'exit' or Ctrl+D to return.[/]\n")

    env = os.environ.copy()

    if shell_name == "bash":
        # --rcfile lets us source ~/.bashrc then prepend our tag to PS1.
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("[ -f ~/.bashrc ] && source ~/.bashrc\n")
            f.write(f'PS1="{prompt_tag} $PS1"\n')
            tmp = f.name
        try:
            subprocess.call([shell, "--rcfile", tmp], cwd=str(eonlet_dir), env=env)
        finally:
            _Path(tmp).unlink(missing_ok=True)
    elif shell_name == "zsh":
        # ZDOTDIR points to a temp dir whose .zshrc sources the real one then patches PROMPT.
        real_zdotdir = os.environ.get("ZDOTDIR", str(_Path("~").expanduser()))
        with tempfile.TemporaryDirectory() as tmpdir:
            zshrc = _Path(tmpdir) / ".zshrc"
            zshrc.write_text(
                f'ZDOTDIR="{real_zdotdir}"\n'
                f'[ -f "$ZDOTDIR/.zshrc" ] && source "$ZDOTDIR/.zshrc"\n'
                f'PROMPT="{prompt_tag} $PROMPT"\n',
                encoding="utf-8",
            )
            env["ZDOTDIR"] = tmpdir
            subprocess.call([shell], cwd=str(eonlet_dir), env=env)
    else:
        env["PS1"] = f"{prompt_tag} $ "
        subprocess.call([shell], cwd=str(eonlet_dir), env=env)

    console.rule("[dim]back[/]")


# ── status ───────────────────────────────────────────────────────────────────


def cmd_status(eonlet_id: str, as_json: bool = False) -> None:
    """`eonlet status` — rich runtime snapshot (tokens, memory tiers, triggers, activity)."""
    from .status import collect, render

    eid = resolve_eonlet_id(eonlet_id)
    report = collect(eid)
    if as_json:
        console.print_json(data=report.model_dump())
    else:
        render(report, console)


# ── memory migrate ───────────────────────────────────────────────────────────


def cmd_memory_migrate(
    legacy_dir: Path,
    eonlet_id: str,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    from ..memory.ltm import LTMStore
    from ..memory.migrate import apply_migration, migrate_legacy_memory
    from ..memory.paths import long_term_path

    if not legacy_dir.exists():
        fail(f"legacy_dir not found: {legacy_dir}", code=3)

    eid = resolve_eonlet_id(eonlet_id)
    mem_dir = paths.memory_dir(eid)
    ltm_path = long_term_path(mem_dir)

    if ltm_path.exists() and not force and not dry_run:
        fail(
            f"{eid} already has long_term.md. Pass --force to overwrite or --dry-run to preview.",
            code=4,
        )

    result = migrate_legacy_memory(legacy_dir)

    if result.errors:
        for e in result.errors:
            console.print(f"[red]error:[/] {e}")
    if result.skipped:
        for s in result.skipped:
            console.print(f"[yellow]skip:[/] {s}")

    if not result.bullets:
        console.print("[yellow]nothing to migrate[/]")
        return

    if dry_run:
        console.print(f"[bold]Dry run — {len(result.bullets)} bullet(s) would be written:[/]")
        for b in result.bullets:
            console.print(f"  [{b.category}] {b.content[:80]}")
        return

    mem_dir.mkdir(parents=True, exist_ok=True)
    store = LTMStore(mem_dir)
    written = anyio.run(lambda: apply_migration(result, store))
    console.print(
        f"[green]migrated {written} bullet(s)[/] to {eid} "
        f"({len(result.skipped)} skipped, {len(result.errors)} errors)"
    )


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
    p = Path(__file__).resolve().parents[1] / "templates" / "config.yaml"
    return p.read_text(encoding="utf-8")
