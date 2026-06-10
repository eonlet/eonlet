"""Event store roundtrip — SPEC §12 invariant I1."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from eonlet.runtime.events import EventKind, assistant_message, tool_call, user_message
from eonlet.runtime.state import fold
from eonlet.runtime.store import EventStore


def test_task_id_roundtrips(tmp_path: Path) -> None:
    # ADR-0009: the task scope is a structural Event field, persisted + restored.
    store = EventStore(tmp_path / "state.db")
    store.append(user_message("chat turn"))  # task_id None (chat scope)
    store.append(user_message("task turn").model_copy(update={"task_id": "task-x"}))
    fetched = store.read()
    assert fetched[0].task_id is None
    assert fetched[1].task_id == "task-x"


def test_task_id_column_added_to_legacy_db(tmp_path: Path) -> None:
    # A store created before ADR-0009 lacks the task_id column; opening it must
    # add the column (additive migration) and keep working.
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL,
            payload BLOB NOT NULL, parent_id INTEGER, trigger_id TEXT,
            cost_usd REAL, tokens_in INTEGER, tokens_out INTEGER)"""
    )
    conn.commit()
    conn.close()
    store = EventStore(db)  # should ALTER TABLE to add task_id
    stored = store.append(user_message("hi").model_copy(update={"task_id": "task-y"}))
    assert stored.id == 1
    assert store.read()[0].task_id == "task-y"


def test_append_assigns_id_and_roundtrips(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    e1 = store.append(user_message("hi"))
    e2 = store.append(assistant_message("hello"))
    assert e1.id == 1
    assert e2.id == 2
    fetched = store.read()
    assert [e.kind for e in fetched] == [EventKind.USER_MESSAGE, EventKind.ASSISTANT_MESSAGE]
    assert fetched[0].payload == {"content": "hi"}


def test_fold_reconstructs_conversation(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    store.append(user_message("compute"))
    store.append(
        assistant_message("calling tool", tool_calls=[{"id": "1", "name": "x", "args": {}}])
    )
    store.append(tool_call("1", "x", {}))
    events = store.read()
    state = fold(events)
    roles = [m.role for m in state.messages]
    assert roles == ["user", "assistant"]  # tool_call alone doesn't create a message
    assert state.messages[1].tool_calls[0]["id"] == "1"


def test_trigger_state_upsert(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "state.db")
    assert store.get_trigger_state("daily")["consecutive_failures"] == 0
    store.update_trigger_state("daily", last_fired_at=42, total_fires=1)
    s = store.get_trigger_state("daily")
    assert s["last_fired_at"] == 42 and s["total_fires"] == 1
    store.update_trigger_state("daily", consecutive_failures=3)
    assert store.get_trigger_state("daily")["consecutive_failures"] == 3
