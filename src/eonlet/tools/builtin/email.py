"""send_email — SMTP via stdlib, env-driven config.

Required env vars (per TOOL_SPEC §6.11):
  SMTP_HOST, SMTP_USER, SMTP_PASSWORD, EMAIL_TO

Optional:
  SMTP_PORT (default 587, STARTTLS), REPLY_TO

The actual transport is factored into ``_send_via_smtp`` so tests can monkey-
patch it without standing up a real server.
"""

from __future__ import annotations

import os
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any

from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool


class SendEmailArgs(BaseModel):
    subject: str
    body: str = Field(description="Plain text or Markdown. Sent as text/plain.")
    to: str | None = Field(default=None, description="Recipient. Falls back to $EMAIL_TO.")
    reply_to: str | None = None


# Transport hook — overridable by tests.
SendFn = Callable[[EmailMessage, dict[str, str]], dict[str, Any]]


def _send_via_smtp(msg: EmailMessage, env: dict[str, str]) -> dict[str, Any]:
    host = env["SMTP_HOST"]
    port = int(env.get("SMTP_PORT") or 587)
    user = env["SMTP_USER"]
    password = env["SMTP_PASSWORD"]
    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=20) as s:
        s.ehlo()
        s.starttls(context=context)
        s.ehlo()
        s.login(user, password)
        s.send_message(msg)
    return {"sent": True, "message_id": msg["Message-ID"] or ""}


# Test hook.
_TRANSPORT: SendFn = _send_via_smtp


def set_transport(fn: SendFn) -> SendFn:
    """Replace the SMTP transport. Returns the previous transport for restoration."""
    global _TRANSPORT
    prev = _TRANSPORT
    _TRANSPORT = fn
    return prev


@tool
class SendEmailTool:
    name = "send_email"
    description = (
        "Send an email via SMTP. Uses env vars SMTP_HOST, SMTP_PORT, SMTP_USER, "
        "SMTP_PASSWORD, EMAIL_TO."
    )
    input_schema = SendEmailArgs
    annotations = ToolAnnotations(destructive=True, network=True, requires_confirmation=False)

    async def __call__(self, args: SendEmailArgs, ctx: ToolContext) -> ToolResult:
        env = {**os.environ, **ctx.env}
        missing = [v for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD") if not env.get(v)]
        if missing:
            return ToolResult(content=f"missing env vars: {missing}", is_error=True)
        to = args.to or env.get("EMAIL_TO")
        if not to:
            return ToolResult(content="no recipient: pass `to` or set EMAIL_TO", is_error=True)

        msg = build_message(
            subject=args.subject,
            body=args.body,
            from_addr=env["SMTP_USER"],
            to=to,
            reply_to=args.reply_to,
        )
        try:
            result = _TRANSPORT(msg, env)
        except Exception as e:
            return ToolResult(content=f"send failed: {e}", is_error=True)
        return ToolResult(content=f"sent to {to}", structured_output=result)


def build_message(
    *,
    subject: str,
    body: str,
    from_addr: str,
    to: str,
    reply_to: str | None = None,
) -> EmailMessage:
    """Compose a multi-line ``EmailMessage``. Pure function — easy to test."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    return msg
