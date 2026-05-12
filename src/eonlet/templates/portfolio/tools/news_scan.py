"""
News scanner for a list of tickers.

If NEWS_API_KEY is set, uses newsapi.org. Otherwise falls back to the builtin
web_search tool (which the agent should use directly in that case).

Returns deduplicated, ranked news items per ticker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, Field

from eonlet.tools import ToolAnnotations, ToolContext, ToolResult, tool


class NewsScanArgs(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=30)
    since: datetime = Field(description="Fetch news published after this time.")
    max_per_ticker: int = Field(default=5, ge=1, le=20)


class NewsItem(BaseModel):
    ticker: str
    title: str
    source: str
    published_at: datetime
    url: str
    summary: str | None = None


class NewsScanResult(BaseModel):
    fetched_at: datetime
    provider: str
    items: list[NewsItem]
    fallback_note: str | None = None


@tool
class NewsScan:
    """Scan recent news for a list of tickers."""

    name = "news_scan"
    description = (
        "Find recent news for given tickers since a given timestamp. "
        "Returns up to `max_per_ticker` items per ticker. If NEWS_API_KEY "
        "is not set, falls back to suggesting use of web_search."
    )
    input_schema = NewsScanArgs
    output_schema = NewsScanResult
    annotations = ToolAnnotations(
        read_only=True,
        network=True,
        estimated_duration_s=5.0,
    )

    async def __call__(
        self, args: NewsScanArgs, ctx: ToolContext
    ) -> ToolResult:
        api_key = ctx.env.get("NEWS_API_KEY")

        if not api_key:
            return ToolResult(
                content=(
                    "NEWS_API_KEY not set. Falling back: use web_search "
                    "for each ticker with a query like "
                    "'TICKER news since {since}'."
                ),
                structured_output=NewsScanResult(
                    fetched_at=datetime.now(UTC),
                    provider="none",
                    items=[],
                    fallback_note="no api key; use web_search",
                ),
            )

        items: list[NewsItem] = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            for ticker in args.tickers:
                try:
                    resp = await client.get(
                        "https://newsapi.org/v2/everything",
                        params={
                            "q": ticker,
                            "from": args.since.isoformat(),
                            "pageSize": args.max_per_ticker,
                            "sortBy": "publishedAt",
                            "language": "en",
                        },
                        headers={"X-Api-Key": api_key},
                    )
                    if resp.status_code != 200:
                        continue
                    data = resp.json()
                    for art in data.get("articles", []):
                        items.append(
                            NewsItem(
                                ticker=ticker,
                                title=art["title"],
                                source=art["source"]["name"],
                                published_at=datetime.fromisoformat(
                                    art["publishedAt"].replace("Z", "+00:00")
                                ),
                                url=art["url"],
                                summary=art.get("description"),
                            )
                        )
                except httpx.HTTPError:
                    continue

        # Sort by published_at desc
        items.sort(key=lambda x: x.published_at, reverse=True)

        result = NewsScanResult(
            fetched_at=datetime.now(UTC),
            provider="newsapi",
            items=items,
        )

        # Render
        if not items:
            content = (
                f"No news found for {args.tickers} since "
                f"{args.since.isoformat()}."
            )
        else:
            lines = [f"News for {args.tickers} since {args.since.isoformat()}:"]
            current_ticker = None
            for item in items:
                if item.ticker != current_ticker:
                    lines.append(f"\n## {item.ticker}")
                    current_ticker = item.ticker
                lines.append(
                    f"- [{item.source}] {item.title} "
                    f"({item.published_at.isoformat()}) — {item.url}"
                )
            content = "\n".join(lines)

        return ToolResult(content=content, structured_output=result)
