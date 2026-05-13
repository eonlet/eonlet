# Skill: Technical Analysis

> Load this skill when you need to assess a ticker's price action with chart patterns, trend, volume, and basic indicators.

## When to load this skill

Load via `load_skill(name="technical_analysis")` when:

- A holding has unusual price action and you want to characterize it
- A watchlist ticker is near a price level the user previously flagged
- The user asks "is XYZ overbought/oversold?"
- You need to articulate a buy zone or stop level

Do NOT load this skill for:

- Calculating P&L (just arithmetic)
- Fundamental questions (use fundamental_analysis instead)
- Macro / market-wide commentary

## Framework: 4 quick checks

For any ticker, in this order:

### 1. Trend (10s)

- Is the 50-day MA above the 200-day MA? (uptrend)
- Is price above the 50-day MA? (short-term bullish)
- Has the trend changed direction in the last 20 sessions?

If you have market_data return, you may not have MAs directly — note this and proceed with what you do have (e.g., "looking at the last 60 days of closes...").

### 2. Range and breakouts

- What is the 20-day range? 60-day range?
- Is today's close near a recent high or low?
- Is there a horizontal price level the stock has tested 3+ times in the last 6 months? (a "level")

### 3. Volume

- Is today's volume above or below the 20-day average?
- Did unusual volume coincide with a price move? (confirms move)
- Was a breakout on low volume? (weaker)

### 4. Momentum

- 14-day RSI: <30 = oversold, >70 = overbought (rough guide)
- Has RSI been diverging from price? (potential reversal)
- Are there back-to-back gap-ups or gap-downs?

## Patterns to recognize

A short list, in rough order of reliability:

- **Trend continuation**: pullback to 50-MA in an uptrend, bouncing with volume → continuation likely
- **Higher highs / higher lows**: ongoing uptrend
- **Lower highs / lower lows**: ongoing downtrend  
- **Range / consolidation**: 3+ tests of upper and lower bounds → eventual breakout
- **Breakout**: close above a clear resistance level on above-average volume
- **Failed breakout**: close above resistance, then quickly back inside range → bearish signal
- **Gap down on earnings + heavy volume**: institutional distribution; respect for at least a few days
- **Gap up on earnings + sustained**: institutional accumulation; can run

Patterns that sound official but are not very reliable (avoid leaning on these):
- Head and shoulders
- Cup and handle  
- Most candlestick patterns ("doji", "hammer", etc.) — high false-positive rate

## Output style

When you write a TA section in a report:

- Lead with the trend in one sentence
- Then note any specific feature (volume spike, gap, range break)
- Then make ONE actionable observation (not a prediction): "this could test ${level}" or "watch for hold of ${support} this week"
- Don't draw conclusions you can't back up

Bad example:
> "XYZ is showing strong bullish momentum with great volume and a beautiful breakout pattern. Target $200."

Good example:
> "XYZ broke above the $150 level on 2x average volume yesterday and held the breakout today. Next obvious resistance is the prior high at $172. A close back below $148 would invalidate the move."

## Limitations

- You're working from end-of-day data. Intraday patterns are not your strength.
- You can't compute MAs directly without enough history; if market_data returned only a few days, say so.
- TA does not predict; it characterizes. Never say "will" — say "could", "tends to", "watch for".

## Caveats to mention when appropriate

- Earnings within next week: TA matters less
- Low average volume (<1M shares/day): patterns less reliable
- Recent index-wide moves: the stock's pattern may just be market beta
