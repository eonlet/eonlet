"""Event store roundtrip — SPEC §12 invariant I1."""
from __future__ import annotations

from pathlib import Path

import pytest

from eonlet.runtime.events import EventKind, assistant_message, tool_call, user_message
from eonlet.runtime.state import fold
from eonlet.runtime.store import EventStore


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
    store.append(assistant_message("calling tool", tool_calls=[{"id": "1", "name": "x", "args": {}}]))
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
