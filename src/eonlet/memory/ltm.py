"""LTM document management (ADR-0005, episodic axis).

As of ADR-0005 the long-term document holds a **single population**: dated
episodic summaries produced by tier-2 compaction. The five semantic
categories (user/feedback/project/reference/fact) moved to the curated
knowledge axis (``memory/knowledge/``); durable facts are now written there
deliberately via the ``knowledge`` tool rather than auto-promoted into LTM.

Format::

    # Long-term memory

    ## episodic
    - 2026-05-22: shipped the web-tools upgrade [src:implicit, ts:2026-05-22]
    - 2026-05-24: debugged the SSRF guard with the user [src:implicit, ts:2026-05-24]

Because the document is now one uniform population, tier-3 forgetting applies
a single recency/salience policy to every bullet — there is no ``src:explicit``
"never drop" exemption (that was the special-casing ADR-0005 removed).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .paths import long_term_path
from .storage import atomic_write_text, file_lock

LTMCategory = Literal["episodic"]
CATEGORIES: tuple[str, ...] = ("episodic",)

# Matches trailing: [src:implicit, ts:2026-05-22]
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
