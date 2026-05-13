"""Agent main loop.

Wires together definition + LLM + tools + permission gate + event store.
A "run" begins when the loop pulls a user message off the trigger queue and
ends when the LLM emits ``end_turn`` (no further tool calls) or hits the
step cap.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..llm import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, build_provider
from ..permissions import PermissionGate
from ..tools import ToolContext, ToolResult, get_registry
from .definition import Definition
from .events import (
    Event,
    EventKind,
    assistant_message,
    tool_call,
    tool_result,
    user_message,
)
from .state import AgentState, fold, recent
from .store import EventStore

log = logging.getLogger("eonlet.runtime.agent")


@dataclass(slots=True)
class AgentRuntime:
    """Stateful runtime for one eonlet instance."""

    eonlet_id: str
    definition: Definition
    store: EventStore
    workspace: Path
    memory_dir: Path
    provider: LLMProvider
    gate: PermissionGate
    state: AgentState = field(default_factory=AgentState)
    # Callable[[Event], Awaitable[None]] | None — wired by the worker to push to IPC.
    event_listener: Callable[[Event], Any] | None = None
    # Callable[[str], Awaitable[None]] | None — token delta push (not persisted).
    on_delta: Callable[[str], Any] | None = None

    # ── construction ─────────────────────────────────────────────────────────

    @classmethod
    def restore(
        cls,
        eonlet_id: str,
        definition: Definition,
        store: EventStore,
        workspace: Path,
        memory_dir: Path,
        *,
        provider: LLMProvider | None = None,
        session_attached: bool = False,
    ) -> AgentRuntime:
        events = store.read()
        state = fold(events)
        gate = PermissionGate(
            mode=definition.config.permissions.mode,
            extra_deny=definition.config.permissions.extra_deny,
            session_attached=session_attached,
        )
        prov = provider or build_provider(definition.config.runtime.model)
        return cls(
            eonlet_id=eonlet_id,
            definition=definition,
            store=store,
            workspace=workspace,
            memory_dir=memory_dir,
            provider=prov,
            gate=gate,
            state=state,
        )

    # ── public API ───────────────────────────────────────────────────────────

    async def handle_user_message(self, content: str) -> AsyncIterator[Event]:
        """Process one user message. Yields each event as it's persisted."""
        ev = await self._record(user_message(content))
        yield ev
        async for out in self._run_until_end():
            yield out

    # ── internal ─────────────────────────────────────────────────────────────

    async def _run_until_end(self) -> AsyncIterator[Event]:
        cfg = self.definition.config
        max_steps = cfg.runtime.max_steps_per_run
        tool_specs = get_registry().schemas(cfg.tools.builtin)

        for _step in range(max_steps):
            messages = self._build_llm_messages()
            try:
                resp = await self._stream_one_turn(messages, tool_specs)
            except Exception as e:
                err = await self._record(
                    Event(kind=EventKind.ERROR, payload={"where": "llm", "error": str(e)})
                )
                yield err
                return

            ev = await self._record(
                assistant_message(
                    resp.content,
                    tool_calls=[
                        {"id": tc.id, "name": tc.name, "args": tc.arguments}
                        for tc in resp.tool_calls
                    ],
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    cost_usd=resp.cost_usd,
                )
            )
            yield ev

            if not resp.tool_calls:
                return

            # Execute each tool call serially. Per SPEC §6 — main loop processes
            # one trigger at a time; concurrent tool fan-out is a future tweak.
            for tc in resp.tool_calls:
                async for sub in self._execute_tool_call(tc):
                    yield sub

        log.warning("agent: hit max_steps_per_run=%d, ending run", max_steps)

    async def _stream_one_turn(
        self,
        messages: list[LLMMessage],
        tool_specs: list[dict[str, Any]],
    ) -> LLMResponse:
        """Drive ``LLMProvider.stream``. Forward text deltas via ``on_delta``;
        return the terminal ``LLMResponse``.

        Token deltas are intentionally *not* persisted to the event store —
        per SPEC §8.1 ``token_delta`` is a notification, not an event. The
        final ``assistant_message`` event still carries the full content.
        """
        final: LLMResponse | None = None
        async for chunk in self.provider.stream(
            messages,
            system=self._build_system_prompt(),
            tools=tool_specs,
            max_tokens=4096,
        ):
            # mypy can't narrow a TypedDict union by ``chunk["type"]`` alone,
            # so we explicitly cast within the branches.
            if chunk["type"] == "text":
                text: str = chunk["text"]
                if self.on_delta is not None and text:
                    res = self.on_delta(text)
                    if hasattr(res, "__await__"):
                        await res
            elif chunk["type"] == "done":
                final = chunk["response"]
        if final is None:
            raise RuntimeError("provider.stream ended without a DoneChunk")
        return final

    async def _execute_tool_call(self, call: LLMToolCall) -> AsyncIterator[Event]:
        registry = get_registry()
        ev_call = await self._record(tool_call(call.id, call.name, call.arguments))
        yield ev_call

        if not registry.has(call.name):
            ev_err = await self._record(
                tool_result(call.id, call.name, f"unknown tool: {call.name}", is_error=True)
            )
            yield ev_err
            return

        tool_instance = registry.get(call.name)
        # Validate input.
        try:
            args_model = tool_instance.input_schema.model_validate(call.arguments or {})
        except ValidationError as e:
            ev_err = await self._record(
                tool_result(call.id, call.name, f"invalid args: {e}", is_error=True)
            )
            yield ev_err
            return

        # Permission gate.
        decision = self.gate.evaluate(tool_instance, args_model)
        await self._record(
            Event(
                kind=EventKind.PERMISSION_GRANTED
                if decision.allowed
                else EventKind.PERMISSION_DENIED,
                payload={
                    "tool_name": call.name,
                    "call_id": call.id,
                    "rule": decision.rule,
                    "reason": decision.reason,
                },
            )
        )
        if not decision.allowed:
            ev_err = await self._record(
                tool_result(
                    call.id, call.name, f"permission denied: {decision.reason}", is_error=True
                )
            )
            yield ev_err
            return

        ctx = ToolContext(
            eonlet_id=self.eonlet_id,
            workspace=self.workspace,
            memory_dir=self.memory_dir,
            notes_files=self.definition.config.memory.notes_files,
            skills=self.definition.skills,
            env=dict(self.definition.config.env.defaults),
        )
        try:
            result: ToolResult = await tool_instance(args_model, ctx)
        except Exception as e:
            log.exception("tool %s raised", call.name)
            ev_err = await self._record(
                tool_result(call.id, call.name, f"tool raised: {e}", is_error=True)
            )
            yield ev_err
            return

        ev_res = await self._record(
            tool_result(call.id, call.name, result.content, is_error=result.is_error)
        )
        yield ev_res

    # ── message-list construction ────────────────────────────────────────────

    def _build_llm_messages(self) -> list[LLMMessage]:
        n = self.definition.config.memory.recent_messages_in_context
        sliced = recent(self.state, n)
        out: list[LLMMessage] = []
        for m in sliced:
            out.append(
                LLMMessage(
                    role=m.role,
                    content=m.content,
                    tool_calls=[
                        LLMToolCall(id=tc["id"], name=tc["name"], arguments=tc.get("args", {}))
                        for tc in m.tool_calls
                    ],
                    tool_call_id=m.tool_call_id,
                    is_error=m.is_error,
                )
            )
        return out

    def _build_system_prompt(self) -> str:
        parts = [self.definition.system_prompt.rstrip()]
        if self.definition.skills:
            lines = ["", "## Available Skills (call load_skill to load the body)"]
            for name, skill in sorted(self.definition.skills.items()):
                lines.append(f"- {name} — {skill.description}")
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    # ── event recording ──────────────────────────────────────────────────────

    async def _record(self, event: Event) -> Event:
        stored = self.store.append(event)
        # Update in-memory state for next iteration.
        from .state import reduce as _reduce

        self.state = _reduce(self.state, stored)
        if self.event_listener is not None:
            try:
                res = self.event_listener(stored)
                # Support both sync and async listeners.
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                log.exception("event listener raised; continuing")
        return stored
