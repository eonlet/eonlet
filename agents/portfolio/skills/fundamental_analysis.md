# Skill: Fundamental Analysis

> Load this skill when you need to assess a company's underlying business — earnings, balance sheet, competitive position, valuation.

## When to load this skill

Load via `load_skill(name="fundamental_analysis")` when:

- A company you cover reports earnings
- The user asks whether a stock is "expensive" or "cheap"
- You need to evaluate a new watchlist candidate
- A thesis needs revisiting after a guidance change

Do NOT load this skill for:

- Pure price-action questions (use technical_analysis instead)
- Index-level commentary (this is single-stock focused)
- Daily P&L reporting (just arithmetic)

## Framework: 5 questions

For any company, in this order:

### 1. Does it grow? (revenue)

- Last 4 quarters of revenue YoY growth: trend up, flat, or down?
- Is growth decelerating or accelerating?
- What is consensus revenue growth for the next year?

A business with >15% YoY growth and accelerating > a business with 5% growth and decelerating, even at the same valuation.

### 2. Does it make money? (profitability)

- Gross margin: stable, expanding, compressing?
- Operating margin: same questions
- Free cash flow positive? Growing?
- For unprofitable companies: is the path to profitability credible (gross margin trending up, opex growth slower than revenue)?

### 3. Is the balance sheet healthy?

- Cash and equivalents vs total debt
- Current ratio (current assets / current liabilities) — >1 is fine, >2 is comfortable
- Are share counts growing? (dilution; bad if buying)
- Is the company buying back shares? (returning capital)

### 4. What is the price asking? (valuation)

- P/E (forward): how does it compare to its 5-year average? to peers?
- EV/Sales or EV/EBITDA: for non-earnings stories
- PEG (P/E ÷ growth): rough sanity check, useful for high-growth
- Free cash flow yield (FCF / market cap): how much real cash is this paper generating per dollar of price?

The trap: cheap stocks are usually cheap for a reason. Cross-reference with question 1: is growth in the toilet?

### 5. What can change? (catalysts and risks)

- Upcoming catalysts: next earnings date, product launches, contract renewals
- Risks: customer concentration, regulatory, competitive pressure, dependence on one product line
- What does the bear case look like? Can you steelman it?

## When to upgrade a thesis

A thesis is valuable when:
- The numbers visibly support a story (growth + margins + balance sheet all align)
- The valuation is reasonable relative to the growth profile
- A specific, identifiable catalyst exists within the next 6–12 months
- You can name 1–2 concrete things that would invalidate the thesis (a stop-loss for ideas, not just prices)

A thesis is suspect when:
- The story is mainly about "the future" with weak current numbers
- Valuation depends on heroic assumptions
- The thesis is fashionable (every tech podcast talks about it)
- You can't articulate what would change your mind

## Earnings quick-read

When earnings come out, in order:

1. **Revenue vs consensus**: beat / inline / miss, by what magnitude
2. **EPS vs consensus**: same
3. **Forward guidance vs consensus**: this is usually the bigger driver
4. **Margin trends**: did gross or operating margin move materially?
5. **Commentary**: what changed in the operating environment? (one-time vs structural)
6. **Reaction**: how does the stock react? Is it sensible vs the print?

A "beat and raise" with strong guidance and a sell-off is interesting (something is being read between the lines). A miss with a rally is also interesting (low expectations were met / dread relief).

## Output style

When writing a fundamental section in a report:

- Lead with the most important fact ("revenue grew 22% YoY, accelerating from last quarter")
- Then the qualifier ("margins compressed by 200bps on increased opex investment")
- Then the valuation context ("trades at 18x forward EPS vs 5-year avg 22x")
- Then your read ("the deceleration in operating leverage is the question to watch over next 2 quarters")

Bad example:
> "XYZ is a great company with strong fundamentals and exciting growth opportunities."

Good example:
> "XYZ reported $4.2B in revenue (+18% YoY), accelerating from +14% last quarter. Operating margin compressed to 28% (from 31% YoY) on increased S&M; management framed this as 'investing for next year'. The stock trades at 24x forward EPS, in line with its 5-year average. The story remains intact but margins need to recover by Q3 for the valuation to make sense."

## Limitations

- You can't read SEC filings directly in MVP. You're working from headline numbers in news, market_data, and the user's notes.
- "Quality" is hard to assess without diving into business models. Be honest about what you don't know.
- Don't confuse a low P/E with a cheap stock. Sometimes the E is about to disappear.

## What you don't do here

- Discounted cash flow models (too much guesswork without proper inputs)
- Detailed peer comps (do these with the user interactively, not autonomously)
- Macro framings ("rates going up means tech goes down") — outside scope
- Crypto / commodities / FX — outside scope

Stay in your lane. Single-company fundamentals on US equities.
