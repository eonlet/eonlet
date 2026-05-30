"""Task store — line-delimited JSON in ``tasks/todos.jsonl`` (ADR-0005).

Moved verbatim from the old ``memory/todos.py``: the storage format and state
machine (pending/done/cancelled, due, tags) are unchanged — only the home
directory (``tasks/`` instead of ``memory/``) and the type names (Task/
TaskStore) changed. Each task is one JSON object per line; writes rewrite the
whole file atomically (the file is small).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..memory.storage import atomic_write_text, file_lock

TaskStatus = Literal["pending", "done", "cancelled"]


def todos_path(tasks_dir: Path) -> Path:
    return tasks_dir / "todos.jsonl"


def todos_archive_path(tasks_dir: Path) -> Path:
    return tasks_dir / "todos.archive.jsonl"


@dataclass(slots=True)
class Task:
    id: str
    content: str
    status: TaskStatus = "pending"
    created_at: str = ""
    due: str | None = None
    done_at: str | None = None
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Task:
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


class TaskStore:
    """File-backed task store rooted at one eonlet's ``tasks/`` directory."""

    def __init__(self, tasks_dir: Path) -> None:
        self._path = todos_path(tasks_dir)

    def _read_all(self) -> list[Task]:
        if not self._path.exists():
            return []
        out: list[Task] = []
        for raw_line in self._path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj: Any = json.loads(line)
            except json.JSONDecodeError:
                # Corrupt line — skip rather than crash. A later write drops it.
                continue
            if not isinstance(obj, dict):
                continue
            try:
                out.append(Task.from_json(obj))
            except (KeyError, ValueError):
                continue
        return out

    def _write_all(self, tasks: list[Task]) -> None:
        if not tasks:
            atomic_write_text(self._path, "")
            return
        text = "\n".join(json.dumps(t.to_json(), ensure_ascii=False) for t in tasks) + "\n"
        atomic_write_text(self._path, text)

    async def add(
        self,
        *,
        id: str,
        content: str,
        due: str | None = None,
        tags: list[str] | None = None,
    ) -> Task:
        async with file_lock(self._path):
            tasks = self._read_all()
            if any(t.id == id for t in tasks):
                raise ValueError(f"task id already exists: {id}")
            task = Task(
                id=id,
                content=content,
                status="pending",
                created_at=_now_iso(),
                due=due,
                tags=list(tags or []),
            )
            tasks.append(task)
            self._write_all(tasks)
            return task

    async def list_tasks(
        self, *, status: Literal["pending", "done", "cancelled", "all"] = "pending"
    ) -> list[Task]:
        async with file_lock(self._path):
            tasks = self._read_all()
            if status == "all":
                return tasks
            return [t for t in tasks if t.status == status]

    async def get(self, *, id: str) -> Task | None:
        async with file_lock(self._path):
            for t in self._read_all():
                if t.id == id:
                    return t
            return None

    async def mark_done(self, *, id: str) -> Task:
        async with file_lock(self._path):
            tasks = self._read_all()
            for t in tasks:
                if t.id == id:
                    t.status = "done"
                    t.done_at = _now_iso()
                    self._write_all(tasks)
                    return t
            raise KeyError(f"no such task: {id}")

    async def mark_cancelled(self, *, id: str) -> Task:
        async with file_lock(self._path):
            tasks = self._read_all()
            for t in tasks:
                if t.id == id:
                    t.status = "cancelled"
                    self._write_all(tasks)
                    return t
            raise KeyError(f"no such task: {id}")

    async def update(
        self,
        *,
        id: str,
        content: str | None = None,
        due: str | None = None,
        tags: list[str] | None = None,
    ) -> Task:
        async with file_lock(self._path):
            tasks = self._read_all()
            for t in tasks:
                if t.id == id:
                    if content is not None:
                        t.content = content
                    if due is not None:
                        t.due = due or None
                    if tags is not None:
                        t.tags = list(tags)
                    self._write_all(tasks)
                    return t
            raise KeyError(f"no such task: {id}")

    async def delete(self, *, id: str) -> bool:
        async with file_lock(self._path):
            tasks = self._read_all()
            new = [t for t in tasks if t.id != id]
            if len(new) == len(tasks):
                return False
            self._write_all(new)
            return True
