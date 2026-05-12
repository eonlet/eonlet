"""TODOs store — line-delimited JSON in ``memory/todos.jsonl``.

Per MEMORY_SPEC §2.4 / §5.4. Each TODO is one JSON object per line. Writes
rewrite the whole file atomically — the file is small (typically a few
dozen items) and rewriting keeps the data-model simple.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .paths import todos_path
from .storage import atomic_write_text, file_lock

TodoStatus = Literal["pending", "done", "cancelled"]


@dataclass(slots=True)
class Todo:
    id: str
    content: str
    status: TodoStatus = "pending"
    created_at: str = ""
    due: str | None = None
    done_at: str | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Todo:
        # Defensive about unknown fields — accept any future schema additions
        # without crashing existing records.
        status = str(raw.get("status", "pending"))
        if status not in ("pending", "done", "cancelled"):
            status = "pending"
        raw_tags = raw.get("tags") or []
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []
        return cls(
            id=str(raw["id"]),
            content=str(raw.get("content", "")),
            status=status,  # type: ignore[arg-type]
            created_at=str(raw.get("created_at", "")),
            due=_opt_str(raw.get("due")),
            done_at=_opt_str(raw.get("done_at")),
            tags=tags,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status,
            "created_at": self.created_at,
            "due": self.due,
            "done_at": self.done_at,
            "tags": self.tags,
        }


def _opt_str(v: object) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if s else None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── Store ──────────────────────────────────────────────────────────────────


class TodosStore:
    """File-backed todo store rooted at one eonlet's ``memory/`` directory."""

    def __init__(self, memory_dir: Path) -> None:
        self._path = todos_path(memory_dir)

    def _read_all(self) -> list[Todo]:
        if not self._path.exists():
            return []
        out: list[Todo] = []
        for raw_line in self._path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                # Corrupt line — skip rather than crash. The original line is
                # still on disk (this code path only reads); a write will drop
                # it. Logging here is out of scope for the storage layer.
                continue
            if not isinstance(obj, dict):
                continue
            try:
                out.append(Todo.from_json(obj))
            except (KeyError, ValueError):
                continue
        return out

    def _write_all(self, todos: list[Todo]) -> None:
        if not todos:
            atomic_write_text(self._path, "")
            return
        text = "\n".join(json.dumps(t.to_json(), ensure_ascii=False) for t in todos) + "\n"
        atomic_write_text(self._path, text)

    async def add(
        self,
        *,
        id: str,
        content: str,
        due: str | None = None,
        tags: list[str] | None = None,
    ) -> Todo:
        async with file_lock(self._path):
            todos = self._read_all()
            if any(t.id == id for t in todos):
                raise ValueError(f"todo id already exists: {id}")
            todo = Todo(
                id=id,
                content=content,
                status="pending",
                created_at=_now_iso(),
                due=due,
                tags=list(tags or []),
            )
            todos.append(todo)
            self._write_all(todos)
            return todo

    async def list_todos(
        self, *, status: Literal["pending", "done", "cancelled", "all"] = "pending"
    ) -> list[Todo]:
        async with file_lock(self._path):
            todos = self._read_all()
            if status == "all":
                return todos
            return [t for t in todos if t.status == status]

    async def get(self, *, id: str) -> Todo | None:
        async with file_lock(self._path):
            for t in self._read_all():
                if t.id == id:
                    return t
            return None

    async def mark_done(self, *, id: str) -> Todo:
        async with file_lock(self._path):
            todos = self._read_all()
            for t in todos:
                if t.id == id:
                    t.status = "done"
                    t.done_at = _now_iso()
                    self._write_all(todos)
                    return t
            raise KeyError(f"no such todo: {id}")

    async def update(
        self,
        *,
        id: str,
        content: str | None = None,
        due: str | None = None,
        tags: list[str] | None = None,
    ) -> Todo:
        async with file_lock(self._path):
            todos = self._read_all()
            for t in todos:
                if t.id == id:
                    if content is not None:
                        t.content = content
                    if due is not None:
                        t.due = due or None
                    if tags is not None:
                        t.tags = list(tags)
                    self._write_all(todos)
                    return t
            raise KeyError(f"no such todo: {id}")

    async def delete(self, *, id: str) -> bool:
        async with file_lock(self._path):
            todos = self._read_all()
            new = [t for t in todos if t.id != id]
            if len(new) == len(todos):
                return False
            self._write_all(new)
            return True
