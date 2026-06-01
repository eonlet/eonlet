"""Knowledge store — the curated, hierarchical knowledge axis (ADR-0005).

Axis 2 of the dual-axis memory model: a directory tree of markdown files the
agent edits deliberately. Unlike episodic memory it is **never** auto-deleted
by a budget; the agent adds, edits, moves, and removes files on purpose.

The retrieval backbone is ``knowledge/index.md`` — an agent-curated map, one
line per file (``- [Title](path) — hook``). The runtime injects the map whole
into every LLM call (it is small) and the agent opens a file's body on demand
by path, exactly like ``load_skill``. Bodies are never injected.

This module is pure file I/O. Event emission (``kb_written`` and friends) is
the caller's responsibility — the tool layer wires it through
``ToolContext.record_event``, mirroring the other memory stores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import structlog

from ..errors import KnowledgeError, KnowledgePathError
from .paths import knowledge_index_path, knowledge_root
from .storage import atomic_write_text, file_lock
from .tokens import estimate

log = structlog.get_logger(__name__)

# A managed map line:  - [Title](relative/path.md) — one-line hook
# The hook (after an em-dash or hyphen) is optional. Both em-dash and hyphen
# are accepted as the separator so hand-edited maps still parse.
_INDEX_LINE_RE = re.compile(
    r"^\s*-\s*\[(?P<title>[^\]]*)\]\((?P<path>[^)]+)\)"
    r"(?:\s*[—-]\s*(?P<hook>.*?))?\s*$"
)

_INDEX_HEADER = "# Knowledge Index"
_RESERVED = "index.md"


@dataclass(slots=True)
class IndexEntry:
    path: str  # relative posix path under knowledge/, e.g. "rules/testing.md"
    title: str
    hook: str = ""

    def render(self) -> str:
        line = f"- [{self.title}]({self.path})"
        if self.hook:
            line += f" — {self.hook}"
        return line


# ── Path safety ──────────────────────────────────────────────────────────────


def _normalize_rel(path: str) -> str:
    """Confine ``path`` to a relative posix path inside the knowledge tree.

    Rejects absolute paths and any ``..`` traversal. Returns the cleaned
    ``a/b/c`` form. Does not touch the filesystem.
    """
    if not path or not path.strip():
        raise KnowledgePathError(path, "empty path")
    p = PurePosixPath(path.strip())
    if p.is_absolute():
        raise KnowledgePathError(path, "must be relative to knowledge/")
    parts: list[str] = []
    for part in p.parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise KnowledgePathError(path, "'..' traversal is not allowed")
        parts.append(part)
    if not parts:
        raise KnowledgePathError(path, "empty path")
    return "/".join(parts)


def _derive_title(rel: str) -> str:
    """A readable default title from a path stem (``rules/testing.md`` → 'Testing')."""
    stem = PurePosixPath(rel).stem
    return stem.replace("-", " ").replace("_", " ").strip().title() or stem


# ── Store ────────────────────────────────────────────────────────────────────


class KnowledgeStore:
    """File-backed knowledge tree rooted at one eonlet's ``memory/knowledge/``."""

    def __init__(self, memory_dir: Path, *, warn_file_tokens: int = 4_000) -> None:
        self._root = knowledge_root(memory_dir)
        self._index_path = knowledge_index_path(memory_dir)
        self._warn_file_tokens = warn_file_tokens

    # ── path resolution ──────────────────────────────────────────────────
    def _abs(self, path: str, *, require_md: bool, allow_reserved: bool) -> tuple[str, Path]:
        rel = _normalize_rel(path)
        if require_md and not rel.endswith(".md"):
            raise KnowledgePathError(path, "knowledge files must end with '.md'")
        if not allow_reserved and rel == _RESERVED:
            raise KnowledgePathError(
                path, "index.md is managed by the store; write knowledge files"
            )
        return rel, self._root / rel

    # ── index (map) parsing / rendering ──────────────────────────────────
    def _read_index(self) -> list[IndexEntry]:
        if not self._index_path.exists():
            return []
        entries: list[IndexEntry] = []
        for line in self._index_path.read_text(encoding="utf-8").splitlines():
            m = _INDEX_LINE_RE.match(line)
            if not m:
                continue
            entries.append(
                IndexEntry(
                    path=m.group("path").strip(),
                    title=(m.group("title") or "").strip(),
                    hook=(m.group("hook") or "").strip(),
                )
            )
        return entries

    def _write_index(self, entries: list[IndexEntry]) -> None:
        body = "\n".join(e.render() for e in entries)
        text = f"{_INDEX_HEADER}\n\n{body}\n" if body else f"{_INDEX_HEADER}\n"
        atomic_write_text(self._index_path, text)

    def _upsert_index(
        self, entries: list[IndexEntry], *, rel: str, hook: str | None
    ) -> list[IndexEntry]:
        for e in entries:
            if e.path == rel:
                if hook is not None:
                    e.hook = hook
                return entries
        entries.append(IndexEntry(path=rel, title=_derive_title(rel), hook=(hook or "")))
        return entries

    # ── read API ─────────────────────────────────────────────────────────
    async def open(self, path: str) -> str | None:
        """Return a knowledge file's body, or ``None`` if it does not exist."""
        _rel, abs_path = self._abs(path, require_md=True, allow_reserved=True)
        async with file_lock(abs_path):
            if not abs_path.exists():
                return None
            return abs_path.read_text(encoding="utf-8")

    async def list_entries(self) -> list[IndexEntry]:
        """Return the curated map. Falls back to a tree walk if no index exists."""
        async with file_lock(self._index_path):
            entries = self._read_index()
        if entries:
            return entries
        return self._regenerate_from_tree()

    def _regenerate_from_tree(self) -> list[IndexEntry]:
        if not self._root.exists():
            return []
        out: list[IndexEntry] = []
        for p in sorted(self._root.rglob("*.md")):
            rel = p.relative_to(self._root).as_posix()
            if rel == _RESERVED:
                continue
            out.append(IndexEntry(path=rel, title=_derive_title(rel)))
        return out

    def index_text(self) -> str:
        """The raw ``index.md`` text for injection, or a tree-walk fallback."""
        if self._index_path.exists():
            text = self._index_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        entries = self._regenerate_from_tree()
        if not entries:
            return ""
        return f"{_INDEX_HEADER}\n\n" + "\n".join(e.render() for e in entries)

    # ── write API ────────────────────────────────────────────────────────
    async def write(self, *, path: str, content: str, index_line: str | None = None) -> str:
        """Create or replace a knowledge file's full body and sync the map.

        ``index_line`` is the one-line relevance hook for the map. Returns the
        normalized relative path.
        """
        rel, abs_path = self._abs(path, require_md=True, allow_reserved=False)
        async with file_lock(self._index_path):
            atomic_write_text(abs_path, content.rstrip("\n") + "\n")
            entries = self._upsert_index(self._read_index(), rel=rel, hook=index_line)
            self._write_index(entries)
        tokens = estimate(content)
        if tokens > self._warn_file_tokens:
            log.warning(
                "knowledge file exceeds warn_file_tokens",
                path=rel,
                tokens=tokens,
                limit=self._warn_file_tokens,
            )
        return rel

    async def edit(self, *, path: str, old_string: str, new_string: str) -> str:
        """String-replace inside an existing knowledge file (``files.py`` semantics)."""
        rel, abs_path = self._abs(path, require_md=True, allow_reserved=False)
        async with file_lock(self._index_path):
            if not abs_path.exists():
                raise KnowledgeError(f"no such knowledge file: {rel}")
            body = abs_path.read_text(encoding="utf-8")
            count = body.count(old_string)
            if count == 0:
                raise KnowledgeError(f"old_string not found in {rel}")
            if count > 1:
                raise KnowledgeError(
                    f"old_string is not unique in {rel} ({count} matches); add context"
                )
            atomic_write_text(abs_path, body.replace(old_string, new_string, 1))
        return rel

    async def delete(self, *, path: str) -> bool:
        """Remove a knowledge file and its map entry. Returns whether it existed."""
        rel, abs_path = self._abs(path, require_md=True, allow_reserved=False)
        async with file_lock(self._index_path):
            existed = abs_path.exists()
            if existed:
                abs_path.unlink()
            entries = [e for e in self._read_index() if e.path != rel]
            self._write_index(entries)
        return existed

    async def move(self, *, src: str, dst: str, index_line: str | None = None) -> tuple[str, str]:
        """Move a knowledge file, carrying its map entry. Returns ``(src, dst)`` rels."""
        src_rel, src_abs = self._abs(src, require_md=True, allow_reserved=False)
        dst_rel, dst_abs = self._abs(dst, require_md=True, allow_reserved=False)
        async with file_lock(self._index_path):
            if not src_abs.exists():
                raise KnowledgeError(f"no such knowledge file: {src_rel}")
            if dst_abs.exists():
                raise KnowledgeError(f"destination already exists: {dst_rel}")
            content = src_abs.read_text(encoding="utf-8")
            atomic_write_text(dst_abs, content)
            src_abs.unlink()
            # Carry the old entry's title/hook onto the new path.
            old = next((e for e in self._read_index() if e.path == src_rel), None)
            entries = [e for e in self._read_index() if e.path != src_rel]
            hook = index_line if index_line is not None else (old.hook if old else "")
            title = old.title if old else _derive_title(dst_rel)
            entries.append(IndexEntry(path=dst_rel, title=title, hook=hook))
            self._write_index(entries)
        return src_rel, dst_rel
