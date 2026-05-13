"""Drive the typer app end-to-end via CliRunner to cover ``cli/main.py``.

Only the offline branches — init, def ls, def validate, doctor, version, ps,
replay — since worker-bound commands are covered by the integration tests.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eonlet import paths
from eonlet.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "eonlet" in result.stdout
    assert "spec eonlet/v1" in result.stdout


def test_init_then_def_ls(isolated_home: Path, runner: CliRunner) -> None:
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["def", "ls"])
    assert r.exit_code == 0
    assert "assistant" in r.stdout
    assert "x-digest" in r.stdout


def test_def_validate_assistant(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["def", "validate", "assistant"])
    assert r.exit_code == 0
    assert "ok" in r.stdout


def test_doctor(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 0
    assert "sqlite WAL" in r.stdout


def test_ps_empty(isolated_home: Path, runner: CliRunner) -> None:
    r = runner.invoke(app, ["ps"])
    assert r.exit_code == 0


def test_create_no_start_then_replay(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    r = runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    assert r.exit_code == 0
    assert "created" in r.stdout
    # Synthesize a couple of events so replay has something to print.
    from eonlet.runtime.events import user_message
    from eonlet.runtime.store import EventStore

    store = EventStore(paths.state_db("assistant.alice"))
    store.append(user_message("hi"))
    store.append(user_message("there"))
    store.close()
    r = runner.invoke(app, ["replay", "alice"])
    assert r.exit_code == 0
    assert "user_message" in r.stdout


def test_help_top_level(runner: CliRunner) -> None:
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    assert "Local-first runtime" in r.stdout


def test_inspect_dead_eonlet(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    r = runner.invoke(app, ["inspect", "alice"])
    assert r.exit_code == 0
    assert "assistant.alice" in r.stdout
    assert "memory" in r.stdout


def test_ls_with_eonlets(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    r = runner.invoke(app, ["ls"])
    assert r.exit_code == 0
    assert "assistant.alice" in r.stdout


def test_ps_with_eonlets(isolated_home: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    r = runner.invoke(app, ["ps", "--all"])
    assert r.exit_code == 0


def test_rm_keeps_user_data_without_with_data(
    isolated_home: Path, runner: CliRunner
) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    paths.memory_dir("assistant.alice").mkdir(exist_ok=True)
    (paths.memory_dir("assistant.alice") / "notes.md").write_text("important")
    r = runner.invoke(app, ["rm", "alice", "-y"])
    assert r.exit_code == 0
    # memory/ preserved per CLI_REFERENCE; meta.json removed.
    assert (paths.memory_dir("assistant.alice") / "notes.md").exists()


def test_export_command(isolated_home: Path, tmp_path: Path, runner: CliRunner) -> None:
    runner.invoke(app, ["init"])
    runner.invoke(app, ["create", "assistant", "--name=alice", "--no-start"])
    archive = tmp_path / "out.tar.gz"
    r = runner.invoke(app, ["export", "alice", "-o", str(archive)])
    assert r.exit_code == 0
    assert archive.exists() and archive.stat().st_size > 0
