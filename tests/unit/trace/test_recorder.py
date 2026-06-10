"""ContextTracer lineage semantics (ADR-0010).

Prefix-extension stays on one line (delta records); any prefix rewrite —
compaction, window slide, scope switch — forks a new line whose root points
at where the old line left off. Restart restores the cursor from the file.
"""

from __future__ import annotations

import json
from pathlib import Path

from eonlet.llm.protocol import LLMMessage, LLMToolCall
from eonlet.trace import TRACE_FILENAME, ContextTracer, fold_line, read_trace


def _msgs(*contents: str) -> list[LLMMessage]:
    roles = ["user", "assistant"]
    return [LLMMessage(role=roles[i % 2], content=c) for i, c in enumerate(contents)]


def _tracer(tmp_path: Path) -> ContextTracer:
    return ContextTracer(tmp_path / "trace")


# ── lineage ──────────────────────────────────────────────────────────────────


def test_first_record_is_root_without_parent(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    rec = t.record(system="sys", messages=_msgs("hi"), model="m")
    assert rec["kind"] == "root"
    assert rec["parent"] is None
    assert rec["seq"] == 1
    assert rec["n_messages"] == 1
    assert rec["system"] == "sys"
    assert [m["content"] for m in rec["messages"]] == ["hi"]
    assert (tmp_path / "trace" / TRACE_FILENAME).exists()


def test_prefix_extension_stays_on_line_and_stores_delta(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("hi"))
    r2 = t.record(system="sys", messages=_msgs("hi", "yo", "more"))
    assert r2["kind"] == "delta"
    assert r2["line"] == r1["line"]
    assert r2["parent"] is None
    assert r2["n_messages"] == 3
    # Only the appended suffix is stored.
    assert [m["content"] for m in r2["messages"]] == ["yo", "more"]


def test_system_prompt_stored_only_when_changed(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    t.record(system="sys-A", messages=_msgs("hi"))
    r2 = t.record(system="sys-A", messages=_msgs("hi", "yo"))
    r3 = t.record(system="sys-B", messages=_msgs("hi", "yo", "x"))
    assert r2["system"] is None  # unchanged → hash only
    assert r3["system"] == "sys-B"  # changed → full text, same line
    assert r3["kind"] == "delta"


def test_system_change_alone_does_not_fork(tmp_path: Path) -> None:
    # The system prompt is rebuilt every turn by design (<task_progress>
    # mutates mid-run) — it must not be part of lineage.
    t = _tracer(tmp_path)
    r1 = t.record(system="sys-A", messages=_msgs("hi"))
    r2 = t.record(system="sys-B", messages=_msgs("hi", "yo"))
    assert r2["line"] == r1["line"]


def test_rewrite_forks_a_new_line(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("hi", "yo"))
    r2 = t.record(system="sys", messages=_msgs("hi", "yo", "more"))
    # Compaction: history rewritten to a summary + the live tail.
    r3 = t.record(system="sys", messages=_msgs("[summary of earlier]", "next"))
    assert r3["kind"] == "root"
    assert r3["line"] != r1["line"]
    assert r3["parent"] == {"line": r2["line"], "seq": r2["seq"]}
    # Root snapshots are full.
    assert [m["content"] for m in r3["messages"]] == ["[summary of earlier]", "next"]


def test_shorter_context_forks(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("a", "b", "c"))
    r2 = t.record(system="sys", messages=_msgs("a"))  # window slid back
    assert r2["kind"] == "root"
    assert r2["parent"] == {"line": r1["line"], "seq": 1}


def test_identical_context_is_an_empty_delta(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("hi"))
    r2 = t.record(system="sys", messages=_msgs("hi"))
    assert r2["kind"] == "delta"
    assert r2["line"] == r1["line"]
    assert r2["messages"] == []


def test_tool_calls_participate_in_fingerprint(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    with_call = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[LLMToolCall(id="c1", name="bash", arguments={"cmd": "ls"})],
    )
    t.record(system="sys", messages=[with_call])
    changed = LLMMessage(
        role="assistant",
        content="",
        tool_calls=[LLMToolCall(id="c1", name="bash", arguments={"cmd": "rm"})],
    )
    r2 = t.record(system="sys", messages=[changed])
    assert r2["kind"] == "root"  # same shape, different arguments → rewrite


# ── responses ────────────────────────────────────────────────────────────────


def test_record_response_appends_on_current_line(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    req = t.record(system="sys", messages=_msgs("hi"))
    rec = t.record_response(LLMMessage(role="assistant", content="hello!"))
    assert rec is not None
    assert rec["kind"] == "response"
    assert rec["line"] == req["line"]
    assert rec["for_seq"] == req["seq"]
    assert rec["seq"] == req["seq"] + 1
    assert rec["message"]["content"] == "hello!"
    assert rec["hash"]


def test_record_response_without_request_is_a_noop(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    assert t.record_response(LLMMessage(role="assistant", content="orphan")) is None
    assert not (tmp_path / "trace" / TRACE_FILENAME).exists()


def test_response_does_not_affect_lineage(tmp_path: Path) -> None:
    # The reply reappears verbatim in the next request — the next record must
    # still be a delta on the same line, never a fork caused by the response.
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("hi"))
    t.record_response(LLMMessage(role="assistant", content="yo"))
    r2 = t.record(system="sys", messages=_msgs("hi", "yo"))
    assert r2["kind"] == "delta"
    assert r2["line"] == r1["line"]
    assert [m["content"] for m in r2["messages"]] == ["yo"]


def test_response_hash_matches_next_delta_hash(tmp_path: Path) -> None:
    # Viewers dedupe the reply out of the following delta by this equality.
    t = _tracer(tmp_path)
    t.record(system="sys", messages=_msgs("hi"))
    resp = t.record_response(LLMMessage(role="assistant", content="yo"))
    r2 = t.record(system="sys", messages=_msgs("hi", "yo"))
    assert resp is not None
    assert r2["hashes"][0] == resp["hash"]


def test_fold_line_excludes_responses_from_context(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys", messages=_msgs("hi"))
    t.record_response(LLMMessage(role="assistant", content="tail reply"))
    folded = fold_line(read_trace(t.path), r1["line"])
    assert [m["content"] for m in folded["messages"]] == ["hi"]
    assert folded["records"][-1]["kind"] == "response"


def test_restart_after_trailing_response_keeps_cursor(tmp_path: Path) -> None:
    t1 = _tracer(tmp_path)
    r1 = t1.record(system="sys", messages=_msgs("hi"))
    t1.record_response(LLMMessage(role="assistant", content="yo"))
    t2 = _tracer(tmp_path)  # restore must anchor on the last *request*
    r2 = t2.record(system="sys", messages=_msgs("hi", "yo"))
    assert r2["kind"] == "delta"
    assert r2["line"] == r1["line"]
    assert r2["seq"] == 3
    assert r2["system"] is None  # system unchanged — hash restored correctly


# ── restart restore ──────────────────────────────────────────────────────────


def test_restart_continues_the_line(tmp_path: Path) -> None:
    t1 = _tracer(tmp_path)
    r1 = t1.record(system="sys", messages=_msgs("hi"))
    t2 = _tracer(tmp_path)  # fresh process, same file
    r2 = t2.record(system="sys", messages=_msgs("hi", "yo"))
    assert r2["kind"] == "delta"
    assert r2["line"] == r1["line"]
    assert r2["seq"] == 2


def test_restart_forks_when_context_differs(tmp_path: Path) -> None:
    t1 = _tracer(tmp_path)
    r1 = t1.record(system="sys", messages=_msgs("hi"))
    t2 = _tracer(tmp_path)
    r2 = t2.record(system="sys", messages=_msgs("rebuilt", "differently"))
    assert r2["kind"] == "root"
    assert r2["parent"] == {"line": r1["line"], "seq": 1}


def test_corrupt_tail_is_skipped(tmp_path: Path) -> None:
    t1 = _tracer(tmp_path)
    t1.record(system="sys", messages=_msgs("hi"))
    path = tmp_path / "trace" / TRACE_FILENAME
    with path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "line": "ln-x", "kind": "ro')  # torn mid-append
    records = read_trace(path)
    assert len(records) == 1
    t2 = _tracer(tmp_path)
    r = t2.record(system="sys", messages=_msgs("hi", "yo"))
    assert r["kind"] == "delta" and r["seq"] == 2


# ── reading / folding ────────────────────────────────────────────────────────


def test_fold_line_reconstructs_full_context(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    r1 = t.record(system="sys-A", messages=_msgs("a"))
    t.record(system="sys-B", messages=_msgs("a", "b", "c"))
    t.record(system="sys", messages=_msgs("compacted"))  # forks away
    records = read_trace(t.path)
    folded = fold_line(records, r1["line"])
    assert [m["content"] for m in folded["messages"]] == ["a", "b", "c"]
    assert folded["system"] == "sys-B"  # last full text seen on the line
    assert len(folded["records"]) == 2


def test_records_are_one_json_object_per_line(tmp_path: Path) -> None:
    t = _tracer(tmp_path)
    t.record(system="sys", messages=_msgs("hi"))
    t.record(system="sys", messages=_msgs("hi", "yo"))
    lines = (tmp_path / "trace" / TRACE_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for ln in lines:
        json.loads(ln)
