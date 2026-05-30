"""Web subsystem — HTTP transport, SSRF guard, HTML→markdown extraction.

This package is the v0.1 implementation of ADR-0004. It deliberately ships
a minimal floor (HTML-only via trafilatura, SSRF-guarded httpx, token-based
pagination) and pushes everything else — PDF, RSS, headless browsing — to
custom tools or v0.2+ MCP servers.

Public surface:

- ``HTTPFetcher`` — the worker-level HTTP client singleton.
- ``extract_html`` / ``extract_text`` — content extraction.
- ``paginate`` — token-window slicing for context budgeting.
- ``ExtractedContent`` / ``PaginatedSlice`` — shared result models.
"""

from __future__ import annotations

from .fetch import ExtractedContent, extract_html, extract_text
from .pagination import PaginatedSlice, paginate
from .transport import HTTPFetcher

__all__ = [
    "ExtractedContent",
    "HTTPFetcher",
    "PaginatedSlice",
    "extract_html",
    "extract_text",
    "paginate",
]
