"""Notes store — marker-delimited sections in ``memory/notes.md``.

Per MEMORY_SPEC §2.3 / §5.3. Notes added via the ``note`` tool are bracketed
by an HTML-comment marker carrying ``id`` / ``title`` / ``tags`` so the
store can list/get/update/delete by id. Free-form prose between markers (or
before the first marker, or after the last) is treated as unmanaged content
and preserved verbatim across writes.

The store is pure file I/O. Event emission (``mem_note_added`` and friends)
is the caller's responsibility — the tool layer wires it through
``ToolContext.record_event``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .paths import notes_path
from .storage import atomic_write_text, file_lock

# A note begins with an HTML comment line:
#   <!-- note id=note-2026-05-22-a1b2 title="..." tags=t1,t2 ts=2026-05-22T14:30 -->
# and runs until the next such marker or EOF. The blank lines around the
# marker are preserved on read and re-emitted on write.

_MARKER_RE = re.compile(
    r"^<!--\s*note\s+"
    r"id=(?P<id>[A-Za-z0-9_-]+)"
    r"(?:\s+title=\"(?P<title>[^\"]*)\")?"
    r"(?:\s+tags=(?P<tags>[A-Za-z0-9_,\-]*))?"
    r"(?:\s+ts=(?P<ts>[A-Za-z0-9:+\-]+))?"
    r"\s*-->\s*$"
)


@dataclass(slots=True)
class Note:
    id: str
    title: str | None
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    body: str = ""


# ── Parsing ─────────────────────────────────────────────────────────────────


def _parse(text: str) -> tuple[list[Note], str]:
    """Split a notes document into managed notes and a leading "preamble".

    Returns ``(notes, preamble)``. The preamble is any text that appears
    before the first marker — typically user free-form prose. It's preserved
    verbatim when the store rewrites the file.
    """
    lines = text.splitlines(keepends=True)
    notes: list[Note] = []
    preamble: list[str] = []
    current: Note | None = None
    current_body: list[str] = []

    def _flush() -> None:
        nonlocal current, current_body
        if current is not None:
            current.body = "".join(current_body).rstrip("\n")
            notes.append(current)
        current = None
        current_body = []

    for line in lines:
        m = _MARKER_RE.match(line.rstrip("\n"))
        if m:
            _flush()
            tags_str = m.group("tags") or ""
            tags = [t for t in tags_str.split(",") if t]
            current = Note(
                id=m.group("id"),
                title=m.group("title"),
                tags=tags,
                created_at=m.group("ts") or "",
            )
            current_body = []
        elif current is None:
            preamble.append(line)
        else:
            current_body.append(line)

    _flush()
    return notes, "".join(preamble)


def _render_marker(n: Note) -> str:
    parts = [f"<!-- note id={n.id}"]
    if n.title:
        # Escape any embedded quotes defensively — the marker title is shown
        # to the LLM verbatim so we accept anything except ``"`` raw.
        safe_title = n.title.replace('"', "'")
        parts.append(f'title="{safe_title}"')
    if n.tags:
        parts.append("tags=" + ",".join(n.tags))
    if n.created_at:
        parts.append(f"ts={n.created_at}")
    parts.append("-->")
    return " ".join(parts) + "\n"


def _render(notes: list[Note], preamble: str) -> str:
    out: list[str] = []
    if preamble:
        out.append(preamble)
        if not preamble.endswith("\n"):
            out.append("\n")
    for n in notes:
        out.append(_render_marker(n))
        if n.body:
            out.append(n.body.rstrip("\n") + "\n")
        out.append("\n")
    return "".join(out).rstrip("\n") + "\n" if (notes or preamble) else ""


# ── Public store API ────────────────────────────────────────────────────────


class NotesStore:
    """File-backed notes store rooted at one eonlet's ``memory/`` directory."""

    def __init__(self, memory_dir: Path) -> None:
        self._path = notes_path(memory_dir)

    def _read_all(self) -> tuple[list[Note], str]:
        if not self._path.exists():
            return [], ""
        return _parse(self._path.read_text(encoding="utf-8"))

    def _write_all(self, notes: list[Note], preamble: str) -> None:
        atomic_write_text(self._path, _render(notes, preamble))

    async def add(
        self, *, id: str, content: str, title: str | None = None, tags: list[str] | None = None
    ) -> Note:
        async with file_lock(self._path):
            notes, preamble = self._read_all()
            if any(n.id == id for n in notes):
                raise ValueError(f"note id already exists: {id}")
            note = Note(
                id=id,
                title=title,
                tags=list(tags or []),
                created_at=datetime.now().strftime("%Y-%m-%dT%H:%M"),
                body=content.rstrip("\n"),
            )
            notes.append(note)
            self._write_all(notes, preamble)
            return note

    async def list_notes(self, *, tags: list[str] | None = None) -> list[Note]:
        async with file_lock(self._path):
            notes, _ = self._read_all()
            if not tags:
                return notes
            filt = set(tags)
            return [n for n in notes if filt.intersection(n.tags)]

    async def get(self, *, id: str) -> Note | None:
        async with file_lock(self._path):
            notes, _ = self._read_all()
            for n in notes:
                if n.id == id:
                    return n
            return None

    async def update(self, *, id: str, content: str) -> Note:
        async with file_lock(self._path):
            notes, preamble = self._read_all()
            for n in notes:
                if n.id == id:
                    n.body = content.rstrip("\n")
                    self._write_all(notes, preamble)
                    return n
            raise KeyError(f"no such note: {id}")

    async def delete(self, *, id: str) -> bool:
        async with file_lock(self._path):
            notes, preamble = self._read_all()
            new = [n for n in notes if n.id != id]
            if len(new) == len(notes):
                return False
            self._write_all(new, preamble)
            return True
