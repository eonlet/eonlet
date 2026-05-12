"""remember / forget tool tests (MEMORY_SPEC §5.2 / §5.6)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.ltm import LTMStore
from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.forget import ForgetArgs, ForgetTool
from eonlet.tools.builtin.remember import RememberArgs, RememberTool
from eonlet.tools.protocol import ToolContext


def _ctx(tmp_path: Path, events: list[Event] | None = None) -> ToolContext:
    """Build a minimal ToolContext backed by tmp_path as memory_dir."""
    captured = events if events is not None else []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    return ToolContext(
        eonlet_id="test",
        memory_dir=tmp_path,
        workspace=tmp_path,
        skills={},
        env={},
        record_event=record,
        extra={},
    )


# ── remember ─────────────────────────────────────────────────────────────────


def test_remember_writes_to_ltm(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tool = RememberTool()
    args = RememberArgs(content="sky is blue", category="fact")

    result = anyio.run(lambda: tool(args, ctx))
    assert not result.is_error
    assert "fact" in result.content

    bullets = LTMStore(tmp_path).read_bullets()
    assert len(bullets) == 1
    assert bullets[0].content == "sky is blue"
    assert bullets[0].section == "fact"
    assert bullets[0].src == "explicit"


def test_remember_default_category_is_fact(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tool = RememberTool()
    anyio.run(lambda: tool(RememberArgs(content="something"), ctx))
    bullets = LTMStore(tmp_path).read_bullets()
    assert bullets[0].section == "fact"


def test_remember_emits_event(tmp_path: Path) -> None:
    events: list[Event] = []
    ctx = _ctx(tmp_path, events)
    tool = RememberTool()
    anyio.run(lambda: tool(RememberArgs(content="preference noted", category="user"), ctx))
    evts = [e for e in events if e.kind == EventKind.MEM_REMEMBER]
    assert len(evts) == 1
    assert evts[0].payload["section"] == "user"
    assert "preference noted" in evts[0].payload["preview"]


def test_remember_user_category(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    tool = RememberTool()
    anyio.run(lambda: tool(RememberArgs(content="prefers dark mode", category="user"), ctx))
    bullets = LTMStore(tmp_path).read_bullets()
    assert bullets[0].section == "user"


def test_remember_ts_trailer_present(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    anyio.run(lambda: RememberTool()(RememberArgs(content="fact A"), ctx))
    raw = LTMStore(tmp_path).read_raw()
    assert "[src:explicit, ts:" in raw


# ── forget ────────────────────────────────────────────────────────────────────


def test_forget_dry_run_no_deletion(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "to forget", "explicit", "2026-05-23"))
    ctx = _ctx(tmp_path)
    tool = ForgetTool()
    result = anyio.run(lambda: tool(ForgetArgs(target="to forget", confirm=False), ctx))
    assert not result.is_error
    assert "Dry run" in result.content
    assert "to forget" in result.content
    # No deletion yet.
    assert len(store.read_bullets()) == 1


def test_forget_confirm_deletes_bullet(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "to forget", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "to keep", "explicit", "2026-05-23"))
    ctx = _ctx(tmp_path)
    result = anyio.run(lambda: ForgetTool()(ForgetArgs(target="to forget", confirm=True), ctx))
    assert not result.is_error
    assert "deleted" in result.content
    remaining = store.read_bullets()
    assert len(remaining) == 1
    assert remaining[0].content == "to keep"


def test_forget_emits_event(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "ephemeral", "implicit", "2026-05-23"))
    events: list[Event] = []
    ctx = _ctx(tmp_path, events)
    anyio.run(lambda: ForgetTool()(ForgetArgs(target="ephemeral", confirm=True), ctx))
    evts = [e for e in events if e.kind == EventKind.MEM_LTM_FORGOTTEN]
    assert len(evts) == 1
    assert evts[0].payload["cause"] == "forget"
    assert evts[0].payload["dropped_count"] == 1
    # M-I7: digest preserved.
    digest = evts[0].payload["dropped_digest"]
    assert any("ephemeral" in d["preview"] for d in digest)


def test_forget_no_match_returns_message(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "something", "explicit", "2026-05-23"))
    ctx = _ctx(tmp_path)
    result = anyio.run(lambda: ForgetTool()(ForgetArgs(target="no-such-content"), ctx))
    assert not result.is_error
    assert "no LTM bullets matched" in result.content


def test_forget_by_index(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "first", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "second", "explicit", "2026-05-23"))
    ctx = _ctx(tmp_path)
    anyio.run(lambda: ForgetTool()(ForgetArgs(target="fact:0", confirm=True), ctx))
    remaining = store.read_bullets()
    assert len(remaining) == 1
    assert remaining[0].content == "second"
