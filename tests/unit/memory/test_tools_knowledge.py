"""`knowledge` builtin tool — action dispatch, permission flags, events (ADR-0005)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.knowledge import KnowledgeArgs, KnowledgeTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path) -> tuple[ToolContext, list[Event]]:
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        stamped = ev.model_copy(update={"id": len(captured) + 1})
        captured.append(stamped)
        return stamped

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
        record_event=record,
    )
    return ctx, captured


def test_tool_is_destructive() -> None:
    assert KnowledgeTool.annotations.destructive is True
    assert KnowledgeTool.annotations.read_only is False


def test_write_then_open_and_list(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = KnowledgeTool()

    async def go() -> None:
        w = await tool(
            KnowledgeArgs(
                action="write",
                path="rules/testing.md",
                content="never mock the DB",
                index_line="DB test rule",
            ),
            ctx,
        )
        assert not w.is_error and w.structured_output == {"path": "rules/testing.md"}

        opened = await tool(KnowledgeArgs(action="open", path="rules/testing.md"), ctx)
        assert "never mock the DB" in opened.content

        listed = await tool(KnowledgeArgs(action="list"), ctx)
        assert "[Testing](rules/testing.md)" in listed.content
        assert "DB test rule" in listed.content

    anyio.run(go)
    assert EventKind.KB_WRITTEN in [e.kind for e in captured]


def test_open_missing_is_error(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = KnowledgeTool()
    out = anyio.run(lambda: tool(KnowledgeArgs(action="open", path="nope.md"), ctx))
    assert out.is_error


def test_edit_emits_kb_written(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = KnowledgeTool()

    async def go() -> None:
        await tool(KnowledgeArgs(action="write", path="a.md", content="hello world"), ctx)
        e = await tool(
            KnowledgeArgs(action="edit", path="a.md", old_string="world", new_string="there"),
            ctx,
        )
        assert not e.is_error
        body = await tool(KnowledgeArgs(action="open", path="a.md"), ctx)
        assert "hello there" in body.content

    anyio.run(go)
    written = [e for e in captured if e.kind == EventKind.KB_WRITTEN]
    assert any(e.payload.get("action") == "edit" for e in written)


def test_delete_and_move_events(tmp_path: Path) -> None:
    ctx, captured = _ctx(tmp_path)
    tool = KnowledgeTool()

    async def go() -> None:
        await tool(KnowledgeArgs(action="write", path="a.md", content="x", index_line="h"), ctx)
        mv = await tool(KnowledgeArgs(action="move", path="a.md", new_path="b.md"), ctx)
        assert not mv.is_error
        dl = await tool(KnowledgeArgs(action="delete", path="b.md"), ctx)
        assert not dl.is_error

    anyio.run(go)
    kinds = [e.kind for e in captured]
    assert EventKind.KB_MOVED in kinds
    assert EventKind.KB_DELETED in kinds


def test_path_escape_is_error_not_exception(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = KnowledgeTool()
    out = anyio.run(
        lambda: tool(KnowledgeArgs(action="write", path="../escape.md", content="x"), ctx)
    )
    assert out.is_error
    assert "knowledge write" in out.content


def test_missing_required_args_are_errors(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = KnowledgeTool()

    async def go() -> None:
        assert (await tool(KnowledgeArgs(action="write", path="a.md"), ctx)).is_error
        assert (await tool(KnowledgeArgs(action="open"), ctx)).is_error
        assert (await tool(KnowledgeArgs(action="edit", path="a.md"), ctx)).is_error
        assert (await tool(KnowledgeArgs(action="move", path="a.md"), ctx)).is_error

    anyio.run(go)


def test_list_empty_base(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    tool = KnowledgeTool()
    out = anyio.run(lambda: tool(KnowledgeArgs(action="list"), ctx))
    assert "empty" in out.content.lower()


def test_kb_written_events_carry_full_body(tmp_path: Path) -> None:
    # Knowledge is never auto-deleted, so the event log must be able to
    # reconstruct it (Invariant #1) — events carry the resulting full body.
    ctx, captured = _ctx(tmp_path)
    tool = KnowledgeTool()

    async def go() -> None:
        await tool(
            KnowledgeArgs(action="write", path="user.md", content="likes terse replies"),
            ctx,
        )
        await tool(
            KnowledgeArgs(
                action="edit", path="user.md", old_string="terse", new_string="concise"
            ),
            ctx,
        )

    anyio.run(go)
    writes = [e for e in captured if e.kind == EventKind.KB_WRITTEN]
    assert writes[0].payload["content"] == "likes terse replies"
    assert writes[1].payload["action"] == "edit"
    # The store normalizes files to end with a newline.
    assert writes[1].payload["content"].rstrip("\n") == "likes concise replies"
    assert writes[1].payload["size"] == len(writes[1].payload["content"])


def test_write_warns_when_index_over_budget(tmp_path: Path) -> None:
    # The agent is the only entity that can prune its index — the overflow
    # signal must ride the ToolResult, not a log line it never sees.
    from types import SimpleNamespace

    ctx, _ = _ctx(tmp_path)
    ctx.extra = {
        "runtime": SimpleNamespace(
            definition=SimpleNamespace(
                config=SimpleNamespace(
                    memory=SimpleNamespace(knowledge=SimpleNamespace(index_max_tokens=1))
                )
            )
        )
    }
    tool = KnowledgeTool()

    async def go() -> None:
        out = await tool(
            KnowledgeArgs(
                action="write",
                path="rules/a.md",
                content="x",
                index_line="a fairly long index line that costs more than one token",
            ),
            ctx,
        )
        assert not out.is_error
        assert "WARNING" in out.content and "knowledge index" in out.content

    anyio.run(go)


def test_write_no_warning_within_budget(tmp_path: Path) -> None:
    from types import SimpleNamespace

    ctx, _ = _ctx(tmp_path)
    ctx.extra = {
        "runtime": SimpleNamespace(
            definition=SimpleNamespace(
                config=SimpleNamespace(
                    memory=SimpleNamespace(knowledge=SimpleNamespace(index_max_tokens=100_000))
                )
            )
        )
    }
    tool = KnowledgeTool()

    async def go() -> None:
        out = await tool(
            KnowledgeArgs(action="write", path="rules/a.md", content="x", index_line="rule"),
            ctx,
        )
        assert "WARNING" not in out.content

    anyio.run(go)
