"""Content extraction — HTML → markdown via trafilatura; text/JSON passthrough.

ADR-0004 §"`web_fetch`": only two extraction paths ship in the runtime,
HTML and plain-text/JSON. Everything else (PDF, RSS, JS-rendered pages)
is an extensibility concern: custom tools today, MCP servers in v0.2+.
"""

from __future__ import annotations

import json
from typing import Any

import trafilatura
from pydantic import BaseModel, Field

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


class ExtractedContent(BaseModel):
    """Result of one extraction call."""

    title: str | None = None
    content_markdown: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


def _decode_bytes(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def _gather_metadata(doc: Any) -> dict[str, Any]:
    fields = ("author", "date", "language", "sitename", "hostname", "description")
    out: dict[str, Any] = {}
    for f in fields:
        v = getattr(doc, f, None)
        if v:
            out[f] = v
    categories = getattr(doc, "categories", None)
    if categories:
        out["categories"] = list(categories) if not isinstance(categories, list) else categories
    tags = getattr(doc, "tags", None)
    if tags:
        out["tags"] = list(tags) if not isinstance(tags, list) else tags
    return out


def extract_html(raw: bytes, *, url: str) -> ExtractedContent:
    """Convert ``raw`` HTML bytes to markdown via trafilatura.

    Returns an empty body with a ``no_main_content`` warning when
    trafilatura can't locate primary content (typical for SPA shells).
    Never raises on extractor failure — the caller surfaces issues via
    ``ToolResult.structured_output``.

    Implementation note: trafilatura's public API doesn't expose a
    single call that yields *both* a markdown body and a structured
    metadata object, so we parse the HTML once via :func:`load_html`
    and feed the resulting lxml tree to both ``extract`` (for the
    markdown body) and ``bare_extraction`` (for title/author/date/etc.).
    This avoids the second HTML→tree parse the naive two-call version
    triggers.
    """
    html = _decode_bytes(raw)
    tree = trafilatura.load_html(html)

    if tree is None:
        return ExtractedContent(
            title=None,
            content_markdown="",
            metadata={"warning": "no_main_content"},
        )

    body_md = trafilatura.extract(
        tree,
        output_format="markdown",
        with_metadata=False,
        url=url,
        include_links=True,
        include_formatting=True,
    )

    doc = trafilatura.bare_extraction(tree, url=url, with_metadata=True)
    title = getattr(doc, "title", None) if doc is not None else None
    metadata = _gather_metadata(doc) if doc is not None else {}

    if not body_md:
        metadata = dict(metadata)
        metadata["warning"] = "no_main_content"
        return ExtractedContent(title=title, content_markdown="", metadata=metadata)

    return ExtractedContent(title=title, content_markdown=body_md, metadata=metadata)


def extract_text(raw: bytes, *, content_type: str, url: str) -> ExtractedContent:
    """Decode ``raw`` as UTF-8 text. JSON is pretty-printed when valid.

    The ``url`` argument is accepted for symmetry with :func:`extract_html`
    even though plain-text extraction makes no use of it today.
    """
    del url  # accepted for symmetry; unused
    text = _decode_bytes(raw)
    ctype = content_type.lower()
    metadata: dict[str, Any] = {"content_type": ctype}

    if "json" in ctype:
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=True)
            metadata["formatted"] = "json"
        except (json.JSONDecodeError, ValueError):
            metadata["warning"] = "invalid_json_passthrough"

    return ExtractedContent(title=None, content_markdown=text, metadata=metadata)


def is_html_content_type(content_type: str) -> bool:
    """True if ``content_type`` looks like HTML / XHTML."""
    head = content_type.lower().split(";", 1)[0].strip()
    return head in _HTML_CONTENT_TYPES


def is_text_content_type(content_type: str) -> bool:
    """True if ``content_type`` is plain text or JSON (not HTML, not XML).

    Per ADR-0004 the runtime floor handles only HTML (via trafilatura) and
    plain text / JSON passthrough. XML/RSS/Atom intentionally fall through
    to the unsupported-content-type error path — feed handling is a
    custom-tool concern (see ``templates/x-digest/tools/feed_read.py``).
    """
    head = content_type.lower().split(";", 1)[0].strip()
    if head in _HTML_CONTENT_TYPES:
        return False
    return head.startswith("text/") or "json" in head
