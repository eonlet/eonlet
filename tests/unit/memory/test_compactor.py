"""Compactor JSON contract + boundary safety (MEMORY_SPEC §4.1)."""

from __future__ import annotations

import anyio
import pytest

from eonlet.memory.compactor import (
    LLMCompactor,
    _render_event_for_prompt,
    _ts_of,
    build_compaction_prompt,
    parse_compaction_response,
)
from eonlet.runtime.events import (
    Event,
    EventKind,
    assistant_message,
    tool_call,
    tool_result,
    user_message,
)


def _evt(ev, id_, ts=1_000_000):  # type: ignore[no-untyped-def]
    return ev.model_copy(update={"id": id_, "ts": ts})


# ── prompt assembly ────────────────────────────────────────────────────────


def test_prompt_lists_events_and_suggested_boundary() -> None:
    events = [_evt(user_message("hello"), 1), _evt(assistant_message("hi back"), 2)]
    text = build_compaction_prompt(events, suggested_boundary=2)
    assert "Suggested boundary_event_id: 2" in text
    assert "#1" in text and "#2" in text
    assert "hello" in text and "hi back" in text


# ── response parsing ───────────────────────────────────────────────────────


_GOOD_JSON = """
{
  "sections": [
    {
      "ts_start": "2026-05-22T14:00:00+00:00",
      "ts_end": "2026-05-22T15:00:00+00:00",
      "topic": "alpha",
      "topics": ["a", "b"],
      "body": "what happened"
    }
  ],
  "boundary_event_id": 5
}
"""


def test_parse_accepts_bare_json() -> None:
    out = parse_compaction_response(
        _GOOD_JSON, valid_event_ids={1, 2, 3, 4, 5}, suggested_boundary=5
    )
    assert out.boundary_event_id == 5
    assert len(out.sections) == 1
    assert out.sections[0].topic == "alpha"


def test_parse_accepts_fenced_json() -> None:
    fenced = "Here is the result:\n```json\n" + _GOOD_JSON + "\n```\n"
    out = parse_compaction_response(fenced, valid_event_ids={5}, suggested_boundary=5)
    assert out.boundary_event_id == 5


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValueError, match="not JSON"):
        parse_compaction_response("nope", valid_event_ids={1}, suggested_boundary=1)


def test_parse_rejects_boundary_past_suggested() -> None:
    j = _GOOD_JSON.replace('"boundary_event_id": 5', '"boundary_event_id": 7')
    with pytest.raises(ValueError, match="exceeds suggested"):
        parse_compaction_response(j, valid_event_ids={1, 2, 3, 4, 5, 7}, suggested_boundary=5)


def test_parse_rejects_unknown_boundary() -> None:
    with pytest.raises(ValueError, match="not a known event id"):
        parse_compaction_response(_GOOD_JSON, valid_event_ids={1, 2, 3}, suggested_boundary=5)


def test_parse_rejects_zero_sections() -> None:
    j = '{"sections": [], "boundary_event_id": 5}'
    with pytest.raises(ValueError, match="zero sections"):
        parse_compaction_response(j, valid_event_ids={5}, suggested_boundary=5)


def test_parse_rejects_schema_violation() -> None:
    j = '{"sections": [{"topic": "x"}], "boundary_event_id": 5}'  # missing fields
    with pytest.raises(ValueError, match="schema"):
        parse_compaction_response(j, valid_event_ids={5}, suggested_boundary=5)


# ── _ts_of ────────────────────────────────────────────────────────────────


def test_ts_of_none_returns_empty() -> None:
    ev = user_message("hi").model_copy(update={"ts": None})
    assert _ts_of(ev) == ""


def test_ts_of_with_timestamp() -> None:
    ev = user_message("hi").model_copy(update={"ts": 1_000_000_000_000})
    result = _ts_of(ev)
    assert "T" in result  # ISO format


# ── _render_event_for_prompt ──────────────────────────────────────────────


def test_render_assistant_with_tool_calls() -> None:
    ev = _evt(
        assistant_message(
            "calling tool",
            tool_calls=[{"id": "c1", "name": "bash", "args": {}}],
        ),
        1,
    )
    text = _render_event_for_prompt(ev)
    assert "calls: bash" in text
    assert "assistant:" in text


def test_render_tool_result_event() -> None:
    ev = _evt(tool_result("c1", "bash", "ok output"), 2)
    text = _render_event_for_prompt(ev)
    assert "tool_result" in text
    assert "ok output" in text


def test_render_tool_error_event() -> None:
    ev = _evt(tool_result("c1", "bash", "error msg", is_error=True), 3)
    text = _render_event_for_prompt(ev)
    assert "tool_error" in text


def test_render_tool_call_event() -> None:
    ev = _evt(tool_call("c1", "file_read", {"path": "test.py"}), 4)
    text = _render_event_for_prompt(ev)
    assert "tool_call" in text
    assert "file_read" in text


def test_render_fallback_for_unknown_kind() -> None:
    ev = Event(kind=EventKind.SESSION_STARTED, payload={}).model_copy(
        update={"id": 5, "ts": 1_000_000}
    )
    text = _render_event_for_prompt(ev)
    assert "session_started" in text.lower() or "SESSION_STARTED" in text


# ── LLMCompactor ──────────────────────────────────────────────────────────


def test_llm_compactor_uses_provider() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from eonlet.llm.protocol import LLMResponse

    good_response = """{
  "sections": [
    {
      "ts_start": "2026-05-22T14:00:00+00:00",
      "ts_end": "2026-05-22T15:00:00+00:00",
      "topic": "summary",
      "topics": ["work"],
      "body": "user asked hello"
    }
  ],
  "boundary_event_id": 1
}"""

    mock_provider = MagicMock()
    mock_provider.complete = AsyncMock(
        return_value=LLMResponse(
            content=good_response,
            tool_calls=[],
            tokens_in=10,
            tokens_out=50,
        )
    )

    compactor = LLMCompactor(mock_provider)
    events = [
        _evt(user_message("hello"), 1),
    ]
    result = anyio.run(compactor.summarize, events, 1)
    assert result.boundary_event_id == 1
    assert len(result.sections) == 1
