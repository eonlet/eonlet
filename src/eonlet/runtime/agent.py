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

from ..llm import LLMMessage, LLMProvider, LLMResponse, LLMToolCall, resolve_model
from ..memory.injection import build_memory_preamble, current_watermark
from ..memory.recall import RecallIndex
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
from .state import AgentState, fold
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
    # CronScheduler — set by the worker post-construction so the schedule tool
    # can mutate triggers from inside the loop. Typed loosely to avoid a
    # runtime import cycle (triggers → runtime → triggers).
    scheduler: Any | None = None
    # Recall index — owned by the runtime so every appended event is indexed
    # synchronously inside ``_record`` (M-I1 in MEMORY_SPEC §12). Set by the
    # worker; ``None`` skips indexing (used by tests that don't care about
    # recall).
    recall_index: RecallIndex | None = None
    # Session-scoped auto-compaction switch (MEMORY_SPEC §4.6). Init from
    # config; ``/compact off`` flips to False; reset on worker restart.
    auto_compact_enabled: bool = True
    # Memory preamble cached for the lifetime of one ``handle_user_message``
    # call — built once at run start and re-used across the loop's turns.
    _cached_preamble: str = ""
    # Run-state — used by session.start so re-attaching clients can tell whether
    # a run is currently in flight (e.g. "agent is mid-tool-call"). Set inside
    # ``handle_user_message`` only; reset in a finally.
    is_running: bool = False
    current_activity: str = ""  # human-readable hint: "thinking" / "tool: bash" / ""

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
        global_cfg: Any = None,
    ) -> AgentRuntime:
        events = store.read()
        state = fold(events)
        gate = PermissionGate(
            mode=definition.config.permissions.mode,
            extra_deny=definition.config.permissions.extra_deny,
            session_attached=session_attached,
        )
        prov = provider or resolve_model(definition.config.runtime.model, global_cfg)
        return cls(
            eonlet_id=eonlet_id,
            definition=definition,
            store=store,
            workspace=workspace,
            memory_dir=memory_dir,
            provider=prov,
            gate=gate,
            state=state,
            auto_compact_enabled=definition.config.memory.conversation.auto_compact,
        )

    # ── public API ───────────────────────────────────────────────────────────

    async def handle_user_message(self, content: str) -> AsyncIterator[Event]:
        """Process one user message. Yields each event as it's persisted."""
        self.is_running = True
        self.current_activity = "thinking"
        # Build the memory preamble once per run. Stores might mutate during
        # the run (e.g. agent adds a note mid-conversation), but recomputing
        # mid-turn would inflate token usage; we re-build next run.
        try:
            self._cached_preamble = await build_memory_preamble(
                self.memory_dir, self.definition.config.memory
            )
        except Exception:
            log.exception("memory preamble build failed; injecting nothing")
            self._cached_preamble = ""
        try:
            ev = await self._record(user_message(content))
            yield ev
            async for out in self._run_until_end():
                yield out
        finally:
            self.is_running = False
            self.current_activity = ""
            self._cached_preamble = ""

    # ── internal ─────────────────────────────────────────────────────────────

    async def _run_until_end(self) -> AsyncIterator[Event]:
        cfg = self.definition.config
        max_steps = cfg.runtime.max_steps_per_run
        tool_specs = get_registry().schemas(cfg.tools.builtin)
        # Loop-break guard: if the model issues the same failed tool-call
        # signature N times in a row (e.g. because its arguments were truncated
        # at max_tokens and it keeps retrying the same bad call), abort the run
        # rather than burning steps + budget.
        consecutive_bad_calls = 0
        last_bad_signature: tuple[str, str] | None = None

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
                    reasoning_content=resp.reasoning_content,
                )
            )
            yield ev

            if not resp.tool_calls:
                return

            # Execute each tool call serially. Per SPEC §6 — main loop processes
            # one trigger at a time; concurrent tool fan-out is a future tweak.
            step_had_arg_error = False
            for tc in resp.tool_calls:
                self.current_activity = f"tool: {tc.name}"
                async for sub in self._execute_tool_call(tc):
                    # Track invalid-args failures (signature = name + serialized
                    # args) so we can break out of a streaming-truncation loop.
                    # ``tool_result(..., is_error=True)`` emits a TOOL_ERROR
                    # event (not TOOL_RESULT) — see events.tool_result.
                    if sub.kind == EventKind.TOOL_ERROR and "invalid args" in (
                        sub.payload.get("output") or ""
                    ):
                        step_had_arg_error = True
                    yield sub
            self.current_activity = "thinking"

            if step_had_arg_error:
                # Build a signature from the tool calls we just tried; if the
                # model retries the exact same broken signature again next step,
                # we'll abort.
                sig = "|".join(
                    f"{tc.name}:{sorted((tc.arguments or {}).keys())}" for tc in resp.tool_calls
                )
                cur: tuple[str, str] = ("args", sig)
                if cur == last_bad_signature:
                    consecutive_bad_calls += 1
                else:
                    consecutive_bad_calls = 1
                    last_bad_signature = cur
                if consecutive_bad_calls >= 3:
                    log.warning(
                        "agent: aborting run — model repeated invalid-args "
                        "tool calls %d times in a row (likely max_tokens truncation)",
                        consecutive_bad_calls,
                    )
                    err = await self._record(
                        Event(
                            kind=EventKind.ERROR,
                            payload={
                                "where": "agent_loop",
                                "error": (
                                    "aborted: model issued invalid tool arguments "
                                    f"{consecutive_bad_calls} steps in a row "
                                    "(arguments may have been truncated at max_tokens). "
                                    "Try a smaller request or raise runtime.max_tokens_per_response."
                                ),
                            },
                        )
                    )
                    yield err
                    return
            else:
                consecutive_bad_calls = 0
                last_bad_signature = None

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
            max_tokens=self.definition.config.runtime.max_tokens_per_response,
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
        # The OpenAI provider stuffs unparseable streamed JSON under "_raw" —
        # surface a clear hint to the model rather than letting pydantic complain
        # about every missing field, since the real root cause is truncation.
        if isinstance(call.arguments, dict) and set(call.arguments.keys()) == {"_raw"}:
            raw_preview = str(call.arguments.get("_raw") or "")[:200]
            ev_err = await self._record(
                tool_result(
                    call.id,
                    call.name,
                    (
                        "invalid args: tool-call JSON arguments could not be parsed "
                        "(likely truncated mid-stream). Please reissue the tool call "
                        f"with the COMPLETE JSON object. Received partial: {raw_preview!r}"
                    ),
                    is_error=True,
                )
            )
            yield ev_err
            return
        # Validate input.
        try:
            args_model = tool_instance.input_schema.model_validate(call.arguments or {})
        except ValidationError as e:
            # Compact the pydantic error to its first 3 issues to keep the
            # feedback to the model short and actionable.
            issues = e.errors()[:3]
            summary = "; ".join(
                f"{'.'.join(str(p) for p in iss['loc']) or '<root>'}: {iss['msg']}"
                for iss in issues
            )
            ev_err = await self._record(
                tool_result(
                    call.id,
                    call.name,
                    f"invalid args: {summary}",
                    is_error=True,
                )
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

        extra: dict[str, Any] = {"runtime": self}
        if self.recall_index is not None:
            extra["recall_index"] = self.recall_index
        ctx = ToolContext(
            eonlet_id=self.eonlet_id,
            workspace=self.workspace,
            memory_dir=self.memory_dir,
            skills=self.definition.skills,
            env=dict(self.definition.config.env.defaults),
            scheduler=self.scheduler,
            record_event=self._record,
            extra=extra,
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
        """Slice the recent-messages window per MEMORY_SPEC §3.2.

        Walks back from the newest message, skipping anything already
        represented by short-term memory (``event_id <= watermark``), until
        ``working_memory_tokens`` is hit or the floor is reached.
        """
        from ..memory.tokens import estimate_message

        cfg = self.definition.config.memory
        watermark = current_watermark(self.memory_dir) if cfg.enabled else 0
        budget = cfg.conversation.working_memory_tokens
        min_keep = cfg.conversation.keep_recent_messages_min

        # Filter messages older than the watermark first — they are STM now.
        eligible = [m for m in self.state.messages if m.event_id is None or m.event_id > watermark]

        selected: list[Any] = []
        total = 0
        for msg in reversed(eligible):
            cost = estimate_message(msg.role, msg.content, tool_calls=len(msg.tool_calls))
            if len(selected) >= min_keep and total + cost > budget and selected:
                break
            selected.append(msg)
            total += cost
            if len(selected) >= 1000:
                break
        selected.reverse()

        # Boundary safety: never start the window with an orphan tool result.
        while selected and selected[0].role == "tool":
            selected.pop(0)

        out: list[LLMMessage] = []
        for m in selected:
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
                    reasoning_content=m.reasoning_content,
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
        if self._cached_preamble:
            parts.append(self._cached_preamble)
        return "\n\n".join(parts)

    # ── event recording ──────────────────────────────────────────────────────

    async def _record(self, event: Event) -> Event:
        stored = self.store.append(event)
        # Update in-memory state for next iteration.
        from .state import reduce as _reduce

        self.state = _reduce(self.state, stored)
        if self.recall_index is not None:
            try:
                self.recall_index.index_event(stored)
            except Exception:
                log.exception("recall index: failed to index event %s; continuing", stored.id)
        if self.event_listener is not None:
            try:
                res = self.event_listener(stored)
                # Support both sync and async listeners.
                if hasattr(res, "__await__"):
                    await res
            except Exception:
                log.exception("event listener raised; continuing")
        return stored
