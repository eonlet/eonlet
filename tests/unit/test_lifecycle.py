"""Worker lifecycle filesystem files: pid/status/heartbeat/meta round-trip."""
from __future__ import annotations

from pathlib import Path

import pytest

from eonlet import paths
from eonlet.worker import lifecycle


def test_status_round_trip(isolated_home: Path) -> None:
    eid = "x.y"
    paths.eonlet_dir(eid).mkdir(parents=True)
    lifecycle.write_status(eid, "running")
    assert lifecycle.read_status(eid) == "running"
    lifecycle.write_status(eid, "dead")
    assert lifecycle.read_status(eid) == "dead"


def test_pid_round_trip(isolated_home: Path) -> None:
    eid = "x.y"
    paths.eonlet_dir(eid).mkdir(parents=True)
    lifecycle.write_pid(eid)
    pid = lifecycle.read_pid(eid)
    assert pid and pid > 0
    # process_alive should return True for our own PID.
    assert lifecycle.process_alive(pid)
    assert not lifecycle.process_alive(2_000_000)  # implausible pid
    assert not lifecycle.process_alive(None)


def test_heartbeat_writes_timestamp(isolated_home: Path) -> None:
    eid = "x.y"
    paths.eonlet_dir(eid).mkdir(parents=True)
    lifecycle.write_heartbeat(eid)
    hb = paths.heartbeat_file(eid).read_text()
    assert hb.isdigit() and int(hb) > 0


def test_meta_round_trip(isolated_home: Path) -> None:
    eid = "assistant.alice"
    paths.eonlet_dir(eid).mkdir(parents=True)
    lifecycle.write_meta(eid, type_="assistant", name="alice", definition=Path("/x"), version="0.1.0")
    m = lifecycle.read_meta(eid)
    assert m["type"] == "assistant" and m["name"] == "alice"


def test_cleanup_removes_only_transient_files(isolated_home: Path) -> None:
    eid = "x.y"
    root = paths.eonlet_dir(eid)
    root.mkdir(parents=True)
    paths.runtime_sock(eid).write_text("dummy")
    paths.pid_file(eid).write_text("123")
    paths.heartbeat_file(eid).write_text("0")
    (root / "state.db").write_text("persists")
    lifecycle.cleanup(eid)
    assert not paths.runtime_sock(eid).exists()
    assert not paths.pid_file(eid).exists()
    assert not paths.heartbeat_file(eid).exists()
    assert (root / "state.db").exists()
