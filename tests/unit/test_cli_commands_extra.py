"""Additional CLI command tests for coverage — offline paths only."""

from __future__ import annotations

from pathlib import Path

import pytest

from eonlet import paths
from eonlet.cli import commands
from eonlet.cli.commands import _count_messages, _short_duration

# ── cmd_version ───────────────────────────────────────────────────────────────


def test_cmd_version(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_version()
    out = capsys.readouterr().out
    assert "eonlet" in out
    assert "spec" in out
    assert "Python" in out


# ── _short_duration ───────────────────────────────────────────────────────────


def test_short_duration_seconds() -> None:
    assert _short_duration(0) == "0s"
    assert _short_duration(45) == "45s"


def test_short_duration_minutes() -> None:
    assert _short_duration(60) == "1m"
    assert _short_duration(119) == "1m"


def test_short_duration_hours() -> None:
    assert _short_duration(3600) == "1h00m"
    assert _short_duration(7800) == "2h10m"


def test_short_duration_days() -> None:
    assert _short_duration(86400) == "1d00h"
    assert _short_duration(90000) == "1d01h"


# ── _count_messages ────────────────────────────────────────────────────────────


def test_count_messages_no_db(isolated_home: Path) -> None:
    paths.eonlet_dir("test.bot").mkdir(parents=True, exist_ok=True)
    assert _count_messages("test.bot") == 0


def test_count_messages_with_db(isolated_home: Path) -> None:
    from eonlet.runtime.events import assistant_message, user_message
    from eonlet.runtime.store import EventStore

    eid = "test.cntbot"
    db = paths.state_db(eid)
    db.parent.mkdir(parents=True, exist_ok=True)
    store = EventStore(db)
    store.append(user_message("hi"))
    store.append(assistant_message("hello"))
    store.close()
    count = _count_messages(eid)
    assert count == 2


# ── cmd_ls ────────────────────────────────────────────────────────────────────


def test_cmd_ls_no_eonlets_dir(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_ls(show_all=False, status_filter=None)
    out = capsys.readouterr().out
    assert "no eonlets" in out


def test_cmd_ls_with_eonlets(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from eonlet.worker.lifecycle import write_meta

    commands.cmd_init(force=False)
    eid = "assistant.testbot"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    write_meta(
        eid,
        type_="assistant",
        name="testbot",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    commands.cmd_ls(show_all=True, status_filter=None)
    out = capsys.readouterr().out
    assert "assistant.testbot" in out


def test_cmd_ls_with_status_filter(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from eonlet.worker.lifecycle import write_meta

    commands.cmd_init(force=False)
    eid = "assistant.filtered"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    write_meta(
        eid,
        type_="assistant",
        name="filtered",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    commands.cmd_ls(show_all=True, status_filter="running")
    out = capsys.readouterr().out
    # filtered bot is not running, so should not appear
    assert "filtered" not in out


# ── cmd_ps ────────────────────────────────────────────────────────────────────


def test_cmd_ps_no_eonlets(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_ps(show_all=False)
    out = capsys.readouterr().out
    assert "no eonlets" in out


def test_cmd_ps_with_dead_eonlet(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from eonlet.worker.lifecycle import write_meta, write_status

    commands.cmd_init(force=False)
    eid = "assistant.deadbot"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    write_meta(
        eid,
        type_="assistant",
        name="deadbot",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    write_status(eid, "dead")
    commands.cmd_ps(show_all=True)
    out = capsys.readouterr().out
    assert "deadbot" in out


def test_cmd_ps_hides_dead_by_default(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from eonlet.worker.lifecycle import write_meta, write_status

    commands.cmd_init(force=False)
    eid = "assistant.hideme"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    write_meta(
        eid,
        type_="assistant",
        name="hideme",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    write_status(eid, "dead")
    commands.cmd_ps(show_all=False)
    out = capsys.readouterr().out
    assert "hideme" not in out


# ── cmd_status ────────────────────────────────────────────────────────────────


def test_cmd_status_renders(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_init(force=False)
    eid = "assistant.statbot"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    from eonlet.worker.lifecycle import write_meta

    write_meta(
        eid,
        type_="assistant",
        name="statbot",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    commands.cmd_status(eid, as_json=False)
    out = capsys.readouterr().out
    assert "statbot" in out or "assistant.statbot" in out


def test_cmd_status_as_json(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_init(force=False)
    eid = "assistant.jsonbot"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    from eonlet.worker.lifecycle import write_meta

    write_meta(
        eid,
        type_="assistant",
        name="jsonbot",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    commands.cmd_status(eid, as_json=True)
    out = capsys.readouterr().out
    assert '"id"' in out or "jsonbot" in out


# ── cmd_memory_migrate ────────────────────────────────────────────────────────


def test_cmd_memory_migrate_dry_run(
    isolated_home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.cmd_init(force=False)
    eid = "assistant.migbot"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    from eonlet.worker.lifecycle import write_meta

    write_meta(
        eid,
        type_="assistant",
        name="migbot",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    # Create a minimal legacy memory dir with one fact file
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "MEMORY.md").write_text("- [Fact](fact.md) — some fact\n", encoding="utf-8")
    fact_content = "---\nname: my-fact\ndescription: a test fact\nmetadata:\n  type: user\n---\n\nTest fact body.\n"
    (legacy_dir / "fact.md").write_text(fact_content, encoding="utf-8")

    commands.cmd_memory_migrate(
        legacy_dir=legacy_dir,
        eonlet_id=eid,
        force=False,
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert "Dry run" in out or "bullet" in out or "migrat" in out


def test_cmd_memory_migrate_missing_dir(isolated_home: Path, tmp_path: Path) -> None:
    commands.cmd_init(force=False)
    eid = "assistant.migbot2"
    paths.eonlet_dir(eid).mkdir(parents=True, exist_ok=True)
    from eonlet.worker.lifecycle import write_meta

    write_meta(
        eid,
        type_="assistant",
        name="migbot2",
        definition=paths.agent_definition_dir("assistant"),
        version="v1",
    )
    missing = tmp_path / "nonexistent"
    with pytest.raises(SystemExit):
        commands.cmd_memory_migrate(
            legacy_dir=missing,
            eonlet_id=eid,
            force=False,
            dry_run=False,
        )
