"""LTM document management (MEMORY_SPEC §2.2).

Format::

    # Long-term memory

    ## user
    - preferred concise responses [src:explicit, ts:2026-04-12]

    ## feedback
    - never mock the database in tests [src:explicit, ts:2026-02-18]

Categories: user, feedback, project, reference, fact, episodic
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import long_term_path
from .storage import atomic_write_text, file_lock

LTMCategory = Literal["user", "feedback", "project", "reference", "fact", "episodic"]
CATEGORIES: tuple[str, ...] = ("user", "feedback", "project", "reference", "fact", "episodic")

# Matches trailing: [src:explicit, ts:2026-04-12]
_TRAILER_RE = re.compile(r"\s*\[src:([^,\]]+),\s*ts:([^\]]+)\]\s*$")


@dataclass(slots=True)
class LTMBullet:
    section: str  # one of CATEGORIES (lowercased)
    content: str  # bullet text without the trailing [src:..., ts:...] trailer
    src: str  # explicit | implicit | user | feedback | project | reference | fact
    ts: str  # YYYY-MM-DD
    raw: str  # original raw line — used as identity key when deleting


class LTMStore:
    """Read/write the long_term.md document (MEMORY_SPEC §2.2)."""

    def __init__(self, memory_dir: Path) -> None:
        self._path = long_term_path(memory_dir)

    def exists(self) -> bool:
        return self._path.exists()

    def read_raw(self) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")

    def read_bullets(self) -> list[LTMBullet]:
        """Parse all bullets from the LTM document."""
        text = self.read_raw()
        return _parse_bullets(text) if text else []

    async def append_bullet(self, section: str, content: str, src: str, ts: str) -> None:
        """Add one bullet to *section*, creating the section header if absent."""
        async with file_lock(self._path):
            text = self.read_raw()
            bullet_line = f"- {content} [src:{src}, ts:{ts}]"
            atomic_write_text(self._path, _insert_bullet(text, section, bullet_line))

    async def rewrite(self, bullets: list[LTMBullet]) -> None:
        """Replace the entire LTM document with *bullets* (canonical order)."""
        async with file_lock(self._path):
            atomic_write_text(self._path, _render_ltm(bullets))

    def find_bullets(self, target: str) -> list[LTMBullet]:
        """Find bullets matching *target*.

        - ``"category:N"`` — zero-based index within that category
          (e.g. ``"fact:0"`` → first fact bullet)
        - anything else — partial case-insensitive content match
        """
        bullets = self.read_bullets()
        if ":" in target:
            cat, _, idx_str = target.partition(":")
            if cat in CATEGORIES and idx_str.isdigit():
                cat_bullets = [b for b in bullets if b.section == cat]
                idx = int(idx_str)
                return [cat_bullets[idx]] if 0 <= idx < len(cat_bullets) else []
        lower = target.lower()
        return [b for b in bullets if lower in b.content.lower()]

    async def delete_bullets(self, to_delete: list[LTMBullet]) -> int:
        """Delete *to_delete* from the document. Returns the count removed."""
        if not to_delete:
            return 0
        raws = {b.raw for b in to_delete}
        async with file_lock(self._path):
            all_bullets = self.read_bullets()
            remaining = [b for b in all_bullets if b.raw not in raws]
            removed = len(all_bullets) - len(remaining)
            if removed:
                atomic_write_text(self._path, _render_ltm(remaining))
        return removed


# ── Format helpers ──────────────────────────────────────────────────────────


def _parse_bullets(text: str) -> list[LTMBullet]:
    bullets: list[LTMBullet] = []
    current_section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            current_section = line[3:].strip().lower()
        elif line.startswith("- ") and current_section:
            raw = line
            m = _TRAILER_RE.search(line)
            if m:
                src = m.group(1).strip()
                ts = m.group(2).strip()
                content = line[: m.start()].lstrip("- ").rstrip()
            else:
                src = "unknown"
                ts = ""
                content = line[2:].strip()
            bullets.append(
                LTMBullet(section=current_section, content=content, src=src, ts=ts, raw=raw)
            )
    return bullets


def _insert_bullet(text: str, section: str, bullet_line: str) -> str:
    """Insert *bullet_line* under *section*, creating the section header if absent."""
    if not text.strip():
        text = "# Long-term memory\n"

    header = f"## {section}"
    lines = text.splitlines(keepends=True)

    for i, line in enumerate(lines):
        if line.rstrip() == header:
            # Walk forward past existing bullets and blank lines to find the
            # point where the next section begins (or EOF).
            j = i + 1
            while j < len(lines) and not lines[j].startswith("## "):
                j += 1
            # Back over trailing blank lines for clean formatting.
            insert_at = j
            while insert_at > i + 1 and lines[insert_at - 1].strip() == "":
                insert_at -= 1
            lines.insert(insert_at, bullet_line + "\n")
            return "".join(lines)

    # Section doesn't exist yet — append at the end.
    if not text.endswith("\n"):
        text += "\n"
    return text + f"\n{header}\n{bullet_line}\n"


def _render_ltm(bullets: list[LTMBullet]) -> str:
    """Render *bullets* back to the canonical LTM markdown document."""
    by_section: dict[str, list[LTMBullet]] = defaultdict(list)
    for b in bullets:
        by_section[b.section].append(b)

    parts: list[str] = ["# Long-term memory\n"]
    for cat in CATEGORIES:
        if cat in by_section:
            parts.append(f"\n## {cat}\n")
            for b in by_section[cat]:
                parts.append(f"- {b.content} [src:{b.src}, ts:{b.ts}]\n")
    return "".join(parts)


__all__ = ["CATEGORIES", "LTMBullet", "LTMCategory", "LTMStore"]
