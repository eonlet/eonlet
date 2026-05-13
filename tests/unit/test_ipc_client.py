"""IPCClient demuxer — concurrent request + notification interleaving.

Starts a tiny in-process server on a temp Unix socket that:
- Responds to ``echo`` with the same params.
- Pushes 3 notifications between two requests.

Verifies that the client cleanly resolves both requests *and* surfaces the
intervening notifications via the notifications() iterator.
"""
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from eonlet.worker.ipc import IPCClient


async def _serve(socket_path: str, scenario: list[str]) -> None:
    listener = await anyio.create_unix_listener(socket_path)

    async def handle(stream):
        buf = b""
        try:
            async for chunk in stream:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    req = json.loads(line)
                    method = req["method"]
                    rid = req.get("id")
                    if method == "echo":
                        # Send 3 notifications, then the response.
                        for i in range(3):
                            note = {
                                "jsonrpc": "2.0",
                                "method": "event",
                                "params": {"i": i},
                            }
                            await stream.send((json.dumps(note) + "\n").encode())
                        resp = {"jsonrpc": "2.0", "id": rid, "result": req["params"]}
                        await stream.send((json.dumps(resp) + "\n").encode())
                    elif method == "close":
                        return
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return

    async with listener:
        await listener.serve(handle)


def test_client_demuxes_requests_and_notifications(tmp_path: Path) -> None:
    sock = str(tmp_path / "s.sock")

    async def runner() -> tuple[dict, list[dict]]:
        result_holder: dict = {}
        notes: list[dict] = []

        async with anyio.create_task_group() as tg:
            tg.start_soon(_serve, sock, [])
            # Tiny race-avoidance: wait for the socket file.
            for _ in range(50):
                if Path(sock).exists():
                    break
                await anyio.sleep(0.01)

            async with IPCClient(sock) as client:
                async with anyio.create_task_group() as ctg:
                    ctg.start_soon(client.run)

                    async def collect_notes() -> None:
                        async for n in client.notifications():
                            notes.append(n)

                    ctg.start_soon(collect_notes)
                    r1 = await client.request("echo", {"x": 1})
                    r2 = await client.request("echo", {"x": 2})
                    result_holder["r1"] = r1
                    result_holder["r2"] = r2
                    # Wait briefly for the in-flight notes to flush.
                    with anyio.move_on_after(0.5):
                        while len(notes) < 6:
                            await anyio.sleep(0.01)
                    ctg.cancel_scope.cancel()
            tg.cancel_scope.cancel()

        return result_holder, notes

    results, notes = anyio.run(runner)
    assert results["r1"] == {"x": 1}
    assert results["r2"] == {"x": 2}
    # 3 notifications per request × 2 requests = 6
    assert len(notes) >= 6
    assert all(n.get("method") == "event" for n in notes)
