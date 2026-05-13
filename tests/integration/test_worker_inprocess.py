"""In-process worker integration test.

Runs ``run_worker`` directly in a TaskGroup (no subprocess) so pytest-cov can
see the worker code paths. The subprocess test in ``test_worker_subprocess.py``
remains for "real OS process" coverage; this one drives the same logic so the
coverage report reflects it.

Connects via a temp Unix socket using the same ``IPCClient`` the CLI uses.
Shutdown is signaled by setting the ``anyio.Event`` directly.
"""
from __future__ import annotations

import functools
from pathlib import Path

import anyio
import pytest

from eonlet import paths
from eonlet.worker.ipc import IPCClient
from eonlet.worker.lifecycle import write_meta
from eonlet.worker.main import run_worker


def _write_fake_definition(home_root: Path, model: str = "fake-echo") -> Path:
    d = paths.agents_dir() / "echobot"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        f"""apiVersion: eonlet/v1
kind: Agent
metadata:
  name: echobot
  description: in-process test
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
    (d / "system.md").write_text("# echobot\nbe terse.\n", encoding="utf-8")
    return d


def _prep_eonlet(eid: str, defn: Path) -> None:
    paths.eonlet_dir(eid).mkdir(parents=True)
    paths.memory_dir(eid).mkdir()
    paths.workspace_dir(eid).mkdir()
    paths.logs_dir(eid).mkdir()
    write_meta(eid, type_="echobot", name="test", definition=defn, version="0.0.1")


def test_inproc_streams_back(isolated_home: Path) -> None:
    """End-to-end with the real IPC server + IPC client + agent loop + fake provider."""
    paths.ensure_home()
    defn = _write_fake_definition(isolated_home)
    eid = "echobot.alice"
    _prep_eonlet(eid, defn)

    deltas: list[str] = []
    final_content: list[str] = []

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            # Wait for the socket to appear before connecting.
            sock = str(paths.runtime_sock(eid))
            for _ in range(100):
                if Path(sock).exists():
                    break
                await anyio.sleep(0.02)
            else:
                pytest.fail(f"socket {sock} never appeared")

            async with IPCClient(sock) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})
                    await client.request("message.send", {"content": "hi"})
                    async for msg in client.notifications():
                        method = msg.get("method")
                        params = msg.get("params") or {}
                        if method == "token_delta":
                            deltas.append(params["delta_text"])
                        elif method == "event" and params.get("kind") == "assistant_message":
                            final_content.append(params["payload"]["content"])
                            ctg.cancel_scope.cancel()
                            break

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(10):
            await go()

    anyio.run(with_timeout)

    assert "".join(deltas) == "echo: hi"
    assert final_content == ["echo: hi"]


def test_inproc_trigger_fire(isolated_home: Path) -> None:
    """Define an agent with a cron trigger, then manually fire it via IPC."""
    paths.ensure_home()
    # Definition with a daily trigger — the cron schedule won't actually fire in
    # the test's 10s budget, but ``trigger.fire`` skips the schedule.
    d = paths.agents_dir() / "scheduled"
    d.mkdir(parents=True)
    (d / "agent.yaml").write_text(
        """apiVersion: eonlet/v1
kind: Agent
metadata:
  name: scheduled
  description: t
  version: 0.0.1
runtime:
  model: fake-echo
triggers:
  - id: morning
    kind: cron
    schedule: "0 8 * * *"
    timezone: UTC
    message: "do the morning thing"
    grace_period: 0s
tools:
  builtin: [sleep]
permissions:
  mode: yolo
""",
        encoding="utf-8",
    )
    (d / "system.md").write_text("# scheduled bot\n", encoding="utf-8")
    eid = "scheduled.morn"
    _prep_eonlet(eid, d)

    fired_message: list[dict] = []

    async def go() -> None:
        shutdown = anyio.Event()
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                functools.partial(run_worker, eid, shutdown, install_signal_watcher=False)
            )
            for _ in range(100):
                if paths.runtime_sock(eid).exists():
                    break
                await anyio.sleep(0.02)

            async with IPCClient(str(paths.runtime_sock(eid))) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)
                    await client.request("session.start", {"client_id": "test"})

                    # Verify triggers.list reflects the configured trigger.
                    listing = await client.request("triggers.list", {})
                    assert any(t["id"] == "morning" for t in listing["triggers"])

                    # Fire it.
                    resp = await client.request(
                        "trigger.fire", {"trigger_id": "morning"}
                    )
                    assert resp["ok"]

                    # Listen for the resulting assistant_message.
                    async for msg in client.notifications():
                        if msg.get("method") == "event" and msg["params"].get("kind") == "assistant_message":
                            fired_message.append(msg["params"]["payload"])
                            ctg.cancel_scope.cancel()
                            break

            shutdown.set()
            tg.cancel_scope.cancel()

    async def with_timeout() -> None:
        with anyio.fail_after(10):
            await go()

    anyio.run(with_timeout)

    assert len(fired_message) == 1
    # The fake-echo provider echoes back the trigger envelope, which is fine —
    # we just want to confirm the trigger fired and produced a response.
    assert fired_message[0]["content"].startswith("echo:")
