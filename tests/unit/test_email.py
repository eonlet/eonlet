"""send_email composition + transport hook."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from eonlet.tools.builtin.email import (
    SendEmailArgs,
    SendEmailTool,
    build_message,
    set_transport,
)
from eonlet.tools.protocol import ToolContext


def test_build_message_basic() -> None:
    msg = build_message(
        subject="hi", body="hello world", from_addr="me@x", to="you@y", reply_to="r@y"
    )
    assert msg["From"] == "me@x"
    assert msg["To"] == "you@y"
    assert msg["Subject"] == "hi"
    assert msg["Reply-To"] == "r@y"
    assert "hello world" in msg.get_content()


def test_send_email_uses_transport_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    captured: dict = {}

    def fake(msg, env):
        captured["to"] = msg["To"]
        captured["subject"] = msg["Subject"]
        return {"sent": True, "message_id": "<id@x>"}

    prev = set_transport(fake)
    try:
        ctx = ToolContext(
            eonlet_id="t.x",
            workspace=tmp_path,
            memory_dir=tmp_path,
            notes_files=[],
            skills={},
            env={},
        )
        tool = SendEmailTool()
        result = anyio.run(tool.__call__, SendEmailArgs(subject="hi", body="x"), ctx)
        assert not result.is_error
        assert captured["to"] == "to@example.com"
        assert captured["subject"] == "hi"
    finally:
        set_transport(prev)


def test_send_email_errors_on_missing_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        notes_files=[],
        skills={},
        env={},
    )
    tool = SendEmailTool()
    result = anyio.run(tool.__call__, SendEmailArgs(subject="hi", body="x", to="you@x"), ctx)
    assert result.is_error
    assert "missing env vars" in result.content
