"""
Market data fetcher.

Returns prices, OHLCV, and basic stats for a list of tickers. Supports two
providers selected via env:

  yfinance — free, 15-min delayed, no key needed (default)
  polygon  — real-time, requires MARKET_DATA_API_KEY

For pre-market / extended hours data, yfinance is limited; use polygon for
production use.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from eonlet.tools import tool, ToolContext, ToolResult, ToolAnnotations


class MarketDataArgs(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=50)
    kind: Literal["eod", "intraday", "pre_market"] = Field(
        default="eod",
        description=(
            "eod = end-of-day OHLCV. "
            "intraday = today's bars. "
            "pre_market = pre-market quote (best-effort)."
        ),
    )


class TickerQuote(BaseModel):
    ticker: str
    timestamp: datetime
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    previous_close: Decimal | None = None
    change_pct: Decimal | None = None
    market_cap: Decimal | None = None


class MarketDataResult(BaseModel):
    fetched_at: datetime
    provider: str
    quotes: list[TickerQuote]
    errors: dict[str, str] = Field(default_factory=dict)


@tool
class MarketData:
    """Fetch market data (OHLCV, quotes, basic stats) for one or more tickers."""

    name = "market_data"
    description = (
        "Fetch market data for tickers. `kind` can be eod (end-of-day close), "
        "intraday (today's session), or pre_market (best-effort pre-market quote)."
    )
    input_schema = MarketDataArgs
    output_schema = MarketDataResult
    annotations = ToolAnnotations(
        read_only=True,
        network=True,
        estimated_duration_s=3.0,
    )

    async def __call__(
        self, args: MarketDataArgs, ctx: ToolContext
    ) -> ToolResult:
        provider = ctx.env.get("MARKET_DATA_PROVIDER", "yfinance")

        if provider == "yfinance":
            result = await self._fetch_yfinance(args, ctx)
        elif provider == "polygon":
            result = await self._fetch_polygon(args, ctx)
        else:
            return ToolResult(
                content=f"Unsupported MARKET_DATA_PROVIDER: {provider}",
                is_error=True,
            )

        # Render
        lines = [
            f"Market data ({provider}) fetched at {result.fetched_at.isoformat()} ({args.kind}):"
        ]
        for q in result.quotes:
            change_str = (
                f"{q.change_pct:+.2f}%" if q.change_pct is not None else "n/a"
            )
            close_str = f"${q.close:.2f}" if q.close else "n/a"
            lines.append(
                f"  {q.ticker:<6} close={close_str:<10} change={change_str:<8} "
                f"vol={q.volume or 0:>12,}"
            )
        if result.errors:
            lines.append("Errors:")
            for ticker, err in result.errors.items():
                lines.append(f"  {ticker}: {err}")

        return ToolResult(
            content="\n".join(lines),
            structured_output=result,
        )

    async def _fetch_yfinance(
        self, args: MarketDataArgs, ctx: ToolContext
    ) -> MarketDataResult:
        # yfinance is sync; run in thread executor
        import yfinance as yf

        loop = asyncio.get_running_loop()
        quotes = []
        errors = {}

        def fetch_one(ticker: str) -> TickerQuote | str:
            try:
                t = yf.Ticker(ticker)
                if args.kind == "eod":
                    hist = t.history(period="2d")
                    if hist.empty:
                        return f"no data"
                    last = hist.iloc[-1]
                    prev = hist.iloc[-2] if len(hist) > 1 else None
                    prev_close = Decimal(str(prev["Close"])) if prev is not None else None
                    close = Decimal(str(last["Close"]))
                    change_pct = (
                        ((close - prev_close) / prev_close * 100)
                        if prev_close
                        else None
                    )
                    return TickerQuote(
                        ticker=ticker,
                        timestamp=datetime.now(timezone.utc),
                        open=Decimal(str(last["Open"])),
                        high=Decimal(str(last["High"])),
                        low=Decimal(str(last["Low"])),
                        close=close,
                        volume=int(last["Volume"]),
                        previous_close=prev_close,
                        change_pct=change_pct,
                    )
                elif args.kind == "intraday":
                    hist = t.history(period="1d", interval="5m")
                    if hist.empty:
                        return "no intraday data"
                    last = hist.iloc[-1]
                    return TickerQuote(
                        ticker=ticker,
                        timestamp=datetime.now(timezone.utc),
                        open=Decimal(str(hist.iloc[0]["Open"])),
                        high=Decimal(str(hist["High"].max())),
                        low=Decimal(str(hist["Low"].min())),
                        close=Decimal(str(last["Close"])),
                        volume=int(hist["Volume"].sum()),
                    )
                elif args.kind == "pre_market":
                    info = t.info
                    return TickerQuote(
                        ticker=ticker,
                        timestamp=datetime.now(timezone.utc),
                        close=Decimal(str(info.get("preMarketPrice") or info.get("regularMarketPrice") or 0)),
                        previous_close=Decimal(str(info.get("regularMarketPreviousClose", 0))),
                    )
                return f"unknown kind {args.kind}"
            except Exception as e:
                return f"{type(e).__name__}: {e}"

        # Parallelize across tickers
        results = await asyncio.gather(
            *[loop.run_in_executor(None, fetch_one, t) for t in args.tickers]
        )
        for ticker, r in zip(args.tickers, results):
            if isinstance(r, TickerQuote):
                quotes.append(r)
            else:
                errors[ticker] = str(r)

        return MarketDataResult(
            fetched_at=datetime.now(timezone.utc),
            provider="yfinance",
            quotes=quotes,
            errors=errors,
        )

    async def _fetch_polygon(
        self, args: MarketDataArgs, ctx: ToolContext
    ) -> MarketDataResult:
        # Implementation left as exercise; signature is identical.
        # Use https://polygon.io/docs/stocks
        return MarketDataResult(
            fetched_at=datetime.now(timezone.utc),
            provider="polygon",
            quotes=[],
            errors={"_": "Polygon implementation not yet provided in template"},
        )
