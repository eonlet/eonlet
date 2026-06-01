"""LTM store CRUD and round-trip (ADR-0005 — episodic-only timeline)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.ltm import CATEGORIES, LTMBullet, LTMStore, _parse_bullets, _render_ltm


def test_categories_is_episodic_only() -> None:
    # ADR-0005: the five semantic categories moved to the knowledge axis.
    assert CATEGORIES == ("episodic",)


def test_empty_store_returns_empty(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    assert store.read_bullets() == []
    assert store.read_raw() == ""
    assert not store.exists()


def test_append_bullet_creates_file(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(
        lambda: store.append_bullet("episodic", "2026-05-23: sky is blue", "implicit", "2026-05-23")
    )
    assert store.exists()
    bullets = store.read_bullets()
    assert len(bullets) == 1
    assert bullets[0].content == "2026-05-23: sky is blue"
    assert bullets[0].section == "episodic"
    assert bullets[0].src == "implicit"
    assert bullets[0].ts == "2026-05-23"


def test_append_to_existing_section(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(
        lambda: store.append_bullet("episodic", "2026-05-23: first", "implicit", "2026-05-23")
    )
    anyio.run(
        lambda: store.append_bullet("episodic", "2026-05-23: second", "implicit", "2026-05-23")
    )
    bullets = store.read_bullets()
    assert len(bullets) == 2
    assert bullets[0].content == "2026-05-23: first"
    assert bullets[1].content == "2026-05-23: second"


def test_rewrite_replaces_document(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("episodic", "2026-05-22: old", "implicit", "2026-05-22"))
    new_bullets = [
        LTMBullet(
            section="episodic",
            content="2026-05-23: new content",
            src="implicit",
            ts="2026-05-23",
            raw="- 2026-05-23: new content [src:implicit, ts:2026-05-23]",
        )
    ]
    anyio.run(lambda: store.rewrite(new_bullets))
    bullets = store.read_bullets()
    assert len(bullets) == 1
    assert bullets[0].section == "episodic"
    assert bullets[0].content == "2026-05-23: new content"


def test_rewrite_empty_list(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(
        lambda: store.append_bullet("episodic", "2026-05-23: something", "implicit", "2026-05-23")
    )
    anyio.run(lambda: store.rewrite([]))
    # File exists but has only the header — no bullets.
    assert store.read_bullets() == []


def test_render_then_parse_round_trips() -> None:
    bullets = [
        LTMBullet("episodic", "2026-05-23: shipped tools", "implicit", "2026-05-23", ""),
        LTMBullet("episodic", "2026-05-22: fixed SSRF", "implicit", "2026-05-22", ""),
    ]
    text = _render_ltm(bullets)
    back = _parse_bullets(text)
    assert len(back) == 2
    assert all(b.section == "episodic" for b in back)
    assert back[0].content == "2026-05-23: shipped tools"
    assert back[0].src == "implicit"
    assert back[0].ts == "2026-05-23"


def test_parse_bullets_without_trailer() -> None:
    text = "# Long-term memory\n\n## episodic\n- no trailer here\n"
    bullets = _parse_bullets(text)
    assert len(bullets) == 1
    assert bullets[0].content == "no trailer here"
    assert bullets[0].src == "unknown"
    assert bullets[0].ts == ""


def test_render_drops_non_episodic_sections() -> None:
    # Defensive: a stray legacy section is not rendered (single-population invariant).
    bullets = [
        LTMBullet("episodic", "2026-05-23: a day", "implicit", "2026-05-23", ""),
        LTMBullet("user", "pref", "explicit", "2026-05-23", ""),
    ]
    text = _render_ltm(bullets)
    assert "## episodic" in text
    assert "## user" not in text
    assert "pref" not in text
