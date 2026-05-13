"""Typer app for the ``eonlet`` CLI."""

from __future__ import annotations

import typer

from . import commands
from .util import err_console

app = typer.Typer(
    name="eonlet",
    help="Local-first runtime for stateful AI agents.",
    no_args_is_help=True,
)
def_app = typer.Typer(help="Manage agent definitions (templates).", no_args_is_help=True)
app.add_typer(def_app, name="def")


# ── system ───────────────────────────────────────────────────────────────────


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Top-up missing files.")) -> None:
    """Set up ~/.eonlet/."""
    commands.cmd_init(force=force)


@app.command()
def version() -> None:
    """Show version info."""
    commands.cmd_version()


@app.command()
def doctor() -> None:
    """Run self-checks (writable home, API keys, SQLite, cron, definitions)."""
    commands.cmd_doctor()


@app.command()
def fire(
    id_: str = typer.Argument(..., metavar="ID"),
    trigger_id: str = typer.Argument(..., metavar="TRIGGER_ID"),
    message: str | None = typer.Option(
        None, "-m", "--message", help="Override the trigger's configured message."
    ),
) -> None:
    """Manually fire a configured trigger (skip the cron wait)."""
    commands.cmd_fire(id_, trigger_id, message)


# ── def ──────────────────────────────────────────────────────────────────────


@def_app.command("ls")
def def_ls() -> None:
    """List installed agent definitions."""
    commands.cmd_def_ls()


@def_app.command("validate")
def def_validate(name_or_path: str) -> None:
    """Validate an agent definition's syntax and semantics."""
    commands.cmd_def_validate(name_or_path)


# ── lifecycle ────────────────────────────────────────────────────────────────


@app.command()
def create(
    agent_type: str = typer.Argument(..., help="Definition type, e.g. 'assistant'."),
    name: str | None = typer.Option(None, "--name", help="Instance name (otherwise random)."),
    no_start: bool = typer.Option(
        False, "--no-start", help="Create dirs but don't start the worker."
    ),
    env: list[str] = typer.Option(
        [], "-e", help="Set env var for this instance (repeatable). VAR=value"
    ),
) -> None:
    """Spawn a new eonlet from a definition."""
    commands.cmd_create(agent_type, name, no_start, env)


@app.command(name="ls")
def ls_cmd(
    all: bool = typer.Option(False, "--all"),
    status: str | None = typer.Option(None, "--filter"),
) -> None:
    """List eonlets."""
    commands.cmd_ls(all, status)


@app.command()
def kill(
    id_: str = typer.Argument(..., metavar="ID"), force: bool = typer.Option(False, "--force")
) -> None:
    """Stop a running eonlet (SIGTERM, 5s grace, then SIGKILL)."""
    commands.cmd_kill(id_, force)


@app.command()
def rm(
    id_: str = typer.Argument(..., metavar="ID"),
    with_data: bool = typer.Option(False, "--with-data"),
    yes: bool = typer.Option(False, "-y"),
) -> None:
    """Remove a dead eonlet's directory."""
    commands.cmd_rm(id_, with_data, yes)


@app.command()
def pause(id_: str = typer.Argument(..., metavar="ID")) -> None:
    """SIGSTOP the worker — instant resume later."""
    commands.cmd_pause(id_)


@app.command()
def resume(id_: str = typer.Argument(..., metavar="ID")) -> None:
    """SIGCONT a paused worker."""
    commands.cmd_resume(id_)


# ── interact ─────────────────────────────────────────────────────────────────


@app.command()
def attach(
    id_: str = typer.Argument(..., metavar="ID"),
    readonly: bool = typer.Option(False, "--readonly"),
) -> None:
    """Open an interactive session with an eonlet."""
    commands.cmd_attach(id_, readonly)


@app.command()
def send(
    id_: str = typer.Argument(..., metavar="ID"),
    message: str = typer.Argument(...),
) -> None:
    """Send a one-shot message and print the reply."""
    commands.cmd_send(id_, message)


@app.command()
def logs(
    id_: str = typer.Argument(..., metavar="ID"),
    follow: bool = typer.Option(False, "-f", "--follow"),
    tail: int | None = typer.Option(None, "--tail"),
) -> None:
    """Tail the eonlet's log."""
    commands.cmd_logs(id_, follow, tail)


@app.command()
def inspect(id_: str = typer.Argument(..., metavar="ID")) -> None:
    """Dump full state as JSON."""
    commands.cmd_inspect(id_)


# ── debug / archive ──────────────────────────────────────────────────────────


@app.command()
def ps(all: bool = typer.Option(False, "--all")) -> None:
    """docker-ps-style listing with uptime, last event, next trigger."""
    commands.cmd_ps(all)


@app.command()
def tail(id_: str = typer.Argument(..., metavar="ID")) -> None:
    """Stream events from a live eonlet, one per line."""
    commands.cmd_tail(id_)


@app.command()
def replay(
    id_: str = typer.Argument(..., metavar="ID"),
    from_: int | None = typer.Option(None, "--from", help="Starting event id."),
    to: int | None = typer.Option(None, "--to", help="Ending event id (inclusive)."),
) -> None:
    """Read state.db and print every event in range. Never re-executes."""
    commands.cmd_replay(id_, from_, to)


@app.command()
def export(
    id_: str = typer.Argument(..., metavar="ID"),
    output: str = typer.Option(..., "--output", "-o", help="Destination .tar.gz"),
) -> None:
    """Archive an eonlet's directory (state + memory + workspace + meta)."""
    from pathlib import Path

    commands.cmd_export(id_, Path(output))


@app.command(name="import")
def import_cmd(
    archive: str = typer.Argument(..., metavar="ARCHIVE"),
    as_: str | None = typer.Option(None, "--as", help="Import under a different id."),
) -> None:
    """Restore an eonlet from an `eonlet export` archive."""
    from pathlib import Path

    commands.cmd_import(Path(archive), as_)


def cli_main() -> None:
    """Console-script entry point."""
    try:
        app()
    except Exception as e:
        err_console.print(f"[red]error:[/] {e}")
        raise SystemExit(1) from e


if __name__ == "__main__":
    cli_main()
