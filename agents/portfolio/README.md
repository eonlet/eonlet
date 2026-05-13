# `portfolio` — Daily Portfolio Analysis Agent

A scheduled agent that produces twice-daily reports on a personal US equity portfolio: a pre-market briefing and a post-close full analysis. Demonstrates the **complex scheduled agent** pattern with multiple custom tools and skills.

## What it does

**Pre-market run (08:30 ET weekdays):**
- Reads watchlist and holdings
- Scans overnight news
- Identifies pre-market movers
- Emails a ≤300 word brief

**Post-close run (16:30 ET weekdays):**
- Reads current positions and P&L
- Pulls EOD market data
- Scans news from the day
- Analyzes each significant holding
- Computes deviations from target allocation
- Scans watchlist for opportunities
- Emails a full ≤1500 word report

## What it explicitly does NOT do

- **Never places trades.** Hard-coded denial of all order-placement tools.
- **Never gives precise share counts.** Suggests direction and rough magnitude only.
- **Never gives advice outside daily portfolio analysis** (no tax, no retirement planning).

These constraints are baked into both `agent.yaml` (extra_deny patterns) and `system.md` (behavioral instructions).

## Architecture

```
   pre-market trigger             post-close trigger
   08:30 ET weekdays              16:30 ET weekdays
        │                                │
        ▼                                ▼
   ┌─────────────┐               ┌─────────────────────────────┐
   │ news_scan   │               │ portfolio_read              │
   │ market_data │               │ market_data (full EOD)      │
   │             │               │ news_scan                   │
   └─────────────┘               │ (optionally) load_skill     │
        │                        └─────────────────────────────┘
        ▼                                │
   ≤300 word brief                       ▼
   email                             full report
                                     write to workspace
                                     email
                                     append to history.md
```

Memory files (user-editable in italics):

- `notes.md` — agent-managed thesis notes
- *`watchlist.md`* — tickers to monitor
- *`target_allocation.md`* — target portfolio weights
- `history.md` — agent-managed daily log

The starred files are the **user's interface to shape the agent's behavior**. Edit them between runs to add tickers, change targets, etc. The agent reads them on every run.

## Prerequisites

You need:

- **A broker API for read access to positions** (Alpaca recommended for ease of setup; Interactive Brokers also supported)
- **Market data**: free yfinance works out of the box; for real-time, use Polygon ($29+/mo) or Tiingo
- **(Optional) NewsAPI** ($0–$449/mo): for tagged news. Falls back to web_search if unset.
- **SMTP** for reports

## Setup

### 1. Install Eonlet and initialize

```bash
pip install eonlet
eonlet init
```

### 2. Configure secrets

```bash
cd ~/.eonlet/agents/portfolio/
cp .env.example .env
vim .env
```

For Alpaca paper trading (recommended starting point — free, no real money):
- Sign up at https://app.alpaca.markets/signup
- Get paper API keys from "Paper Trading" dashboard
- Set `BROKER_PROVIDER=alpaca` and put paper keys in `.env`

### 3. Set up your watchlist and target allocation

Before first run, create these in the eonlet's memory directory (the agent will look for them):

```bash
# After `eonlet create portfolio --name=main`:
cd ~/.eonlet/eonlets/portfolio.main/memory/

# Watchlist: tickers you don't own but want monitored
cat > watchlist.md <<EOF
# Watchlist

## Tech
- NVDA — AI cycle
- MSFT — Copilot adoption
- AMD — server CPU share

## Energy
- XOM — base

## Notes
- Add a buy zone for NVDA at \$110 if it pulls back
EOF

# Target allocation
cat > target_allocation.md <<EOF
# Target Allocation

| Bucket       | Target % |
|--------------|----------|
| US Large Tech| 30       |
| US Other     | 30       |
| International| 15       |
| Bonds        | 10       |
| Cash         | 15       |
EOF
```

These files are yours. Edit them anytime — the agent reads them on every run.

### 4. Validate

```bash
eonlet def validate portfolio
```

### 5. Create the eonlet

```bash
eonlet create portfolio --name=main
```

### 6. Test fire

```bash
# Run the post-close analysis right now (don't wait until 16:30 ET)
eonlet fire portfolio.main post_close

# Watch it work
eonlet logs portfolio.main --follow
```

If it produces a report and emails you, you're set. It will now fire automatically twice per trading day.

## Customizing

### Use only paper / no real broker

Set `BROKER_PROVIDER=yfinance_only` in `.env`. The agent will look for `memory/holdings_manual.md` (a markdown table you maintain) instead of polling a real broker. Useful for testing without exposing brokerage credentials.

### Change report tone

Most behavior is in `system.md`. Common tweaks:

- "Be more aggressive" — change the system prompt to suggest larger position changes
- "Be more conservative" — increase the deviation thresholds before flagging
- "Add sector commentary" — modify the post-close template

### Add specialized skills

Drop a Markdown file in `skills/`. The agent will see it in the skill list and can `load_skill` it. Examples you might add:

- `skills/options_basics.md`
- `skills/sector_etfs.md`
- `skills/macro_factors.md`

### Multiple portfolios

```bash
# Same definition, different instances
eonlet create portfolio --name=main
eonlet create portfolio --name=retirement
# Each has independent memory, target allocation, etc.
```

You can use different brokerages per instance via instance-level `.env`:

```bash
# Instance-level .env (overrides type-level)
echo "BROKER_ACCOUNT_ID=different-account" >> ~/.eonlet/eonlets/portfolio.retirement/.env
```

## Safety

This is the most security-sensitive agent in the bundle. Key safeguards:

1. **`permissions.mode: yolo`** for autonomous runs, but with `extra_deny` patterns blocking ALL order-placement tools.
2. **Tools are read-only by design.** `portfolio_read`, `market_data`, `news_scan` cannot mutate broker state.
3. **No Bash access.** `extra_deny: ["Bash(*)"]` prevents shell escapes.
4. **Budget cap on `pause`**: if the agent exceeds `$3/day` of LLM cost, it pauses and waits for the user to inspect.
5. **Use paper trading for testing**, real money only after weeks of paper success.

Even with all this: **review every report critically**. The agent is a research assistant, not a oracle. If a report says "trim XYZ", *you* decide whether to act, with what timing, and at what size.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Reports are empty / errors | broker API unreachable | Check `eonlet logs`; verify with curl from command line |
| "Cannot identify pre-market data" | yfinance limitation | Use Polygon if you need real pre-market |
| Trigger fired but no email | SMTP misconfigured | Test SMTP with `swaks` |
| Agent says "no holdings_manual.md" in yfinance_only mode | file not created | Create the markdown table as documented |
| Agent suggests selling everything | likely the budget tripped before it finished | Check `eonlet inspect`; raise budget or investigate |

## See Also

- [`x-digest/`](../x-digest/) — simpler scheduled agent pattern
- [`assistant/`](../assistant/) — the interactive baseline
- [`docs/TRIGGER_SPEC.md`](../../docs/TRIGGER_SPEC.md) — how multiple triggers work
- [`docs/SECURITY.md`](../../docs/SECURITY.md) — the security model in detail
