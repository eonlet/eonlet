"""export → import → re-read round trip preserves event log and memory files."""

from __future__ import annotations

from pathlib import Path

from eonlet import paths
from eonlet.cli import commands
from eonlet.runtime.events import user_message
from eonlet.runtime.store import EventStore


def _stage_eonlet(home: Path, eid: str = "assistant.alice") -> Path:
    """Create just enough on disk that export/import can find the eonlet."""
    root = home / "eonlets" / eid
    (root / "memory").mkdir(parents=True)
    (root / "workspace").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "memory" / "notes.md").write_text("hello from notes", encoding="utf-8")
    (root / "workspace" / "scratch.txt").write_text("artifact", encoding="utf-8")
    # Junk that should be excluded by export.
    (root / "pid").write_text("12345")
    (root / "logs" / "current.log").write_text("noise")
    (root / "runtime.sock").write_text("dummy")
    # meta.json so resolve_eonlet_id is happy.
    (root / "meta.json").write_text('{"name": "alice", "type": "assistant"}', encoding="utf-8")
    (root / "status").write_text("dead")
    # Append an event so the imported DB is interesting.
    store = EventStore(root / "state.db")
    store.append(user_message("first"))
    store.append(user_message("second"))
    store.close()
    return root


def test_export_import_roundtrip(isolated_home: Path) -> None:
    src = _stage_eonlet(isolated_home, "assistant.alice")
    archive = isolated_home / "alice.tar.gz"

    commands.cmd_export("alice", archive)
    assert archive.exists() and archive.stat().st_size > 0

    # Wipe original and re-import under a new name.
    import shutil

    shutil.rmtree(src)
    commands.cmd_import(archive, as_name="assistant.bob")

    bob_root = paths.eonlet_dir("assistant.bob")
    assert (bob_root / "memory" / "notes.md").read_text() == "hello from notes"
    assert (bob_root / "workspace" / "scratch.txt").read_text() == "artifact"
    # Transient files must NOT be present.
    assert not (bob_root / "pid").exists()
    assert not (bob_root / "logs" / "current.log").exists()
    assert not (bob_root / "runtime.sock").exists()
    # Event log carries over.
    store = EventStore(bob_root / "state.db")
    events = store.read()
    store.close()
    assert [e.payload["content"] for e in events] == ["first", "second"]
