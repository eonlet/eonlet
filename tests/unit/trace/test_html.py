"""HTML viewer rendering for context traces (ADR-0010).

The page must be self-contained (data embedded as JSON), and embedding must
be breakout-safe: record content is model/user text and may contain
``</script>`` — it must never terminate the data script element.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eonlet.llm.protocol import LLMMessage
from eonlet.trace import ContextTracer, read_trace, render_html


def _records(tmp_path: Path, *contents: str) -> list[dict]:
    t = ContextTracer(tmp_path / "trace")
    msgs: list[LLMMessage] = []
    for i, c in enumerate(contents):
        msgs = [*msgs, LLMMessage(role="user" if i % 2 == 0 else "assistant", content=c)]
        t.record(system="sys", messages=msgs, model="m")
    return read_trace(t.path)


def test_page_embeds_records_and_title(tmp_path: Path) -> None:
    records = _records(tmp_path, "hello there", "general kenobi")
    page = render_html(records, title="a.b · context trace")
    assert page.startswith("<!doctype html>")
    assert "a.b · context trace" in page
    assert "hello there" in page
    assert "general kenobi" in page
    # The line id of every record is in the payload.
    assert records[0]["line"] in page


def test_script_breakout_is_escaped(tmp_path: Path) -> None:
    evil = "before </script><script>alert(1)</script> after"
    records = _records(tmp_path, evil)
    page = render_html(records)
    # The raw close-tag sequence from record content must not survive; the
    # payload form uses the JSON-legal <\/ escape instead.
    assert "</script><script>alert(1)" not in page
    assert "<\\/script><script>alert(1)" in page
    # The page keeps exactly its own script element.
    assert page.count("</script>") == 1


def test_title_is_html_escaped() -> None:
    page = render_html([], title="<img src=x onerror=alert(1)>")
    assert "<img" not in page
    assert "&lt;img" in page


def test_page_embeds_trailing_reply(tmp_path: Path) -> None:
    # The run's final reply exists only as a response record — it must still
    # reach the page payload.
    t = ContextTracer(tmp_path / "trace")
    t.record(system="sys", messages=[LLMMessage(role="user", content="question")])
    t.record_response(LLMMessage(role="assistant", content="FINAL-REPLY-ONLY-IN-RESPONSE"))
    page = render_html(read_trace(t.path))
    assert "FINAL-REPLY-ONLY-IN-RESPONSE" in page
    assert '"kind": "response"' in page or '"kind":"response"' in page


def test_cmd_trace_writes_html_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eonlet.cli.commands import cmd_trace

    monkeypatch.setenv("EONLET_HOME", str(tmp_path))
    inst = tmp_path / "eonlets" / "assistant.demo"
    t = ContextTracer(inst / "trace")
    t.record(system="sys", messages=[LLMMessage(role="user", content="MARKER-CONTENT")])

    out = tmp_path / "viewer.html"
    cmd_trace("assistant.demo", html_path=str(out))
    page = out.read_text(encoding="utf-8")
    assert "MARKER-CONTENT" in page
    assert "assistant.demo · context trace" in page
