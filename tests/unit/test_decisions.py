"""DecisionBroker — the blocking user-decision round-trip (ADR-0006 M2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import anyio.lowlevel

from eonlet.worker.decisions import DECISION_REQUEST, DecisionBroker
from eonlet.worker.ipc import IPCClient, IPCServer


class _FakeServer:
    """Minimal stand-in for IPCServer: a session set + a broadcast recorder."""

    def __init__(self, *, attached: bool = True) -> None:
        self.sessions: dict[str, object] = {"s1": object()} if attached else {}
        self.broadcasts: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(self, method: str, params: dict[str, Any]) -> None:
        self.broadcasts.append((method, params))


def test_ask_declines_immediately_with_no_session() -> None:
    server = _FakeServer(attached=False)
    broker = DecisionBroker(server)

    async def go() -> str:
        return await broker.ask(
            kind="permission", prompt="p", options=["approve", "deny"], decline="deny"
        )

    assert anyio.run(go) == "deny"
    assert server.broadcasts == []  # nobody to ask → no notification


def test_ask_resolve_round_trip_and_first_responder_wins() -> None:
    server = _FakeServer()
    broker = DecisionBroker(server)
    result: dict[str, str] = {}

    async def go() -> None:
        async with anyio.create_task_group() as tg:

            async def do_ask() -> None:
                result["choice"] = await broker.ask(
                    kind="permission", prompt="Allow bash?", options=["approve", "deny"]
                )

            tg.start_soon(do_ask)
            # Wait until the request was broadcast (pending registered).
            while not server.broadcasts:
                await anyio.lowlevel.checkpoint()
            method, params = server.broadcasts[0]
            assert method == DECISION_REQUEST
            did = params["id"]
            assert broker.resolve(did, "approve") is True
            # A late/duplicate answer to the same id is ignored.
            assert broker.resolve(did, "deny") is False

    anyio.run(go)
    assert result["choice"] == "approve"


def test_resolve_unknown_id_is_noop() -> None:
    broker = DecisionBroker(_FakeServer())
    assert broker.resolve("nope", "approve") is False


def test_detach_while_pending_declines() -> None:
    server = _FakeServer()
    broker = DecisionBroker(server)
    result: dict[str, str] = {}

    async def go() -> None:
        async with anyio.create_task_group() as tg:

            async def do_ask() -> None:
                result["choice"] = await broker.ask(
                    kind="permission", prompt="p", options=["approve", "deny"], decline="deny"
                )

            tg.start_soon(do_ask)
            while not server.broadcasts:
                await anyio.lowlevel.checkpoint()
            # The last (only) session detaches before answering.
            server.sessions.clear()
            broker.on_session_closed("s1")

    anyio.run(go)
    assert result["choice"] == "deny"


def test_detach_with_other_session_left_keeps_pending() -> None:
    server = _FakeServer()
    server.sessions["s2"] = object()  # two attached
    broker = DecisionBroker(server)
    result: dict[str, str] = {}

    async def go() -> None:
        async with anyio.create_task_group() as tg:

            async def do_ask() -> None:
                result["choice"] = await broker.ask(
                    kind="permission", prompt="p", options=["approve", "deny"]
                )

            tg.start_soon(do_ask)
            while not server.broadcasts:
                await anyio.lowlevel.checkpoint()
            # One of two sessions detaches — the decision stays open.
            server.sessions.pop("s1")
            broker.on_session_closed("s1")
            await anyio.lowlevel.checkpoint()
            assert "choice" not in result  # still blocked
            did = server.broadcasts[0][1]["id"]
            broker.resolve(did, "approve")

    anyio.run(go)
    assert result["choice"] == "approve"


def test_decision_round_trip_over_real_socket(short_tmp_path: Path) -> None:
    """End-to-end across a real Unix socket: broker broadcasts a decision/request,
    a connected client answers via decision.respond, broker.ask unblocks."""
    sock = str(short_tmp_path / "d.sock")
    holder: dict[str, DecisionBroker] = {}

    async def handler(method: str, params: dict[str, Any]) -> Any:
        if method == "session.start":
            # Mirror the worker: only sessions that declare themselves
            # interactive count as decision listeners.
            params["_session"].interactive = bool(params.get("interactive", True))
            return {"ok": True}
        if method == "decision.respond":
            ok = holder["broker"].resolve(str(params.get("id")), str(params.get("choice")))
            return {"ok": ok}
        return {"ok": True}

    async def runner() -> str:
        server = IPCServer(sock, handler)
        broker = DecisionBroker(server)
        holder["broker"] = broker
        server.on_disconnect = broker.on_session_closed
        result: dict[str, str] = {}

        async with anyio.create_task_group() as tg:
            tg.start_soon(server.serve)
            for _ in range(100):
                if Path(sock).exists():
                    break
                await anyio.sleep(0.01)

            async with IPCClient(sock) as client, anyio.create_task_group() as ctg:
                ctg.start_soon(client.run)
                # Declare an interactive session, then wait until the server
                # counts us as a decision listener.
                await client.request("session.start", {"interactive": True})
                for _ in range(100):
                    if broker.has_listener():
                        break
                    await anyio.sleep(0.01)

                async def answer() -> None:
                    async for note in client.notifications():
                        if note.get("method") == DECISION_REQUEST:
                            did = note["params"]["id"]
                            await client.request(
                                "decision.respond", {"id": did, "choice": "approve"}
                            )
                            return

                ctg.start_soon(answer)
                result["choice"] = await broker.ask(
                    kind="permission", prompt="Allow?", options=["approve", "deny"]
                )
                ctg.cancel_scope.cancel()
            tg.cancel_scope.cancel()
        return result["choice"]

    assert anyio.run(runner) == "approve"
