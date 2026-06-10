"""LLM-compressed task checkpoint brief (ADR-0009 M2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anyio

from eonlet.llm.protocol import LLMMessage, LLMResponse, StreamChunk
from eonlet.runtime.events import assistant_message, tool_result, user_message
from eonlet.tasks.brief import (
    build_brief_prompt,
    build_decision_trace,
    build_task_brief,
    build_trace_prompt,
)


def _ev(maker: Any, *, eid: int) -> Any:
    return maker.model_copy(update={"id": eid, "task_id": "task-a"})


def test_build_brief_prompt_includes_goal_and_events() -> None:
    events = [
        _ev(user_message("framing"), eid=1),
        _ev(assistant_message("looked at config.py"), eid=2),
        _ev(tool_result("c1", "bash", "exit 0"), eid=3),
    ]
    prompt = build_brief_prompt("ship the parser", events)
    assert "Task goal: ship the parser" in prompt
    assert "looked at config.py" in prompt
    assert "tool_result: exit 0" in prompt


class _StubProvider:
    """Captures the brief request and returns a canned brief."""

    name = "stub"
    model = "stub"

    def __init__(self) -> None:
        self.last_system = ""
        self.last_prompt = ""

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        self.last_system = system
        self.last_prompt = messages[0].content
        return LLMResponse(
            content="Done: parsed config. Decided to skip legacy keys. Next: wire CLI.",
            tool_calls=[],
            stop_reason="end_turn",
        )

    async def stream(self, *a: Any, **k: Any) -> AsyncIterator[StreamChunk]:  # pragma: no cover
        raise NotImplementedError
        yield  # type: ignore[unreachable]


def test_build_task_brief_returns_provider_text() -> None:
    prov = _StubProvider()
    events = [_ev(user_message("go"), eid=1), _ev(assistant_message("working"), eid=2)]

    async def go() -> str:
        return await build_task_brief(prov, goal="ship it", events=events)  # type: ignore[arg-type]

    brief = anyio.run(go)
    assert "Decided to skip legacy keys" in brief
    assert "decisions" in prov.last_system.lower()  # brief-specific system prompt
    assert "ship it" in prov.last_prompt


def test_brief_prompt_is_cumulative_with_prior() -> None:
    # ADR-0009 M4: a prior brief is folded in so the new brief is cumulative.
    events = [_ev(assistant_message("new step"), eid=5)]
    prompt = build_brief_prompt("goal", events, prior_brief="already decided X")
    assert "Brief so far" in prompt
    assert "already decided X" in prompt
    assert "new step" in prompt


def test_build_trace_prompt_includes_parent_and_child() -> None:
    events = [_ev(assistant_message("decided to use Postgres"), eid=1)]
    prompt = build_trace_prompt("ship the feature", "design the schema", events)
    assert "Parent objective: ship the feature" in prompt
    assert "Subtask: design the schema" in prompt
    assert "decided to use Postgres" in prompt


def test_build_decision_trace_returns_provider_text() -> None:
    prov = _StubProvider()
    events = [_ev(assistant_message("chose snake_case"), eid=1)]

    async def go() -> str:
        return await build_decision_trace(
            prov,  # type: ignore[arg-type]
            parent_goal="ship it",
            child_goal="schema",
            events=events,
        )

    trace = anyio.run(go)
    assert trace  # provider text returned
    assert "handoff" in prov.last_system.lower()  # trace-specific system prompt
    assert "Subtask: schema" in prov.last_prompt


def test_structural_fallback_is_scope_aware() -> None:
    # ADR-0009 M2 fallback: the structural brief draws only on the task's own
    # assistant turns, never another scope's (chat or sibling task).
    from types import SimpleNamespace

    from eonlet.runtime.state import Message
    from eonlet.worker.main import _structural_checkpoint_summary

    runtime = SimpleNamespace(
        state=SimpleNamespace(
            messages=[
                Message(role="assistant", content="CHAT-NOISE", task_id=None),
                Message(role="assistant", content="TASK-A-PROGRESS", task_id="task-a"),
                Message(role="assistant", content="TASK-B-OTHER", task_id="task-b"),
            ]
        )
    )
    brief = _structural_checkpoint_summary(runtime, "task-a", "the goal")  # type: ignore[arg-type]
    assert "TASK-A-PROGRESS" in brief
    assert "CHAT-NOISE" not in brief
    assert "TASK-B-OTHER" not in brief
