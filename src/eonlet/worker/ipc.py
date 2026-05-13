"""JSON-RPC 2.0 over a Unix socket, newline-framed.

Per SPEC §8.1. The worker side is the server; the CLI is the client.

We use newline-framed JSON (one JSON object per line) for simplicity. Each
direction can carry:

  - Requests   : {"jsonrpc": "2.0", "id": ..., "method": ..., "params": ...}
  - Responses  : {"jsonrpc": "2.0", "id": ..., "result": ...}  or "error"
  - Notifications: same as requests but with no "id" field. Used for server-
                   pushed events (event/token_delta/tool_use_*).

This is intentionally minimal — no batch, no JSON-RPC over JSON-RPC, no
binary framing. Sufficient for v0.0.1.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import SocketStream
from anyio.streams.memory import MemoryObjectReceiveStream

from ..errors import IPCError

log = logging.getLogger("eonlet.worker.ipc")


Handler = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class Session:
    """One attached client. Holds the send-half of the connection."""

    id: str
    stream: SocketStream
    _lock: anyio.Lock

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._send(msg)

    async def respond(self, request_id: Any, result: Any) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._send(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        line = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._lock:
            try:
                await self.stream.send(line)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                # Client went away; surface as broken so the server tears down.
                raise


class IPCServer:
    """Listens on a Unix socket, dispatches requests to a handler.

    Sessions are tracked so the runtime can push notifications to them.
    """

    def __init__(self, socket_path: str, handler: Handler) -> None:
        self.socket_path = socket_path
        self.handler = handler
        self.sessions: dict[str, Session] = {}
        self._next_id = 0
        self._sessions_lock = anyio.Lock()

    async def serve(self) -> None:
        listener = await anyio.create_unix_listener(self.socket_path)
        log.info("ipc: listening on %s", self.socket_path)
        async with listener:
            await listener.serve(self._handle_connection)

    async def broadcast(self, method: str, params: dict[str, Any]) -> None:
        """Send a notification to every attached session. Best-effort."""
        dead: list[str] = []
        async with self._sessions_lock:
            targets = list(self.sessions.values())
        for s in targets:
            try:
                await s.notify(method, params)
            except Exception:
                dead.append(s.id)
        if dead:
            async with self._sessions_lock:
                for sid in dead:
                    self.sessions.pop(sid, None)

    async def _handle_connection(self, stream: SocketStream) -> None:
        self._next_id += 1
        sid = f"s{self._next_id}"
        session = Session(id=sid, stream=stream, _lock=anyio.Lock())
        async with self._sessions_lock:
            self.sessions[sid] = session
        log.info("ipc: session %s opened", sid)
        try:
            buffer = b""
            async for chunk in stream:
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    await self._process_line(session, line)
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            pass
        except Exception:
            log.exception("ipc: session %s crashed", sid)
        finally:
            async with self._sessions_lock:
                self.sessions.pop(sid, None)
            log.info("ipc: session %s closed", sid)

    async def _process_line(self, session: Session, line: bytes) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            await session.respond_error(None, -32700, f"parse error: {e}")
            return
        method = req.get("method")
        params = req.get("params") or {}
        request_id = req.get("id")
        if not method:
            await session.respond_error(request_id, -32600, "invalid request")
            return
        try:
            result = await self.handler(method, {"_session": session, **params})
        except Exception as e:
            log.exception("ipc: handler raised for %s", method)
            await session.respond_error(request_id, -32603, f"internal error: {e}")
            return
        if request_id is not None:
            await session.respond(request_id, result)


# ── client side (for the CLI) ────────────────────────────────────────────────


class IPCClient:
    """Newline-JSON-RPC client with a single background reader task.

    A single reader owns the inbound stream and demultiplexes:

    - **Responses** (``"id"`` present) → resolved on the pending-request map.
    - **Notifications** (no ``"id"``) → pushed onto a memory channel that
      ``notifications()`` exposes as an async iterator.

    This lets callers concurrently issue ``request(...)`` *and* iterate over
    notifications without racing on stream reads — fixing the workaround in
    v0.0.2 where the CLI sent fire-and-forget messages as notifications.

    Usage::

        async with IPCClient(sock) as client:
            async with anyio.create_task_group() as tg:
                tg.start_soon(client.run)
                ...
                await client.request("session.start", {})
                async for note in client.notifications():
                    ...
    """

    def __init__(self, socket_path: str) -> None:
        self.socket_path = socket_path
        self._stream: SocketStream | None = None
        self._buffer = b""
        self._next_id = 0
        self._write_lock = anyio.Lock()
        # Pending requests awaiting responses: id -> anyio.Event-style payload.
        self._pending: dict[int, anyio.Event] = {}
        self._results: dict[int, dict[str, Any]] = {}
        # Notification channel — bounded so a misbehaving server doesn't OOM us.
        self._note_send, self._note_recv = anyio.create_memory_object_stream[dict[str, Any]](128)
        self._closed = False

    async def __aenter__(self) -> IPCClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._stream = await anyio.connect_unix(self.socket_path)
        except OSError as e:
            raise IPCError(f"cannot connect to {self.socket_path}: {e}") from e

    async def close(self) -> None:
        self._closed = True
        if self._stream is not None:
            await self._stream.aclose()
            self._stream = None
        # Wake any pending callers so they unblock with EOF.
        for ev in self._pending.values():
            ev.set()
        await self._note_send.aclose()

    async def run(self) -> None:
        """Background reader loop. Start once via a task group, run for the
        lifetime of the connection. Returns on stream close.
        """
        assert self._stream is not None, "not connected"
        try:
            while not self._closed:
                msg = await self._read_one()
                if "id" in msg and msg["id"] is not None and "method" not in msg:
                    # It's a response.
                    rid = int(msg["id"])
                    self._results[rid] = msg
                    ev = self._pending.pop(rid, None)
                    if ev is not None:
                        ev.set()
                else:
                    # Notification — best-effort enqueue; drop if backed up.
                    try:
                        self._note_send.send_nowait(msg)
                    except anyio.WouldBlock:
                        log.warning("ipc client: notification dropped (queue full)")
        except (anyio.EndOfStream, anyio.ClosedResourceError):
            return
        finally:
            # Make sure any pending callers see EOF.
            for ev in list(self._pending.values()):
                ev.set()
            await self._note_send.aclose()

    async def send_raw(self, payload: dict[str, Any]) -> None:
        assert self._stream is not None, "not connected"
        line = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._write_lock:
            await self._stream.send(line)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Send a request and await its response. Concurrent-safe."""
        assert self._stream is not None, "not connected"
        self._next_id += 1
        rid = self._next_id
        ev = anyio.Event()
        self._pending[rid] = ev
        await self.send_raw({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        await ev.wait()
        msg = self._results.pop(rid, None)
        if msg is None:
            # Reader closed without a result.
            raise IPCError(f"{method}: connection closed before response")
        if "error" in msg:
            raise IPCError(f"{method}: {msg['error']}")
        return msg.get("result")

    def notifications(self) -> MemoryObjectReceiveStream[dict[str, Any]]:
        """Async iterator over server-pushed notifications."""
        return self._note_recv

    async def _read_one(self) -> dict[str, Any]:
        assert self._stream is not None, "not connected"
        while b"\n" not in self._buffer:
            chunk = await self._stream.receive(4096)
            if not chunk:
                raise anyio.EndOfStream
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        result: dict[str, Any] = json.loads(line)
        return result
