"""DuckDuckGo HTML scrape — fragile zero-config fallback.

ADR-0004 explicitly accepts this as fragile. Prefer setting
``TAVILY_API_KEY`` for production use. The regex is the same shape as
the v0.0.2 implementation, refactored to share the ``HTTPFetcher`` and
to produce :class:`SearchResponse` rather than ad-hoc dicts.
"""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, unquote, urlparse

from ..transport import HTTPFetcher
from .types import SearchHit, SearchResponse

_DDG_URL = "https://duckduckgo.com/html/"
_TIMEOUT_S = 20.0

_RESULT_A = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_RESULT_SNIPPET = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


async def ddg_search(
    *,
    query: str,
    max_results: int,
    fetcher: HTTPFetcher,
) -> SearchResponse:
    """Scrape DuckDuckGo's HTML search page.

    Per ADR-0004 the failure mode is fragility, not security — the page
    layout changes silently. We surface zero hits rather than raising so
    the agent can fall back to a different strategy.
    """
    url = f"{_DDG_URL}?q={query.replace(' ', '+')}"
    body, _headers, _final = await fetcher.get(url, timeout_s=_TIMEOUT_S)
    text = body.decode("utf-8", errors="replace")

    titles = _RESULT_A.findall(text)
    snippets = _RESULT_SNIPPET.findall(text)

    hits: list[SearchHit] = []
    for i, (raw_url, raw_title) in enumerate(titles[:max_results]):
        snippet = snippets[i] if i < len(snippets) else ""
        hits.append(
            SearchHit(
                title=_strip_tags(raw_title),
                url=_decode_ddg_url(raw_url),
                snippet=_strip_tags(snippet),
            )
        )
    return SearchResponse(provider="ddg", query=query, hits=hits)


def _strip_tags(s: str) -> str:
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _decode_ddg_url(u: str) -> str:
    """DDG wraps real URLs as ``/l/?uddg=<urlencoded>&rut=...``. Unwrap."""
    parsed = urlparse(u if "://" in u else f"https:{u}")
    qs = parse_qs(parsed.query)
    uddg = qs.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return u
