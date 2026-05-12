"""Tier-3 orchestration: LTM forgetting (MEMORY_SPEC §4.5)."""

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
from eonlet.memory.tier3 import build_tier3_prompt, parse_tier3_response, run_tier3

# ── Stub provider ────────────────────────────────────────────────────────────


class _StaticProvider:
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
        raise RuntimeError("tier-3 provider error")

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system: str,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamChunk]:
        raise RuntimeError("tier-3 provider error")
        yield TextChunk(type="text", text="")  # type: ignore[misc]


# ── Helpers ──────────────────────────────────────────────────────────────────


def _seed_ltm(store: LTMStore, n: int = 5) -> None:
    for i in range(n):
        anyio.run(
            lambda i=i: store.append_bullet(
                "fact", f"fact number {i} " + "x" * 50, "implicit", "2026-05-22"
            )
        )


def _over_budget_cfg() -> MemoryConfig:
    # long_term_tokens=64 (minimum) ensures a moderately sized LTM is over budget.
    return MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 64, "long_term_tokens": 64}}
    )


def _under_budget_cfg() -> MemoryConfig:
    return MemoryConfig.model_validate(
        {"conversation": {"working_memory_tokens": 64, "long_term_tokens": 99999}}
    )


# ── Prompt assembly ────────────────────────────────────────────────────────


def test_prompt_includes_ltm_text() -> None:
    ltm = "# Long-term memory\n\n## fact\n- something [src:implicit, ts:2026-05-22]\n"
    prompt = build_tier3_prompt(ltm)
    assert "something" in prompt
    assert "long-term" in prompt.lower()


# ── Response parsing ───────────────────────────────────────────────────────


_GOOD_RESP: dict[str, Any] = {
    "kept_bullets": [
        {"section": "fact", "content": "kept fact", "src": "implicit", "ts": "2026-05-22"}
    ],
    "dropped_bullets": [{"section": "fact", "preview": "dropped fact", "reason": "stale"}],
}


def test_parse_accepts_good_response() -> None:
    resp = parse_tier3_response(json.dumps(_GOOD_RESP))
    assert len(resp.kept_bullets) == 1
    assert resp.kept_bullets[0].content == "kept fact"
    assert len(resp.dropped_bullets) == 1


def test_parse_accepts_fenced_json() -> None:
    fenced = "```json\n" + json.dumps(_GOOD_RESP) + "\n```"
    resp = parse_tier3_response(fenced)
    assert len(resp.kept_bullets) == 1


def test_parse_rejects_non_json() -> None:
    with pytest.raises(ValueError, match="not JSON"):
        parse_tier3_response("not json")


def test_parse_rejects_schema_violation() -> None:
    with pytest.raises(ValueError, match="schema"):
        parse_tier3_response('{"kept_bullets": "wrong"}')


# ── Orchestration ─────────────────────────────────────────────────────────


def test_run_tier3_no_op_when_ltm_empty(tmp_path: Path) -> None:
    cfg = _over_budget_cfg()
    out = anyio.run(
        lambda: run_tier3(memory_dir=tmp_path, cfg=cfg, provider=_StaticProvider(_GOOD_RESP))
    )
    assert out.ran is False


def test_run_tier3_no_op_when_under_budget(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    anyio.run(lambda: store.append_bullet("fact", "something", "implicit", "2026-05-22"))
    cfg = _under_budget_cfg()
    out = anyio.run(
        lambda: run_tier3(memory_dir=tmp_path, cfg=cfg, provider=_StaticProvider(_GOOD_RESP))
    )
    assert out.ran is False


def test_run_tier3_rewrites_ltm(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    _seed_ltm(store, n=5)

    good_resp: dict[str, Any] = {
        "kept_bullets": [
            {
                "section": "fact",
                "content": "consolidated fact",
                "src": "implicit",
                "ts": "2026-05-22",
            }
        ],
        "dropped_bullets": [{"section": "fact", "preview": "dropped", "reason": "stale"}],
    }
    cfg = _over_budget_cfg()
    out = anyio.run(
        lambda: run_tier3(memory_dir=tmp_path, cfg=cfg, provider=_StaticProvider(good_resp))
    )
    assert out.ran is True
    assert out.kept_count == 1
    assert out.dropped_count == 1

    # LTM rewritten with only the kept bullet.
    remaining = LTMStore(tmp_path).read_bullets()
    assert len(remaining) == 1
    assert remaining[0].content == "consolidated fact"


def test_run_tier3_handles_llm_failure(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    _seed_ltm(store, n=3)
    cfg = _over_budget_cfg()
    original_text = store.read_raw()

    out = anyio.run(lambda: run_tier3(memory_dir=tmp_path, cfg=cfg, provider=_FailingProvider()))
    assert out.ran is False
    assert out.error is not None
    # LTM unchanged.
    assert store.read_raw() == original_text


def test_run_tier3_handles_parse_failure(tmp_path: Path) -> None:
    store = LTMStore(tmp_path)
    _seed_ltm(store, n=3)
    cfg = _over_budget_cfg()
    original_text = store.read_raw()

    class _BadJson:
        name = "bad"
        model = "bad"

        async def complete(
            self, messages: list[LLMMessage], *, system: str, **kw: Any
        ) -> LLMResponse:
            return LLMResponse(content="not-json", tool_calls=[], stop_reason="end_turn")

        async def stream(
            self, messages: list[LLMMessage], *, system: str, **kw: Any
        ) -> AsyncIterator[StreamChunk]:
            raise NotImplementedError
            yield TextChunk(type="text", text="")  # type: ignore[misc]

    out = anyio.run(lambda: run_tier3(memory_dir=tmp_path, cfg=cfg, provider=_BadJson()))
    assert out.ran is False
    assert store.read_raw() == original_text


def test_run_tier3_emits_event(tmp_path: Path) -> None:
    from eonlet.runtime.events import Event, EventKind

    store = LTMStore(tmp_path)
    _seed_ltm(store, n=3)
    cfg = _over_budget_cfg()
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    anyio.run(
        lambda: run_tier3(
            memory_dir=tmp_path,
            cfg=cfg,
            provider=_StaticProvider(_GOOD_RESP),
            record_event=record,
        )
    )
    evts = [e for e in captured if e.kind == EventKind.MEM_LTM_FORGOTTEN]
    assert len(evts) == 1
    assert evts[0].payload["cause"] == "tier3"
    assert evts[0].payload["kept_count"] == 1
    assert evts[0].payload["dropped_count"] == 1


def test_run_tier3_dropped_digest_in_event(tmp_path: Path) -> None:
    """M-I7: dropped content digest is in the event even after LTM rewrite."""
    from eonlet.runtime.events import Event, EventKind

    store = LTMStore(tmp_path)
    _seed_ltm(store, n=5)
    cfg = _over_budget_cfg()

    good_resp: dict[str, Any] = {
        "kept_bullets": [],
        "dropped_bullets": [{"section": "fact", "preview": "DROPPED-PREVIEW", "reason": "stale"}],
    }
    captured: list[Event] = []

    async def record(ev: Event) -> Event:
        captured.append(ev)
        return ev

    anyio.run(
        lambda: run_tier3(
            memory_dir=tmp_path,
            cfg=cfg,
            provider=_StaticProvider(good_resp),
            record_event=record,
        )
    )
    evts = [e for e in captured if e.kind == EventKind.MEM_LTM_FORGOTTEN]
    digest = evts[0].payload["dropped_digest"]
    assert any("DROPPED-PREVIEW" in d["preview"] for d in digest)
