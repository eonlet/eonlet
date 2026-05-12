"""Builtin tool basics — file_read/write/edit, glob, grep, notes, bash, skill."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.runtime.definition import Skill
from eonlet.tools import builtin as _builtin  # noqa: F401 — register
from eonlet.tools.builtin.bash import BashArgs, BashTool
from eonlet.tools.builtin.files import (
    FileEditArgs,
    FileEditTool,
    FileReadArgs,
    FileReadTool,
    FileWriteArgs,
    FileWriteTool,
    GlobArgs,
    GlobTool,
    GrepArgs,
    GrepTool,
)
from eonlet.tools.builtin.skill_tool import LoadSkillArgs, LoadSkillTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path, *, skills: dict | None = None) -> ToolContext:
    ws = tmp_path / "ws"
    mem = tmp_path / "mem"
    ws.mkdir()
    mem.mkdir()
    return ToolContext(
        eonlet_id="t.x",
        workspace=ws,
        memory_dir=mem,
        skills=skills or {},
        env={},
    )


# ── file_read / file_write / file_edit ───────────────────────────────────────


def test_file_write_then_read(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    write_r = anyio.run(
        FileWriteTool().__call__, FileWriteArgs(path="hello.txt", content="abc"), ctx
    )
    assert not write_r.is_error and "wrote 3 bytes" in write_r.content
    read_r = anyio.run(FileReadTool().__call__, FileReadArgs(path="hello.txt"), ctx)
    assert not read_r.is_error
    assert "abc" in read_r.content


def test_file_edit_replaces_exact_match(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.workspace / "src.py").write_text("def foo(): pass\n", encoding="utf-8")
    edit = anyio.run(
        FileEditTool().__call__,
        FileEditArgs(path="src.py", search="foo", replace="bar"),
        ctx,
    )
    assert not edit.is_error
    assert (ctx.workspace / "src.py").read_text() == "def bar(): pass\n"


def test_file_edit_refuses_mismatched_count(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.workspace / "f.txt").write_text("a a a", encoding="utf-8")
    out = anyio.run(
        FileEditTool().__call__,
        FileEditArgs(path="f.txt", search="a", replace="b", expected_count=1),
        ctx,
    )
    assert out.is_error and "3 times" in out.content


def test_file_write_rejects_workspace_prefix(tmp_path: Path) -> None:
    """A leading ``workspace/`` in a tool path is a model-side mistake — the
    workspace is already the cwd for every file tool and for ``bash``.
    Silent-stripping the prefix used to "fix" file_write in isolation, but the
    LLM would then reuse the same path string in ``bash("python workspace/foo.py")``
    and hit ``<workspace>/workspace/foo.py``. Failing loudly here teaches one
    consistent rule. See `docs/MANUAL_TEST_SCENARIOS.md` M-A1 for the bug
    that motivated this."""
    ctx = _ctx(tmp_path)
    r = anyio.run(
        FileWriteTool().__call__,
        FileWriteArgs(path="workspace/notes.md", content="hi"),
        ctx,
    )
    assert r.is_error
    assert "workspace/" in r.content and "notes.md" in r.content
    # Nothing should have been written under the doubled path either.
    assert not (ctx.workspace / "workspace").exists()
    assert not (ctx.workspace / "notes.md").exists()


def test_file_write_bare_relative_path_works(tmp_path: Path) -> None:
    """The correct form: bare relative path, resolved against the workspace."""
    ctx = _ctx(tmp_path)
    r = anyio.run(
        FileWriteTool().__call__,
        FileWriteArgs(path="notes.md", content="hi"),
        ctx,
    )
    assert not r.is_error, r.content
    assert (ctx.workspace / "notes.md").read_text() == "hi"


def test_file_read_missing_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = anyio.run(FileReadTool().__call__, FileReadArgs(path="nope.txt"), ctx)
    assert out.is_error and "not found" in out.content


# ── glob / grep ──────────────────────────────────────────────────────────────


def test_glob_finds_files(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.workspace / "a.py").write_text("x")
    (ctx.workspace / "b.py").write_text("x")
    (ctx.workspace / "c.txt").write_text("x")
    out = anyio.run(GlobTool().__call__, GlobArgs(pattern="*.py"), ctx)
    paths = out.structured_output["paths"]
    assert len(paths) == 2 and all(p.endswith(".py") for p in paths)


def test_grep_matches_regex(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.workspace / "f.py").write_text("foo\nbar\nFOObar\n", encoding="utf-8")
    out = anyio.run(GrepTool().__call__, GrepArgs(pattern=r"foo"), ctx)
    assert "foo" in out.content
    assert out.structured_output["total"] == 1  # case-sensitive by default


def test_grep_bad_regex(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = anyio.run(GrepTool().__call__, GrepArgs(pattern="("), ctx)
    assert out.is_error and "bad regex" in out.content


# ── bash ─────────────────────────────────────────────────────────────────────


def test_bash_runs_echo(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = anyio.run(BashTool().__call__, BashArgs(command="echo hello"), ctx)
    assert not out.is_error
    assert "hello" in out.content
    assert out.structured_output["return_code"] == 0


def test_bash_rejects_cwd_outside_workspace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = anyio.run(BashTool().__call__, BashArgs(command="pwd", cwd="../.."), ctx)
    assert out.is_error and "outside workspace" in out.content


# ── load_skill ───────────────────────────────────────────────────────────────


def test_load_skill_returns_body(tmp_path: Path) -> None:
    skill = Skill(name="demo", description="d", body="# demo body\n\ncontent")
    ctx = _ctx(tmp_path, skills={"demo": skill})
    out = anyio.run(LoadSkillTool().__call__, LoadSkillArgs(name="demo"), ctx)
    assert not out.is_error
    assert "# demo body" in out.content


def test_load_skill_missing(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, skills={"demo": Skill("demo", "d", "x")})
    out = anyio.run(LoadSkillTool().__call__, LoadSkillArgs(name="nope"), ctx)
    assert out.is_error and "unknown skill" in out.content
