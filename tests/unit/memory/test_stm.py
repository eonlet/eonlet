"""STM section parsing and round-trip (MEMORY_SPEC §2.1)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.stm import STMSection, STMStore, parse, render


def _section(start: str = "2026-05-22T14:00:00+00:00", topic: str = "x") -> STMSection:
    return STMSection(
        ts_start=start,
        ts_end="2026-05-22T15:00:00+00:00",
        topic=topic,
        topics=["a", "b"],
        body="body line 1\nbody line 2",
    )


def test_render_then_parse_round_trips() -> None:
    secs = [_section(topic="alpha"), _section(start="2026-05-23T01:00:00+00:00", topic="beta")]
    text = render(secs)
    parsed = parse(text)
    assert len(parsed) == 2
    assert parsed[0].topic == "alpha"
    assert parsed[1].topic == "beta"
    assert parsed[0].topics == ["a", "b"]
    assert "body line 1" in parsed[0].body


def test_parse_accepts_ascii_dash_separator() -> None:
    raw = (
        "## [2026-05-22T14:00:00+00:00 -- 2026-05-22T15:00:00+00:00] alpha\n"
        "[topics: a, b]\n"
        "\n"
        "body\n"
    )
    secs = parse(raw)
    assert len(secs) == 1
    assert secs[0].topic == "alpha"


def test_parse_drops_pre_first_header_garbage() -> None:
    raw = (
        "leftover line 1\n## [2026-05-22T14:00:00+00:00 – 2026-05-22T15:00:00+00:00] alpha\nbody\n"  # noqa: RUF001
    )
    secs = parse(raw)
    assert len(secs) == 1
    assert "leftover" not in secs[0].body


def test_store_append_then_read(tmp_path: Path) -> None:
    store = STMStore(tmp_path)

    async def go() -> None:
        await store.append_sections([_section(topic="first")])
        await store.append_sections([_section(topic="second")])
        secs = await store.read()
        assert [s.topic for s in secs] == ["first", "second"]

    anyio.run(go)


def test_store_replace_overwrites(tmp_path: Path) -> None:
    store = STMStore(tmp_path)

    async def go() -> None:
        await store.append_sections([_section(topic="a")])
        await store.replace([_section(topic="b")])
        secs = await store.read()
        assert [s.topic for s in secs] == ["b"]

    anyio.run(go)


def test_read_raw_returns_file_text(tmp_path: Path) -> None:
    store = STMStore(tmp_path)

    async def go() -> None:
        await store.append_sections([_section(topic="t")])
        raw = await store.read_raw()
        assert "t" in raw
        assert "## [" in raw

    anyio.run(go)


def test_empty_returns_empty(tmp_path: Path) -> None:
    store = STMStore(tmp_path)

    async def go() -> None:
        assert await store.read() == []
        assert await store.read_raw() == ""

    anyio.run(go)


def test_render_handles_empty_topics_list() -> None:
    sec = STMSection(
        ts_start="2026-05-22T14:00:00+00:00",
        ts_end="2026-05-22T14:30:00+00:00",
        topic="bare",
        topics=[],
        body="hello",
    )
    text = render([sec])
    parsed = parse(text)
    assert len(parsed) == 1 and parsed[0].topics == []
