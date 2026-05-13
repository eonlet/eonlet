"""
Read current positions from the configured broker.

Required env vars:
    BROKER_PROVIDER  — alpaca | ibkr | yfinance_only
    BROKER_API_KEY   — credentials (semantics vary by provider)
    BROKER_API_SECRET (alpaca only)
    BROKER_ACCOUNT_ID — account identifier

Returns positions in a normalized format regardless of provider.

This tool is read-only. It cannot place, cancel, or modify orders.
Even if your API key has trading permissions, this tool only uses the
read endpoints.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from eonlet.tools import tool, ToolContext, ToolResult, ToolAnnotations


class PortfolioReadArgs(BaseModel):
    include_closed_positions: bool = Field(
        default=False,
        description="If True, include positions closed today.",
    )


class Position(BaseModel):
    ticker: str
    shares: Decimal
    avg_cost: Decimal
    current_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    unrealized_pl_pct: Decimal
    daily_pl: Decimal | None = None


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    account_id: str
    provider: str
    cash: Decimal
    equity: Decimal
    buying_power: Decimal | None = None
    daily_pl: Decimal | None = None
    daily_pl_pct: Decimal | None = None
    positions: list[Position]
    note: str | None = None


@tool
class PortfolioRead:
    """Read current positions and account balance from the broker."""

    name = "portfolio_read"
    description = (
        "Read current portfolio: positions (ticker, shares, avg cost, market "
        "value, unrealized P&L), cash balance, and account equity. Read-only — "
        "this tool cannot place trades."
    )
    input_schema = PortfolioReadArgs
    output_schema = PortfolioSnapshot
    annotations = ToolAnnotations(
        read_only=True,
        network=True,
        estimated_duration_s=2.0,
    )

    async def __call__(
        self, args: PortfolioReadArgs, ctx: ToolContext
    ) -> ToolResult:
        provider = ctx.env.get("BROKER_PROVIDER", "alpaca")

        try:
            if provider == "alpaca":
                snapshot = await self._read_alpaca(ctx)
            elif provider == "yfinance_only":
                snapshot = await self._read_manual(ctx)
            elif provider == "ibkr":
                return ToolResult(
                    content="IBKR support not yet implemented in this template. "
                            "See tools/portfolio_read.py to add it.",
                    is_error=True,
                )
            else:
                return ToolResult(
                    content=f"Unknown BROKER_PROVIDER: {provider}",
                    is_error=True,
                )
        except httpx.HTTPError as e:
            return ToolResult(
                content=f"Broker API error: {e}. "
                        f"Suggestion: sleep and retry, or write a partial report.",
                is_error=True,
            )

        # Render for LLM: structured output is in structured_output, but
        # the visible content is a readable summary.
        lines = [
            f"Account {snapshot.account_id} ({snapshot.provider}) at "
            f"{snapshot.timestamp.isoformat()}",
            f"  Cash: ${snapshot.cash:,.2f}",
            f"  Equity: ${snapshot.equity:,.2f}",
        ]
        if snapshot.daily_pl is not None:
            lines.append(
                f"  Today's P&L: ${snapshot.daily_pl:,.2f} "
                f"({snapshot.daily_pl_pct:+.2f}%)"
            )
        lines.append(f"  Positions ({len(snapshot.positions)}):")
        for p in sorted(
            snapshot.positions, key=lambda x: x.market_value, reverse=True
        ):
            lines.append(
                f"    {p.ticker:<6} {p.shares} sh  "
                f"@${p.avg_cost:>8.2f}  "
                f"now ${p.current_price:>8.2f}  "
                f"mv ${p.market_value:>12,.2f}  "
                f"unr P&L {p.unrealized_pl_pct:+6.2f}%"
            )
        if snapshot.note:
            lines.append(f"Note: {snapshot.note}")

        return ToolResult(
            content="\n".join(lines),
            structured_output=snapshot,
        )

    async def _read_alpaca(self, ctx: ToolContext) -> PortfolioSnapshot:
        api_key = ctx.env["BROKER_API_KEY"]
        api_secret = ctx.env["BROKER_API_SECRET"]
        account_id = ctx.env["BROKER_ACCOUNT_ID"]

        # Paper or live based on key prefix
        base_url = (
            "https://paper-api.alpaca.markets"
            if api_key.startswith("PK")
            else "https://api.alpaca.markets"
        )
        headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            acct_resp = await client.get(
                f"{base_url}/v2/account", headers=headers
            )
            acct_resp.raise_for_status()
            acct = acct_resp.json()

            pos_resp = await client.get(
                f"{base_url}/v2/positions", headers=headers
            )
            pos_resp.raise_for_status()
            positions_raw = pos_resp.json()

        positions = [
            Position(
                ticker=p["symbol"],
                shares=Decimal(p["qty"]),
                avg_cost=Decimal(p["avg_entry_price"]),
                current_price=Decimal(p["current_price"]),
                market_value=Decimal(p["market_value"]),
                unrealized_pl=Decimal(p["unrealized_pl"]),
                unrealized_pl_pct=Decimal(p["unrealized_plpc"]) * 100,
                daily_pl=Decimal(p["unrealized_intraday_pl"])
                if "unrealized_intraday_pl" in p
                else None,
            )
            for p in positions_raw
        ]

        daily_pl = Decimal(acct["equity"]) - Decimal(acct["last_equity"])
        daily_pl_pct = (
            (daily_pl / Decimal(acct["last_equity"])) * 100
            if Decimal(acct["last_equity"]) > 0
            else Decimal(0)
        )

        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            account_id=account_id,
            provider="alpaca",
            cash=Decimal(acct["cash"]),
            equity=Decimal(acct["equity"]),
            buying_power=Decimal(acct["buying_power"]),
            daily_pl=daily_pl,
            daily_pl_pct=daily_pl_pct,
            positions=positions,
        )

    async def _read_manual(self, ctx: ToolContext) -> PortfolioSnapshot:
        """Read from memory/holdings_manual.md — user-edited holdings."""
        # Minimal implementation: parse a simple markdown table.
        # User maintains the file:
        #   | Ticker | Shares | Avg Cost |
        #   |--------|--------|----------|
        #   | AAPL   | 100    | 150.00   |
        manual_path = ctx.memory_dir / "holdings_manual.md"
        if not manual_path.exists():
            return PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                account_id="manual",
                provider="yfinance_only",
                cash=Decimal(0),
                equity=Decimal(0),
                positions=[],
                note=(
                    "No holdings_manual.md in memory/. "
                    "Create one with markdown table of holdings."
                ),
            )
        # Parse implementation left as user exercise; this is a template.
        # See tools/market_data.py to enrich with current prices.
        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc),
            account_id="manual",
            provider="yfinance_only",
            cash=Decimal(0),
            equity=Decimal(0),
            positions=[],
            note="Manual mode: complete the parser in _read_manual()",
        )
