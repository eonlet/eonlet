"""LTM store CRUD and round-trip (MEMORY_SPEC §2.2)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.ltm import LTMBullet, LTMStore, _parse_bullets, _render_ltm


def test_empty_store_returns_empty(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    assert store.read_bullets() == []
    assert store.read_raw() == ""
    assert not store.exists()


def test_append_bullet_creates_file(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "sky is blue", "explicit", "2026-05-23"))
    assert store.exists()
    bullets = store.read_bullets()
    assert len(bullets) == 1
    assert bullets[0].content == "sky is blue"
    assert bullets[0].section == "fact"
    assert bullets[0].src == "explicit"
    assert bullets[0].ts == "2026-05-23"


def test_append_multiple_categories(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("user", "prefers concise", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("feedback", "no db mocks", "explicit", "2026-05-22"))
    anyio.run(lambda: store.append_bullet("fact", "sky is blue", "implicit", "2026-05-21"))
    bullets = store.read_bullets()
    assert len(bullets) == 3
    sections = {b.section for b in bullets}
    assert sections == {"user", "feedback", "fact"}


def test_append_to_existing_section(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "first", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "second", "explicit", "2026-05-23"))
    bullets = store.read_bullets()
    assert len(bullets) == 2
    assert bullets[0].content == "first"
    assert bullets[1].content == "second"


def test_rewrite_replaces_document(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "old", "explicit", "2026-05-22"))
    new_bullets = [
        LTMBullet(
            section="user",
            content="new content",
            src="explicit",
            ts="2026-05-23",
            raw="- new content [src:explicit, ts:2026-05-23]",
        )
    ]
    anyio.run(lambda: store.rewrite(new_bullets))
    bullets = store.read_bullets()
    assert len(bullets) == 1
    assert bullets[0].section == "user"
    assert bullets[0].content == "new content"


def test_rewrite_empty_list(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "something", "explicit", "2026-05-23"))
    anyio.run(lambda: store.rewrite([]))
    # File exists but has only the header — no bullets.
    assert store.read_bullets() == []


def test_find_by_content_match(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "sky is blue", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "grass is green", "explicit", "2026-05-23"))
    matches = store.find_bullets("sky")
    assert len(matches) == 1
    assert matches[0].content == "sky is blue"


def test_find_by_content_case_insensitive(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("user", "Prefers concise", "explicit", "2026-05-23"))
    assert len(store.find_bullets("prefers")) == 1
    assert len(store.find_bullets("PREFERS")) == 1


def test_find_by_index(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "first fact", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "second fact", "explicit", "2026-05-23"))
    matches = store.find_bullets("fact:1")
    assert len(matches) == 1
    assert matches[0].content == "second fact"


def test_find_by_index_out_of_range(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "only one", "explicit", "2026-05-23"))
    assert store.find_bullets("fact:5") == []


def test_find_no_match(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "something", "explicit", "2026-05-23"))
    assert store.find_bullets("zzz-not-here") == []


def test_delete_bullets(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "to delete", "explicit", "2026-05-23"))
    anyio.run(lambda: store.append_bullet("fact", "to keep", "explicit", "2026-05-23"))
    matches = store.find_bullets("to delete")
    count = anyio.run(lambda: store.delete_bullets(matches))
    assert count == 1
    remaining = store.read_bullets()
    assert len(remaining) == 1
    assert remaining[0].content == "to keep"


def test_delete_empty_list_is_noop(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "stays", "explicit", "2026-05-23"))
    count = anyio.run(lambda: store.delete_bullets([]))
    assert count == 0
    assert len(store.read_bullets()) == 1


def test_render_then_parse_round_trips() -> None:
    bullets = [
        LTMBullet("user", "prefers concise", "explicit", "2026-05-23", ""),
        LTMBullet("feedback", "no db mocks", "explicit", "2026-05-22", ""),
        LTMBullet("fact", "earth is round", "implicit", "2026-05-21", ""),
    ]
    text = _render_ltm(bullets)
    back = _parse_bullets(text)
    assert len(back) == 3
    sections = [b.section for b in back]
    assert sections == ["user", "feedback", "fact"]
    assert back[0].content == "prefers concise"
    assert back[0].src == "explicit"
    assert back[0].ts == "2026-05-23"


def test_parse_bullets_without_trailer() -> None:
    text = "# Long-term memory\n\n## fact\n- no trailer here\n"
    bullets = _parse_bullets(text)
    assert len(bullets) == 1
    assert bullets[0].content == "no trailer here"
    assert bullets[0].src == "unknown"
    assert bullets[0].ts == ""


def test_category_order_preserved_in_render() -> None:
    """Bullets are rendered in canonical CATEGORIES order regardless of input order."""
    bullets = [
        LTMBullet("episodic", "2026-05-23: a day", "implicit", "2026-05-23", ""),
        LTMBullet("user", "pref", "explicit", "2026-05-23", ""),
    ]
    text = _render_ltm(bullets)
    assert text.index("## user") < text.index("## episodic")
