"""Migration from legacy Claude Code auto-memory (MEMORY_SPEC §11).

Reads the MEMORY.md index + per-fact .md files written by Claude Code's
built-in memory system, maps each fact's ``metadata.type`` to an LTM
category, and writes bullets to ``long_term.md`` with ``src:explicit``.

Type mapping
------------
user      → user
feedback  → feedback
project   → project
reference → reference
(anything else) → fact
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio

from .ltm import CATEGORIES, LTMStore

# ── public dataclasses ────────────────────────────────────────────────────────


@dataclass(slots=True)
class MigrationBullet:
    category: str
    content: str
    ts: str


@dataclass
class MigrationResult:
    bullets: list[MigrationBullet] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ── YAML frontmatter helpers ──────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Only the "name: value" style used by Claude Code memory files
_KV_RE = re.compile(r"^(\w+):\s*(.+)$", re.MULTILINE)


_NESTED_KV_RE = re.compile(r"^\s+(\w+):\s*(.+)$", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter_dict, body_text)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw = m.group(1)
    body = text[m.end() :]
    kv: dict[str, Any] = {}
    for key, val in _KV_RE.findall(raw):
        kv[key] = val.strip()
    # Handle one level of nesting for "metadata:\n  type: value"
    metadata_re = re.compile(r"^metadata:\s*\n((?:[ \t]+.+\n?)+)", re.MULTILINE)
    meta_m = metadata_re.search(raw)
    if meta_m:
        nested: dict[str, Any] = {}
        for nk, nv in _NESTED_KV_RE.findall(meta_m.group(1)):
            nested[nk.strip()] = nv.strip()
        kv["metadata"] = nested
    return kv, body


# ── MEMORY.md index parser ────────────────────────────────────────────────────

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _parse_memory_index(text: str) -> list[tuple[str, str]]:
    """Return list of (title, relative_path) from MEMORY.md."""
    return [(title, path) for title, path in _LINK_RE.findall(text)]


# ── type → category mapping ───────────────────────────────────────────────────

_TYPE_MAP: dict[str, str] = {
    "user": "user",
    "feedback": "feedback",
    "project": "project",
    "reference": "reference",
}


def _map_type(type_str: str | None) -> str:
    if type_str is None:
        return "fact"
    return _TYPE_MAP.get(type_str.lower(), "fact")


# ── mtime helper ─────────────────────────────────────────────────────────────


def _mtime_ts(path: Path) -> str:
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%d")
    except OSError:
        return datetime.now(tz=UTC).strftime("%Y-%m-%d")


# ── main migration logic ──────────────────────────────────────────────────────


def migrate_legacy_memory(legacy_dir: Path) -> MigrationResult:
    """Read legacy MEMORY.md + per-fact files; return a MigrationResult.

    Does NOT write anything — call ``apply_migration`` separately.
    """
    result = MigrationResult()
    memory_index = legacy_dir / "MEMORY.md"
    if not memory_index.exists():
        result.errors.append(f"MEMORY.md not found in {legacy_dir}")
        return result

    try:
        index_text = memory_index.read_text(encoding="utf-8")
    except OSError as e:
        result.errors.append(f"could not read MEMORY.md: {e}")
        return result

    links = _parse_memory_index(index_text)
    if not links:
        result.skipped.append("MEMORY.md has no file links")
        return result

    for title, rel_path in links:
        fact_path = legacy_dir / rel_path
        if not fact_path.exists():
            result.skipped.append(f"{rel_path}: file not found")
            continue
        try:
            text = fact_path.read_text(encoding="utf-8")
        except OSError as e:
            result.errors.append(f"{rel_path}: read error: {e}")
            continue

        fm, body = _parse_frontmatter(text)
        metadata = fm.get("metadata", {})
        type_str = metadata.get("type") if isinstance(metadata, dict) else None
        category = _map_type(type_str)

        content = body.strip()
        if not content:
            # Fall back to the link title as the bullet content.
            content = title.strip()
        if not content:
            result.skipped.append(f"{rel_path}: empty content")
            continue

        ts = _mtime_ts(fact_path)
        result.bullets.append(MigrationBullet(category=category, content=content, ts=ts))

    return result


async def apply_migration(result: MigrationResult, store: LTMStore) -> int:
    """Write MigrationResult bullets to the given LTMStore. Returns bullets written."""
    written = 0
    for bullet in result.bullets:
        cat = bullet.category if bullet.category in CATEGORIES else "fact"
        await store.append_bullet(cat, bullet.content, "explicit", bullet.ts)
        written += 1
    return written


def apply_migration_sync(result: MigrationResult, store: LTMStore) -> int:
    """Synchronous wrapper around ``apply_migration`` (for CLI entry points)."""
    return anyio.run(lambda: apply_migration(result, store))
