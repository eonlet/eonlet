"""Migration from legacy Claude Code auto-memory (MEMORY_SPEC §11)."""

from __future__ import annotations

from pathlib import Path

import anyio

from eonlet.memory.ltm import LTMStore
from eonlet.memory.migrate import (
    MigrationResult,
    apply_migration,
    migrate_legacy_memory,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _write_fact(d: Path, filename: str, type_: str, content: str) -> Path:
    p = d / filename
    p.write_text(
        f"---\nname: {filename}\nmetadata:\n  type: {type_}\n---\n\n{content}\n",
        encoding="utf-8",
    )
    return p


def _write_memory_index(d: Path, links: list[tuple[str, str]]) -> None:
    lines = ["# Memory\n"]
    for title, path in links:
        lines.append(f"- [{title}]({path})")
    (d / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── parse tests ────────────────────────────────────────────────────────────────


def test_migrate_reads_facts(tmp_path: Path) -> None:
    _write_fact(tmp_path, "fact1.md", "user", "prefers dark mode")
    _write_fact(tmp_path, "fact2.md", "feedback", "avoid mocking the db")
    _write_memory_index(tmp_path, [("pref", "fact1.md"), ("no mock", "fact2.md")])

    result = migrate_legacy_memory(tmp_path)
    assert not result.errors
    assert len(result.bullets) == 2
    assert result.bullets[0].category == "user"
    assert "dark mode" in result.bullets[0].content
    assert result.bullets[1].category == "feedback"


def test_migrate_maps_unknown_type_to_fact(tmp_path: Path) -> None:
    _write_fact(tmp_path, "x.md", "custom_type", "some content")
    _write_memory_index(tmp_path, [("x", "x.md")])
    result = migrate_legacy_memory(tmp_path)
    assert result.bullets[0].category == "fact"


def test_migrate_maps_project_and_reference(tmp_path: Path) -> None:
    _write_fact(tmp_path, "p.md", "project", "deadline is Friday")
    _write_fact(tmp_path, "r.md", "reference", "see linear board INGEST")
    _write_memory_index(tmp_path, [("p", "p.md"), ("r", "r.md")])
    result = migrate_legacy_memory(tmp_path)
    cats = {b.category for b in result.bullets}
    assert cats == {"project", "reference"}


def test_migrate_missing_memory_index(tmp_path: Path) -> None:
    result = migrate_legacy_memory(tmp_path)
    assert result.errors
    assert "MEMORY.md not found" in result.errors[0]


def test_migrate_empty_index_no_links(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("# Memory\n(empty)\n", encoding="utf-8")
    result = migrate_legacy_memory(tmp_path)
    assert not result.bullets
    assert result.skipped


def test_migrate_skips_missing_file(tmp_path: Path) -> None:
    _write_memory_index(tmp_path, [("ghost", "ghost.md")])
    result = migrate_legacy_memory(tmp_path)
    assert not result.bullets
    assert any("ghost.md" in s for s in result.skipped)


def test_migrate_fallback_content_from_title(tmp_path: Path) -> None:
    p = tmp_path / "empty.md"
    p.write_text("---\nmetadata:\n  type: fact\n---\n\n", encoding="utf-8")
    _write_memory_index(tmp_path, [("The Title", "empty.md")])
    result = migrate_legacy_memory(tmp_path)
    assert result.bullets[0].content == "The Title"


def test_migrate_ts_from_mtime(tmp_path: Path) -> None:
    _write_fact(tmp_path, "f.md", "fact", "content")
    _write_memory_index(tmp_path, [("f", "f.md")])
    result = migrate_legacy_memory(tmp_path)
    # ts must be a YYYY-MM-DD string
    assert len(result.bullets[0].ts) == 10
    assert result.bullets[0].ts[4] == "-"


# ── apply tests ───────────────────────────────────────────────────────────────


def test_apply_migration_writes_bullets(tmp_path: Path) -> None:
    result = MigrationResult()
    result.bullets = [
        # Use the dataclass directly
        __import__("eonlet.memory.migrate", fromlist=["MigrationBullet"]).MigrationBullet(
            category="fact", content="sky is blue", ts="2026-05-01"
        ),
        __import__("eonlet.memory.migrate", fromlist=["MigrationBullet"]).MigrationBullet(
            category="user", content="prefers dark mode", ts="2026-05-01"
        ),
    ]
    store = LTMStore(tmp_path)
    written = anyio.run(lambda: apply_migration(result, store))
    assert written == 2
    bullets = store.read_bullets()
    assert len(bullets) == 2
    contents = {b.content for b in bullets}
    assert "sky is blue" in contents
    assert "prefers dark mode" in contents
    for b in bullets:
        assert b.src == "explicit"


def test_apply_migration_empty_result(tmp_path: Path) -> None:
    result = MigrationResult()
    store = LTMStore(tmp_path)
    written = anyio.run(lambda: apply_migration(result, store))
    assert written == 0


def test_full_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "legacy"
    src.mkdir()
    _write_fact(src, "a.md", "user", "user prefers verbose logs")
    _write_fact(src, "b.md", "feedback", "do not use bare except")
    _write_memory_index(src, [("a", "a.md"), ("b", "b.md")])

    result = migrate_legacy_memory(src)
    assert len(result.bullets) == 2

    ltm_dir = tmp_path / "memory"
    ltm_dir.mkdir()
    store = LTMStore(ltm_dir)
    written = anyio.run(lambda: apply_migration(result, store))
    assert written == 2

    bullets = store.read_bullets()
    secs = {b.section for b in bullets}
    assert "user" in secs
    assert "feedback" in secs
    for b in bullets:
        assert b.src == "explicit"
