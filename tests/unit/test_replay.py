"""`eonlet replay` reads from state.db and prints in id order."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eonlet.cli import commands
from eonlet.runtime.events import (
    Event,
    EventKind,
    assistant_message,
    tool_call,
    tool_result,
    user_message,
)
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


def _stage_long_content(home: Path) -> str:
    """Stage events whose content exceeds the legacy 100/120-char slice, so we
    can pin that the new renderer keeps the full bytes intact — mirrors the
    real bug seen in ``chat-log.txt`` where a tool_error's stderr was sliced
    mid-path."""
    root = home / "eonlets" / "assistant.alice"
    root.mkdir(parents=True)
    (root / "meta.json").write_text('{"name":"alice","type":"assistant"}')
    (root / "status").write_text("dead")
    store = EventStore(root / "state.db")
    long_script = "#!/usr/bin/env python3\n" + "print('x')\n" * 50
    long_stderr = (
        "python3: can't open file '/home/zzyu/.eonlet/eonlets/assistant.alice/"
        "workspace/workspace/hello.py': [Errno 2] No such file or directory"
    )
    store.append(user_message("write a script and run it"))
    store.append(
        assistant_message(
            "ok",
            tool_calls=[
                {
                    "call_id": "c1",
                    "tool_name": "file_write",
                    "args": {"path": "hello.py", "content": long_script},
                }
            ],
        )
    )
    store.append(tool_call("c1", "file_write", {"path": "hello.py", "content": long_script}))
    store.append(
        tool_result(
            "c1",
            "file_write",
            f"exit=2\n--- stdout ---\n\n--- stderr ---\n{long_stderr}",
            is_error=True,
        )
    )
    store.close()
    return "assistant.alice"


def test_replay_prints_id_range(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eid = _stage(isolated_home)
    commands.cmd_replay(eid, from_=3, to=5)
    captured = capsys.readouterr().out
    assert "#3" in captured
    assert "#4" in captured
    assert "#5" in captured
    assert "#6" not in captured
    assert "#2" not in captured


def test_replay_compact_keeps_legacy_one_liner(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eid = _stage(isolated_home)
    commands.cmd_replay(eid, from_=3, to=5, compact=True)
    captured = capsys.readouterr().out
    assert "#   3" in captured
    assert "#   5" in captured


def test_replay_full_content_not_truncated(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing the LLM saw should be hidden behind a `[:100]` slice."""
    eid = _stage_long_content(isolated_home)
    commands.cmd_replay(eid, from_=None, to=None)
    captured = capsys.readouterr().out
    assert "No such file or directory" in captured
    assert captured.count("print('x')") >= 40


def test_replay_jsonl_emits_one_object_per_event(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eid = _stage(isolated_home)
    commands.cmd_replay(eid, from_=None, to=None, fmt="jsonl")
    captured = capsys.readouterr().out
    lines = [line for line in captured.splitlines() if line.strip()]
    assert len(lines) == 8
    rows = [json.loads(line) for line in lines]
    assert {r["id"] for r in rows} == {1, 2, 3, 4, 5, 6, 7, 8}
    assert all("payload" in r and "ts" in r and "kind" in r for r in rows)


def test_replay_head_and_tail(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eid = _stage(isolated_home)
    commands.cmd_replay(eid, from_=None, to=None, head=2, fmt="jsonl")
    rows = [json.loads(s) for s in capsys.readouterr().out.splitlines() if s.strip()]
    assert [r["id"] for r in rows] == [1, 2]

    commands.cmd_replay(eid, from_=None, to=None, tail=2, fmt="jsonl")
    rows = [json.loads(s) for s in capsys.readouterr().out.splitlines() if s.strip()]
    assert [r["id"] for r in rows] == [7, 8]


def test_replay_shows_token_and_cost_metadata(
    isolated_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tokens_in/out and cost_usd are top-level Event fields the old replay
    never surfaced."""
    root = isolated_home / "eonlets" / "assistant.alice"
    root.mkdir(parents=True)
    (root / "meta.json").write_text('{"name":"alice","type":"assistant"}')
    (root / "status").write_text("dead")
    store = EventStore(root / "state.db")
    store.append(user_message("hi"))
    store.append(
        Event(
            kind=EventKind.ASSISTANT_MESSAGE,
            payload={"content": "hello", "tool_calls": []},
            tokens_in=120,
            tokens_out=8,
            cost_usd=0.00042,
        )
    )
    store.close()
    commands.cmd_replay("assistant.alice", from_=None, to=None)
    captured = capsys.readouterr().out
    assert "tokens 120→8" in captured
    assert "cost=$0.0004" in captured
