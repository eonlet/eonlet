"""Tier-2 orchestration: STM→LTM promotion (MEMORY_SPEC §4.4)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from eonlet.llm.protocol import DoneChunk, LLMMessage, LLMResponse, StreamChunk, TextChunk
from eonlet.memory.config import MemoryConfig
from eonlet.memory.ltm import LTMStore
from eonlet.memory.stm import STMSection, STMStore
from eonlet.memory.tier2 import build_tier2_prompt, parse_tier2_response, run_tier2


def _section(
    topic: str = "t",
    ts_start: str = "2026-05-22T14:00:00+00:00",
    ts_end: str = "2026-05-22T15:00:00+00:00",
    body: str = "something happened",
) -> STMSection:
    return STMSection(
        ts_start=ts_start,
        ts_end=ts_end,
        topic=topic,
        topics=["a"],
        body=body,
    )


# ── Stub provider ────────────────────────────────────────────────────────────


class _StaticProvider:
    """LLMProvider stub returning pre-canned JSON content."""

    name = "static"
    model = "static"

    def __init__(self, json_payload: dict[str, Any]) -> None:
        self._content = json.dumps(json_payload)

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        return LLMResponse(content=self._content, tool_calls=[], stop_reason="end_turn")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        yield TextChunk(type="text", text=self._content)
        yield DoneChunk(
            type="done",
            response=LLMResponse(content=self._content, tool_calls=[], stop_reason="end_turn"),
        )


class _FailingProvider:
    """LLMProvider stub that raises on complete()."""

    name = "fail"
    model = "fail"

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        raise RuntimeError("provider error")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        raise RuntimeError("provider error")
        # satisfy the generator return type
        yield TextChunk(type="text", text="")  # type: ignore[misc]


# ── Prompt assembly ────────────────────────────────────────────────────────


def test_prompt_lists_sections() -> None:
    secs = [_section(topic="alpha"), _section(topic="beta")]
    text = build_tier2_prompt(secs)
    assert "alpha" in text
    assert "beta" in text
    assert "something happened" in text


# ── Response parsing ───────────────────────────────────────────────────────


_GOOD_RESP = {
    "ltm_additions": [
        {"section": "fact", "content": "agent compacted portfolio notes"},
        {"section": "episodic", "content": "2026-05-22: portfolio rebalance discussion"},
    ],
    "stm_keep_section_headers": [
        "## [2026-05-22T14:00:00+00:00 – 2026-05-22T15:00:00+00:00] t"  # noqa: RUF001
    ],
}


def test_parse_accepts_good_response() -> None:
    secs = [_section()]
    resp = parse_tier2_response(json.dumps(_GOOD_RESP), secs)
    assert len(resp.ltm_additions) == 2
    assert resp.ltm_additions[0].section == "fact"
    assert len(resp.stm_keep_section_headers) == 1


def test_parse_accepts_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(_GOOD_RESP) + "\n```"
    resp = parse_tier2_response(fenced, [_section()])
    assert resp.ltm_additions[0].section == "fact"


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValueError, match="not JSON"):
        parse_tier2_response("nope", [])


def test_parse_rejects_unknown_section() -> None:
    bad = {
        "ltm_additions": [{"section": "unknown_cat", "content": "x"}],
        "stm_keep_section_headers": [],
    }
    with pytest.raises(ValueError, match="unknown LTM section"):
        parse_tier2_response(json.dumps(bad), [])


def test_parse_rejects_schema_violation() -> None:
    with pytest.raises(ValueError, match="schema"):
        parse_tier2_response('{"ltm_additions": "not-a-list"}', [])


# ── Orchestration ─────────────────────────────────────────────────────────


def _under_budget_cfg() -> MemoryConfig:
    return MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 64, "short_term_tokens": 99999}}
    )


def _over_budget_cfg() -> MemoryConfig:
    # short_term_tokens=64 (minimum) ensures a moderately sized STM is over budget.
    return MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 64, "short_term_tokens": 64}}
    )


def test_run_tier2_no_op_when_stm_empty(tmp_path: Path) -> None:
    cfg = _over_budget_cfg()
    provider = _StaticProvider(_GOOD_RESP)
    out = anyio.run(lambda: run_tier2(memory_dir=tmp_path, cfg=cfg, provider=provider))
    assert out.ran is False


def test_run_tier2_no_op_when_under_budget(tmp_path: Path) -> None:
    store = STMStore(tmp_path)
    anyio.run(lambda: store.append_sections([_section()]))
    cfg = _under_budget_cfg()
    provider = _StaticProvider(_GOOD_RESP)
    out = anyio.run(lambda: run_tier2(memory_dir=tmp_path, cfg=cfg, provider=provider))
    assert out.ran is False


def test_run_tier2_promotes_to_ltm(tmp_path: Path) -> None:
    stm_store = STMStore(tmp_path)
    # Write enough content to exceed the tiny short_term_tokens budget.
    body = "x" * 200
    anyio.run(lambda: stm_store.append_sections([_section(body=body)]))

    cfg = _over_budget_cfg()
    good_resp = {
        "ltm_additions": [{"section": "fact", "content": "portfolio stuff"}],
        "stm_keep_section_headers": [],
    }
    provider = _StaticProvider(good_resp)
    out = anyio.run(lambda: run_tier2(memory_dir=tmp_path, cfg=cfg, provider=provider))
    assert out.ran is True
    assert out.additions == 1
    assert out.kept_section_count == 0

    # LTM bullet written with src:implicit.
    ltm_bullets = LTMStore(tmp_path).read_bullets()
    assert len(ltm_bullets) == 1
    assert ltm_bullets[0].content == "portfolio stuff"
    assert ltm_bullets[0].src == "implicit"
    assert ltm_bullets[0].section == "fact"

    # STM replaced with kept sections (none kept in this run).
    stm_sections = anyio.run(STMStore(tmp_path).read)
    assert stm_sections == []


def test_run_tier2_keeps_sections_in_stm(tmp_path: Path) -> None:
    sec = _section(topic="recent", body="x" * 200)
    stm_store = STMStore(tmp_path)
    anyio.run(lambda: stm_store.append_sections([sec]))

    header = f"## [{sec.ts_start} – {sec.ts_end}] {sec.topic}"  # noqa: RUF001
    good_resp = {
        "ltm_additions": [],
        "stm_keep_section_headers": [header],
    }
    cfg = _over_budget_cfg()
    out = anyio.run(
        lambda: run_tier2(
            memory_dir=tmp_path,
            cfg=cfg,
            provider=_StaticProvider(good_resp),
        )
    )
    assert out.ran is True
    assert out.kept_section_count == 1
    # Section still in STM.
    assert anyio.run(STMStore(tmp_path).read) != []


def test_run_tier2_handles_llm_failure(tmp_path: Path) -> None:
    stm_store = STMStore(tmp_path)
    anyio.run(lambda: stm_store.append_sections([_section(body="x" * 200)]))
    cfg = _over_budget_cfg()
    out = anyio.run(lambda: run_tier2(memory_dir=tmp_path, cfg=cfg, provider=_FailingProvider()))
    assert out.ran is False
    assert out.error is not None
    # STM unchanged.
    assert anyio.run(STMStore(tmp_path).read) != []


def test_run_tier2_handles_parse_failure(tmp_path: Path) -> None:
    stm_store = STMStore(tmp_path)
    anyio.run(lambda: stm_store.append_sections([_section(body="x" * 200)]))
    cfg = _over_budget_cfg()

    class _BadJson:
        name = "bad"
        model = "bad"

        async def complete(
            self, messages: list[LLMMessage], *, system: str, **kw: Any
        ) -> LLMResponse:
            return LLMResponse(content="not json at all", tool_calls=[], stop_reason="end_turn")

        async def stream(
            self, messages: list[LLMMessage], *, system: str, **kw: Any
        ) -> AsyncIterator[StreamChunk]:
            raise NotImplementedError
            yield TextChunk(type="text", text="")  # type: ignore[misc]

    out = anyio.run(lambda: run_tier2(memory_dir=tmp_path, cfg=cfg, provider=_BadJson()))
    assert out.ran is False
    assert "parse" in (out.error or "").lower()
    # STM still intact.
    assert anyio.run(STMStore(tmp_path).read) != []


def test_run_tier2_emits_event(tmp_path: Path) -> None:
    from eonlet.runtime.events import Event, EventKind

    stm_store = STMStore(tmp_path)
    anyio.run(lambda: stm_store.append_sections([_section(body="x" * 200)]))

    good_resp = {
        "ltm_additions": [{"section": "fact", "content": "something durable"}],
        "stm_keep_section_headers": [],
    }
    cfg = _over_budget_cfg()
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    anyio.run(
        lambda: run_tier2(
            memory_dir=tmp_path,
            cfg=cfg,
            provider=_StaticProvider(good_resp),
            record_event=record,
        )
    )
    assert any(e.kind == EventKind.MEM_LTM_PROMOTED for e in captured)
