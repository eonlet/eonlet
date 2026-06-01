"""Blocking user-decision round-trip (ADR-0006 M2).

The worker can pause a turn to ask the attached user a yes/no question and wait
for the answer. This is one generic channel shared by two callers: the
interactive permission confirm (this milestone) and, later, compaction proposals
(M3).

Flow::

    agent task          broker                       CLI (attached)
      ask() ─────────▶ register pending
                       broadcast "decision/request" ──▶ render + prompt
      await event ◀──  (blocked)
                       resolve(id, choice) ◀────────── "decision.respond" request
      return choice ◀─ event.set()

If no session is attached when ``ask`` is called — or the last session detaches
*while* a decision is pending — the decision auto-declines, so a headless worker
never hangs (ADR-0006 / plan M2 "no-session → auto-decline").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import anyio

from .ipc import IPCServer

log = logging.getLogger("eonlet.worker.decisions")

# Notification method pushed to attached sessions; response comes back as a
# "decision.respond" request. Kept as a module constant so the CLI and tests
# reference the same string.
DECISION_REQUEST = "decision/request"


@dataclass(slots=True)
class _Pending:
    id: str
    event: anyio.Event
    choice: str | None = None


class DecisionBroker:
    """Registry + round-trip for blocking user decisions over IPC."""

    def __init__(self, server: IPCServer) -> None:
        self._server = server
        self._pending: dict[str, _Pending] = {}
        self._counter = 0

    def has_listener(self) -> bool:
        """True if at least one client session is currently attached."""
        return bool(self._server.sessions)

    async def ask(
        self,
        *,
        kind: str,
        prompt: str,
        options: list[str],
        payload: dict[str, Any] | None = None,
        decline: str = "deny",
    ) -> str:
        """Ask the attached user to choose one of ``options``; block until they do.

        Returns the chosen option string. Returns ``decline`` immediately when
        no session is attached, and returns ``decline`` if the last session
        detaches before an answer arrives.
        """
        if not self.has_listener():
            return decline
        self._counter += 1
        did = f"d{self._counter}"
        pending = _Pending(id=did, event=anyio.Event())
        self._pending[did] = pending
        await self._server.broadcast(
            DECISION_REQUEST,
            {
                "id": did,
                "kind": kind,
                "prompt": prompt,
                "options": options,
                "payload": payload or {},
            },
        )
        try:
            await pending.event.wait()
        finally:
            self._pending.pop(did, None)
        return pending.choice if pending.choice is not None else decline

    def resolve(self, decision_id: str, choice: str) -> bool:
        """Record a user's answer. First responder wins.

        Returns True if the answer was applied, False if the id is unknown or
        already resolved (a late/duplicate ``decision.respond`` is ignored).
        """
        pending = self._pending.get(decision_id)
        if pending is None or pending.event.is_set():
            return False
        pending.choice = choice
        pending.event.set()
        return True

    def on_session_closed(self, _sid: str) -> None:
        """Decline every outstanding decision once the last session detaches, so
        a blocked agent task unblocks instead of hanging forever.
        """
        if self._server.sessions:
            return
        for pending in list(self._pending.values()):
            if not pending.event.is_set():
                # Leave choice=None → ask() falls back to its decline default.
                pending.event.set()


__all__ = ["DECISION_REQUEST", "DecisionBroker"]
