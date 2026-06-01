"""Shared search-result models.

Flat pydantic schema rather than a ``SearchProvider`` Protocol — see
ADR-0004 §"Part 1" for the "no abstraction" decision.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    """One result row from a web search backend."""

    title: str
    url: str
    snippet: str
    # Tavily can populate the full extracted body when
    # ``include_raw_content=True``; DDG never does.
    raw_content: str | None = None
    published_at: datetime | None = None


class SearchResponse(BaseModel):
    """Aggregate search result. ``provider`` distinguishes the backend."""

    provider: str  # "tavily" | "ddg"
    query: str
    hits: list[SearchHit] = Field(default_factory=list)
    # Tavily returns a free-text synthesized answer when available; DDG
    # never does. The agent can use it as a head-start before paging hits.
    answer: str | None = None
    # Structured warnings (e.g. ``raw_content_unavailable_on_ddg``).
    warnings: list[str] = Field(default_factory=list)
