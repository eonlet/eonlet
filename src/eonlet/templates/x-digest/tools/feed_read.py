"""RSS / Atom / JSON Feed reader — canonical "extend Eonlet" example.

This is **not** a built-in tool. It lives in the agent's ``tools/``
directory because RSS handling is a polling concern, not a fetch concern;
the runtime's ``web_fetch`` is the floor (HTML-only via trafilatura) and
anything beyond that — PDFs, feeds, headless rendering — is a custom-tool
or MCP-server concern. See ADR-0004 for the boundary.

Requires ``feedparser`` to be installed alongside Eonlet:

    pip install feedparser

The tool imports feedparser lazily so missing-dep errors surface as a
clear ToolResult message rather than a worker startup crash.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from eonlet.tools import ToolAnnotations, ToolContext, ToolResult, tool


class FeedReadArgs(BaseModel):
    url: str = Field(description="RSS, Atom, or JSON Feed URL.")
    limit: int = Field(default=20, ge=1, le=100, description="Max entries to return.")


@tool
class FeedReadTool:
    name = "feed_read"
    description = (
        "Read an RSS / Atom / JSON Feed URL and return its latest entries "
        "as [{title, url, summary, published_at}]. Custom per-template "
        "tool; not a built-in (see ADR-0004 for the runtime boundary)."
    )
    input_schema = FeedReadArgs
    annotations = ToolAnnotations(read_only=True, network=True)

    async def __call__(self, args: FeedReadArgs, ctx: ToolContext) -> ToolResult:
        del ctx  # this tool does its own fetch via feedparser; ctx unused
        try:
            import feedparser
        except ImportError:
            return ToolResult(
                content=(
                    "feed_read requires `feedparser`. Install with "
                    "`pip install feedparser` in the same environment "
                    "as eonlet."
                ),
                is_error=True,
            )

        parsed = feedparser.parse(args.url)
        if parsed.bozo and not parsed.entries:
            return ToolResult(
                content=f"feed_read: failed to parse {args.url!r} ({parsed.bozo_exception})",
                is_error=True,
            )

        entries: list[dict[str, Any]] = []
        for raw in parsed.entries[: args.limit]:
            entries.append(
                {
                    "title": raw.get("title", ""),
                    "url": raw.get("link", ""),
                    "summary": raw.get("summary", ""),
                    "published_at": _coerce_published(raw),
                }
            )

        body = "\n\n".join(
            f"{i + 1}. {e['title']}\n   {e['url']}\n   {e['summary'][:160]}"
            for i, e in enumerate(entries)
        )
        return ToolResult(
            content=body or "no entries",
            structured_output={"feed_url": args.url, "entries": entries},
        )


def _coerce_published(entry: Any) -> str | None:
    """Pull a parsed-time tuple off a feedparser entry and ISO-format it."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    try:
        dt = datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return dt.isoformat()
