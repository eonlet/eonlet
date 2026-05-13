"""`eonlet replay` reads from state.db and prints in id order."""
from __future__ import annotations

from pathlib import Path

import pytest

from eonlet import paths
from eonlet.cli import commands
from eonlet.runtime.events import assistant_message, user_message
from eonlet.runtime.store import EventStore


def _stage(home: Path) -> str:
    root = home / "eonlets" / "assistant.alice"
    root.mkdir(parents=True)
    (root / "meta.json").write_text('{"name":"alice","type":"assistant"}')
    (root / "status").write_text("dead")
    store = EventStore(root / "state.db")
    for c in ("a", "b", "c", "d"):
        store.append(user_message(c))
        store.append(assistant_message(c.upper()))
    store.close()
    return "assistant.alice"


def test_replay_prints_id_range(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eid = _stage(isolated_home)
    commands.cmd_replay(eid, from_=3, to=5)
    captured = capsys.readouterr().out
    # 3, 4, 5 — that's 3 events in [3..5]
    assert "#   3" in captured
    assert "#   4" in captured
    assert "#   5" in captured
    assert "#   6" not in captured
    assert "#   2" not in captured
