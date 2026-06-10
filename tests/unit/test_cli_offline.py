"""CLI commands that don't need a running worker: doctor, def ls/validate,
id resolution, llm provider factory.

These cover the offline branches; worker-bound flows (attach/send/fire/tail)
need subprocess integration tests, scheduled for v0.0.5.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eonlet import paths
from eonlet.cli import commands
from eonlet.cli.util import resolve_eonlet_id
from eonlet.errors import EonletNotFoundError

# ── id resolution ────────────────────────────────────────────────────────────


def test_resolve_full_id_when_present(isolated_home: Path) -> None:
    paths.eonlet_dir("assistant.alice").mkdir(parents=True)
    assert resolve_eonlet_id("assistant.alice") == "assistant.alice"


def test_resolve_bare_name_unambiguous(isolated_home: Path) -> None:
    paths.eonlet_dir("assistant.alice").mkdir(parents=True)
    assert resolve_eonlet_id("alice") == "assistant.alice"


def test_resolve_bare_name_ambiguous(isolated_home: Path) -> None:
    paths.eonlet_dir("assistant.alice").mkdir(parents=True)
    paths.eonlet_dir("other.alice").mkdir(parents=True)
    with pytest.raises(EonletNotFoundError, match="ambiguous"):
        resolve_eonlet_id("alice")


def test_resolve_missing(isolated_home: Path) -> None:
    paths.eonlets_dir().mkdir(parents=True)
    with pytest.raises(EonletNotFoundError):
        resolve_eonlet_id("ghost")


# ── /task view id resolution (partial id) ─────────────────────────────────────


def _tasks() -> list[dict[str, object]]:
    return [
        {"id": "task_abc123", "status": "done", "content": "alpha"},
        {"id": "task_abc999", "status": "pending", "content": "beta"},
        {"id": "task_xyz000", "status": "active", "content": "gamma"},
    ]


def test_resolve_task_exact_id() -> None:
    t = commands._resolve_task("task_abc123", _tasks())
    assert t is not None and t["id"] == "task_abc123"


def test_resolve_task_unique_fragment() -> None:
    # A fragment that appears in exactly one id resolves to its full task.
    t = commands._resolve_task("xyz", _tasks())
    assert t is not None and t["id"] == "task_xyz000"


def test_resolve_task_unique_suffix() -> None:
    # "某部分" — any substring, not just a prefix.
    t = commands._resolve_task("123", _tasks())
    assert t is not None and t["id"] == "task_abc123"


def test_resolve_task_ambiguous_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    # "abc" matches two ids → ambiguous, no selection.
    assert commands._resolve_task("abc", _tasks()) is None
    out = capsys.readouterr().out
    assert "ambiguous" in out


def test_resolve_task_no_match_returns_none(capsys: pytest.CaptureFixture[str]) -> None:
    assert commands._resolve_task("nope", _tasks()) is None
    assert "no such task" in capsys.readouterr().out


def test_resolve_task_exact_wins_over_fragment() -> None:
    # An exact full id wins even when it is also a substring of another id.
    tasks = [
        {"id": "task_a", "status": "done", "content": "x"},
        {"id": "task_ab", "status": "done", "content": "y"},
    ]
    t = commands._resolve_task("task_a", tasks)
    assert t is not None and t["id"] == "task_a"


# ── /tools chat-view toggle ───────────────────────────────────────────────────


def test_tools_slash_toggle_and_explicit() -> None:
    scope = commands._ViewScope()
    assert scope.verbose_tools is False  # quiet chat view by default

    commands._handle_tools_slash("", scope)  # bare toggle → on
    assert scope.verbose_tools is True
    commands._handle_tools_slash("", scope)  # toggle → off
    assert scope.verbose_tools is False

    commands._handle_tools_slash("on", scope)
    assert scope.verbose_tools is True
    commands._handle_tools_slash("off", scope)
    assert scope.verbose_tools is False

    # An unknown arg is a no-op (prints usage, leaves state alone).
    commands._handle_tools_slash("maybe", scope)
    assert scope.verbose_tools is False


# ── doctor ───────────────────────────────────────────────────────────────────


def test_doctor_runs_clean_with_init(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.cmd_init(force=False)
    commands.cmd_doctor()
    out = capsys.readouterr().out
    assert "eonlet home writable" in out
    assert "sqlite WAL" in out
    assert "cron parser" in out
    assert "definitions validate" in out


# ── def ls / validate ────────────────────────────────────────────────────────


def test_def_ls_after_init(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    commands.cmd_init(force=False)
    commands.cmd_def_ls()
    out = capsys.readouterr().out
    assert "assistant" in out
    assert "x-digest" in out
    assert "portfolio" in out


def test_def_validate_bundled_agents(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.cmd_init(force=False)
    for name in ("assistant", "x-digest", "portfolio"):
        commands.cmd_def_validate(name)
    out = capsys.readouterr().out
    # Each line includes "ok" + version.
    assert out.count("ok —") == 3


# ── llm/factory routing ──────────────────────────────────────────────────────


def test_provider_factory_routes_by_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    from eonlet.llm.factory import build_provider

    a = build_provider("claude-sonnet-4-6")
    o = build_provider("gpt-5")
    assert a.name == "anthropic" and a.model == "claude-sonnet-4-6"
    assert o.name == "openai" and o.model == "gpt-5"


def test_provider_factory_empty_model() -> None:
    from eonlet.errors import ConfigError
    from eonlet.llm.factory import build_provider

    with pytest.raises(ConfigError):
        build_provider("")


# ── config ───────────────────────────────────────────────────────────────────


def test_load_global_config_default_when_absent(isolated_home: Path) -> None:
    from eonlet.config import load_global_config

    cfg = load_global_config()
    assert isinstance(cfg.providers, dict)


def test_parse_duration_units() -> None:
    from eonlet.config import parse_duration

    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration(60) == 60.0


def test_parse_duration_rejects_bad_input() -> None:
    from eonlet.config import parse_duration
    from eonlet.errors import ConfigError

    with pytest.raises(ConfigError):
        parse_duration("twenty")
