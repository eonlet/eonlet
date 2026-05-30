"""Tavily search backend.

Calls Tavily's REST API directly (no `tavily-python` SDK) via the shared
``HTTPFetcher.post_json``, so search and ``web_fetch`` share one retry,
size-cap, and SSRF policy. The Tavily host is public, so the SSRF check
is a no-op for the normal happy path — but going through ``HTTPFetcher``
keeps reliability uniform across the two web tools.
"""

from __future__ import annotations

import json
import os
from datetime import datetime

from ...errors import HTTPFetchError
from ..transport import HTTPFetcher
from .types import SearchHit, SearchResponse

_TAVILY_URL = "https://api.tavily.com/search"
_TAVILY_TIMEOUT_S = 20.0


async def tavily_search(
    *,
    query: str,
    max_results: int,
    include_raw_content: bool,
    fetcher: HTTPFetcher,
    api_key: str | None = None,
) -> SearchResponse:
    """Run a Tavily search and map the response to :class:`SearchResponse`.

    Raises :class:`HTTPFetchError` when the API call fails (after the
    shared retry path exhausts). The caller (``WebSearchTool``) converts
    that into a ``ToolResult(is_error=True)`` so the agent sees a typed
    error instead of an exception.
    """
    key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY", "")
    if not key:
        raise HTTPFetchError(_TAVILY_URL, "TAVILY_API_KEY is not set")

    payload: dict[str, object] = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        # `advanced` gives raw_content for each hit; `basic` returns just
        # snippets. We swap based on the caller's preference.
        "search_depth": "advanced" if include_raw_content else "basic",
        "include_raw_content": include_raw_content,
        "include_answer": True,
    }

    body, _headers, _final = await fetcher.post_json(
        _TAVILY_URL, payload, timeout_s=_TAVILY_TIMEOUT_S
    )
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPFetchError(_TAVILY_URL, f"non-JSON response: {e}") from e

    raw_results = data.get("results") or []
    hits: list[SearchHit] = []
    for r in raw_results:
        hits.append(
            SearchHit(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("content") or ""),
                raw_content=r.get("raw_content"),
                published_at=_parse_published(r.get("published_date")),
            )
        )

    return SearchResponse(
        provider="tavily",
        query=query,
        hits=hits,
        answer=data.get("answer"),
    )


def _parse_published(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Tavily emits ISO 8601 with a "Z" suffix in some payloads.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
