"""Web search backends — Tavily (recommended) and DDG (zero-config fallback).

ADR-0004 §"Part 1": two paths, no abstraction. Provider selection is by
``TAVILY_API_KEY`` env-var presence. See ``docs/plans/web-tools.md``
"Resolved decisions" §2: the chosen path is recorded via the
``provider`` field on ``SearchResponse`` and on the
``WEB_SEARCH_PERFORMED`` event — no separate fallback event.
"""

from __future__ import annotations

from .ddg import ddg_search
from .tavily import tavily_search
from .types import SearchHit, SearchResponse

__all__ = ["SearchHit", "SearchResponse", "ddg_search", "tavily_search"]
