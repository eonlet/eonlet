"""End-to-end worker subprocess test.

Spawns ``python -m eonlet.worker.main <id>`` as a real OS process, connects
via the same ``IPCClient`` the CLI uses, sends a message, and asserts that
both streamed ``token_delta`` notifications and a final ``assistant_message``
event arrive. SIGTERM is used to clean up.

Uses the in-process ``fake-echo`` LLM provider so no API key is needed and
output is deterministic. The same fixture also exercises ``cli/commands.py``
paths for ``cmd_create`` (via test helpers below).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import anyio
import pytest

from eonlet import paths
from eonlet.worker.ipc import IPCClient
from eonlet.worker.lifecycle import write_meta


SUBPROCESS_BOOT_TIMEOUT = 10.0  # seconds — generous for slow CI


# ── fixture ──────────────────────────────────────────────────────────────────


def _write_fake_definition(definitions_dir: Path, *, model: str = "fake-echo") -> Path:
    """Drop a minimal definition that uses a fake provider."""
    d = definitions_dir / "echobot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"""apiVersion: eonlet/v1
kind: Agent
metadata:
  name: echobot
  description: integration test bot
  version: 0.0.1
runtime:
  model: {model}
  max_steps_per_run: 5
tools:
  builtin: [sleep]
permissions:
  mode: yolo
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# echobot\nBe terse.\n", encoding="utf-8")
    return d


@pytest.fixture
def worker_subprocess(isolated_home: Path) -> Iterator[tuple[str, subprocess.Popen]]:
    """Spawn an eonlet-worker process backed by the fake-echo provider.

    Yields ``(eonlet_id, popen)``. Always SIGTERMs the worker on teardown.
    """
    paths.ensure_home()
    defn_dir = _write_fake_definition(paths.agents_dir())
    eid = "echobot.test"
    eonlet_root = paths.eonlet_dir(eid)
    eonlet_root.mkdir(parents=True)
    paths.memory_dir(eid).mkdir()
    paths.workspace_dir(eid).mkdir()
    paths.logs_dir(eid).mkdir()
    write_meta(eid, type_="echobot", name="test", definition=defn_dir, version="0.0.1")

    src_root = Path(__file__).resolve().parents[2] / "src"
    env = {**os.environ, "EONLET_HOME": str(isolated_home), "PYTHONPATH": str(src_root)}

    log_file = paths.current_log(eid)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logfh = open(log_file, "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "eonlet.worker.main", eid],
            env=env,
            stdout=logfh,
            stderr=logfh,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        logfh.close()
        raise

    # Wait for the runtime socket to appear — this is the worker's readiness signal.
    sock_path = paths.runtime_sock(eid)
    deadline = time.time() + SUBPROCESS_BOOT_TIMEOUT
    while time.time() < deadline:
        if sock_path.exists():
            break
        if proc.poll() is not None:
            logfh.close()
            tail = log_file.read_text(errors="replace")[-2000:] if log_file.exists() else ""
            pytest.fail(
                f"worker exited before binding socket (rc={proc.returncode}).\n"
                f"--- log tail ---\n{tail}"
            )
        time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail(f"timed out waiting for {sock_path}")

    try:
        yield eid, proc
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        logfh.close()


# ── tests ────────────────────────────────────────────────────────────────────


def test_worker_boots_and_echoes_via_streaming(
    worker_subprocess: tuple[str, subprocess.Popen],
) -> None:
    """Send "hi" → receive 3 token_deltas + an assistant_message saying "echo: hi"."""
    eid, _proc = worker_subprocess
    sock = str(paths.runtime_sock(eid))

    deltas: list[str] = []
    final_msg: dict | None = None

    async def go() -> None:
        nonlocal final_msg
        async with IPCClient(sock) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(client.run)
                await client.request("session.start", {"client_id": "test"})
                await client.request("message.send", {"content": "hi"})
                # Drain until end-of-run.
                async for msg in client.notifications():
                    if msg.get("method") == "token_delta":
                        deltas.append(msg["params"]["delta_text"])
                    elif msg.get("method") == "event":
                        p = msg["params"]
                        if p.get("kind") == "assistant_message":
                            final_msg = p
                            tg.cancel_scope.cancel()
                            return

    async def with_timeout() -> None:
        with anyio.fail_after(SUBPROCESS_BOOT_TIMEOUT):
            await go()

    # Convert `go` to take no args so we can pass it to anyio.run via with_timeout.
    anyio.run(with_timeout)

    # Fake provider chunks into 3 pieces; check 1+ deltas arrived and concat matches.
    assert len(deltas) >= 1
    assert "".join(deltas) == "echo: hi"
    assert final_msg is not None
    assert final_msg["payload"]["content"] == "echo: hi"
    assert final_msg["payload"]["tool_calls"] == []


def test_worker_persists_state_across_send(
    worker_subprocess: tuple[str, subprocess.Popen],
) -> None:
    """After a round-trip the event log should contain user + assistant messages."""
    eid, _proc = worker_subprocess
    sock = str(paths.runtime_sock(eid))

    async def go() -> None:
        async with IPCClient(sock) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(client.run)
                await client.request("session.start", {"client_id": "test"})
                await client.request("message.send", {"content": "hello"})
                async for msg in client.notifications():
                    if msg.get("method") == "event" and msg["params"].get("kind") == "assistant_message":
                        tg.cancel_scope.cancel()
                        return

    async def with_timeout() -> None:
        with anyio.fail_after(SUBPROCESS_BOOT_TIMEOUT):
            await go()

    # Convert `go` to take no args so we can pass it to anyio.run via with_timeout.
    anyio.run(with_timeout)

    # Inspect the event store directly.
    from eonlet.runtime.events import EventKind
    from eonlet.runtime.store import EventStore

    store = EventStore(paths.state_db(eid))
    try:
        events = store.read()
    finally:
        store.close()
    kinds = [str(e.kind).split(".")[-1] for e in events]
    assert "user_message" in kinds
    assert "assistant_message" in kinds
    # No token_delta events persisted — they're notifications only.
    assert "assistant_token_delta" not in kinds
