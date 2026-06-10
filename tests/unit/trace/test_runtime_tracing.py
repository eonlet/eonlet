"""AgentRuntime ↔ ContextTracer wiring (ADR-0010).

Every request that reaches the provider must land in trace/context.jsonl
first; a context rewrite (here: the compaction watermark advancing) must
show up as a fork. Uses the same recorder-provider pattern as the memory
injection tests — no API key needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio

from eonlet.config import load_agent_config
from eonlet.llm.protocol import DoneChunk, LLMMessage, LLMResponse, StreamChunk, TextChunk
from eonlet.memory.watermark import write_watermark
from eonlet.runtime.agent import AgentRuntime
from eonlet.runtime.definition import Definition
from eonlet.runtime.store import EventStore
from eonlet.tools import builtin as _builtin  # noqa: F401  (register builtin tools)
from eonlet.trace import TRACE_FILENAME, read_trace


class _Recorder:
    name = "recorder"
    model = "recorder"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        return LLMResponse(content="ok", tool_calls=[], stop_reason="end_turn")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        yield TextChunk(type="text", text="ok")
        yield DoneChunk(
            type="done",
            response=LLMResponse(content="ok", tool_calls=[], stop_reason="end_turn"),
        )


def _write_definition(tmp_path: Path, *, trace_enabled: bool) -> Definition:
    defn_dir = tmp_path / "defn" / "assistant"
    defn_dir.mkdir(parents=True)
    (defn_dir / "system.md").write_text("you are a test agent")
    (defn_dir / "agent.yaml").write_text(
        "apiVersion: eonlet/v1\n"
        "kind: Agent\n"
        "metadata:\n"
        "  name: assistant\n"
        "  description: t\n"
        "  version: 0.0.1\n"
        "runtime:\n"
        "  model: fake-echo\n"
        "tools:\n"
        "  builtin: []\n"
        f"trace:\n  enabled: {str(trace_enabled).lower()}\n"
    )
    cfg = load_agent_config(defn_dir)
    return Definition(
        type="assistant",
        path=defn_dir,
        config=cfg,
        system_prompt="you are a test agent",
        custom_tool_paths=[],
        skills={},
    )


def _restore_runtime(tmp_path: Path, *, trace_enabled: bool) -> AgentRuntime:
    definition = _write_definition(tmp_path, trace_enabled=trace_enabled)
    eonlet_dir = tmp_path / "inst"
    workspace = eonlet_dir / "workspace"
    memory = eonlet_dir / "memory"
    workspace.mkdir(parents=True)
    memory.mkdir(parents=True)
    return AgentRuntime.restore(
        eonlet_id="t.x",
        definition=definition,
        store=EventStore(eonlet_dir / "state.db"),
        workspace=workspace,
        memory_dir=memory,
        provider=_Recorder(),  # type: ignore[arg-type]
    )


def test_restore_builds_tracer_when_enabled(tmp_path: Path) -> None:
    runtime = _restore_runtime(tmp_path, trace_enabled=True)
    assert runtime.tracer is not None
    # trace/ sits beside memory/ (DIRECTORY_LAYOUT §3).
    assert runtime.tracer.trace_dir == runtime.memory_dir.parent / "trace"


def test_restore_skips_tracer_by_default(tmp_path: Path) -> None:
    runtime = _restore_runtime(tmp_path, trace_enabled=False)
    assert runtime.tracer is None


def test_turns_trace_as_one_line_until_watermark_forks_it(tmp_path: Path) -> None:
    runtime = _restore_runtime(tmp_path, trace_enabled=True)
    assert runtime.tracer is not None
    trace_path = runtime.tracer.trace_dir / TRACE_FILENAME

    async def turn(text: str) -> None:
        async for _ in runtime.handle_user_message(text):
            pass

    # Two ordinary turns: the second request's window prefix-extends the first;
    # each request is followed by its reply (a response record).
    anyio.run(turn, "first message")
    anyio.run(turn, "second message")
    records = read_trace(trace_path)
    assert [r["kind"] for r in records] == ["root", "response", "delta", "response"]
    assert records[2]["line"] == records[0]["line"]
    assert records[2]["task_id"] is None
    assert records[2]["model"] == "recorder"
    # The reply is attached to its request, and its hash matches the same
    # message's hash in the next delta — what viewers dedupe by.
    assert records[1]["for_seq"] == records[0]["seq"]
    assert records[1]["message"]["content"] == "ok"
    assert records[1]["hash"] in records[2]["hashes"]

    # Compaction advances the watermark → the next window is rewritten → fork.
    write_watermark(runtime.memory_dir, runtime.store.latest_id())
    anyio.run(turn, "post-compaction message")
    records = read_trace(trace_path)
    assert records[-1]["kind"] == "response"  # the run's last reply is on file
    assert records[-2]["kind"] == "root"
    assert records[-2]["parent"] == {"line": records[0]["line"], "seq": records[3]["seq"]}


def test_trace_failure_never_breaks_the_run(tmp_path: Path) -> None:
    runtime = _restore_runtime(tmp_path, trace_enabled=True)

    class _Boom:
        def record(self, **_: Any) -> None:
            raise OSError("disk full")

        def record_response(self, *_: Any) -> None:
            raise OSError("disk full")

    runtime.tracer = _Boom()  # type: ignore[assignment]

    async def turn() -> list[Any]:
        return [ev async for ev in runtime.handle_user_message("hello")]

    events = anyio.run(turn)
    # The run completed: user message + assistant reply, no ERROR event.
    kinds = [e.kind.value for e in events]
    assert "user_message" in kinds and "assistant_message" in kinds
    assert "error" not in kinds
