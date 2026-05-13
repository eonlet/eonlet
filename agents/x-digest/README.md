# `x-digest` — Daily X Timeline Digest

A scheduled agent that reads your X (Twitter) timeline each morning, groups tweets by theme, and emails you a concise digest. The author uses this to stay on top of an information-dense follow list without spending hours scrolling.

## What it does

- Fires once per day at 08:00 (configurable timezone)
- Fetches tweets from people you follow since the last successful run
- Groups by theme (AI/ML, markets, geopolitics, etc.)
- Picks 3 "top reads" of the day
- Saves a Markdown digest to `workspace/outputs/`
- Emails the digest to you via SMTP
- Records run state in `memory/last_run.md`

## Architecture

```
┌─────────────────────────────────────────────┐
│  cron 0 8 * * *  (Asia/Tokyo)               │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  read last_run.md → get since_timestamp     │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  x_timeline_fetch(since=..., limit=200)     │  ← custom tool
│    → list of tweets                          │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  LLM: group by theme, pick top 3, format    │
└──────────────────┬──────────────────────────┘
                   ▼
┌─────────────────────────────────────────────┐
│  file_write → workspace/outputs/YYYY-MM-DD  │
│  send_email → user                          │
│  notes_append → last_run.md                 │
└─────────────────────────────────────────────┘
```

## Prerequisites

You need:

- **X API access (Basic tier or higher)** — $100/month from [X Developer Platform](https://developer.x.com). The free tier does NOT support reading timelines.
  - *Alternative:* modify `tools/x_timeline.py` to use an unofficial library like `twikit`. This violates X's ToS but is what some people do. Your call.
- **An SMTP account** for sending email — Gmail with app password, Fastmail, or your own server.

## Setup

```bash
# 1. Make sure ~/.eonlet/ exists
eonlet init

# 2. Configure secrets
cd ~/.eonlet/agents/x-digest/
cp .env.example .env
vim .env       # fill in X_BEARER_TOKEN, SMTP_*, EMAIL_TO

# 3. Validate the agent definition (catches typos)
eonlet def validate x-digest

# 4. Create an eonlet instance
eonlet create x-digest --name=morning

# 5. Test fire the trigger without waiting for 8am
eonlet fire x-digest.morning morning_digest

# 6. Watch the run
eonlet logs x-digest.morning --follow

# 7. Check the output
cat ~/.eonlet/eonlets/x-digest.morning/workspace/outputs/*.md

# 8. Check that you got the email!
```

After step 5 succeeds, the eonlet is set up. It will fire automatically at 08:00 every day until you `pause` or `kill` it.

## Customizing

### Change the schedule

Edit `~/.eonlet/agents/x-digest/agent.yaml`:

```yaml
triggers:
  - id: morning_digest
    schedule: "0 7 * * 1-5"          # 7am weekdays only
    timezone: America/New_York       # change tz
```

Then `eonlet restart x-digest.morning` (v0.2+) or `kill` and `create` again.

### Filter what gets included

Edit `~/.eonlet/eonlets/x-digest.morning/memory/filter_rules.md` (you can create it; the agent reads it on each run):

```markdown
# Filter rules

## Skip
- Retweets without commentary
- Tweets from @somebody (too much volume)

## Always include
- Anything from @anthropicai, @openai, @lilianweng
```

The agent treats this file as user-edited and respects it. You don't need to bump the definition version.

### Change tone / format

Edit `system.md`. Common adjustments:

- "Make the digest shorter — Top 5 only, no theme sections"
- "Add a 'controversial takes' section for spicy tweets"
- "Include rough engagement numbers (likes/retweets) per item"

### Run multiple digests with different scopes

```bash
# A separate eonlet for tech only
eonlet create x-digest --name=tech-only
# Edit its .env to set X_BEARER_TOKEN to a different account / different filter rules
```

Each eonlet has its own memory and state — they don't interfere.

## Limits

- X API Basic tier returns at most 200 tweets per call. If you follow many high-volume accounts, you may not get full coverage during quiet windows.
- The agent does not currently de-duplicate threads — if @someone tweets a 10-part thread, you may see references to multiple parts (the system prompt instructs the agent to fold these, but it's not perfect).
- The `send_email` tool sends one email per run. If SMTP fails, the digest still lives in `workspace/outputs/` and you can read it via `cat` or `eonlet attach` and `/notes`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `eonlet create` fails with "X_BEARER_TOKEN required" | env vars not set | `cp .env.example .env` and fill in |
| Trigger fires but no email arrives | SMTP config wrong | Check `eonlet logs`; verify with `swaks` or `mail` from CLI |
| API returns 401 | bearer token expired | Regenerate at developer.x.com |
| API returns 429 | rate-limited | Reduce `X_DIGEST_MAX_TWEETS` or wait |
| Digest is empty | nothing new since last run | Normal on a slow day; check `memory/last_run.md` |
| Agent died with `dead` status | uncaught exception | `eonlet logs` to see the error; common cause: SMTP timeout |

## See Also

- [`portfolio/`](../portfolio/) — a more complex scheduled agent with multiple custom tools and skills
- [`assistant/`](../assistant/) — the interactive baseline
- [`docs/TRIGGER_SPEC.md`](../../docs/TRIGGER_SPEC.md) — how cron triggers work in detail
- [`docs/TOOL_SPEC.md`](../../docs/TOOL_SPEC.md) — how to write custom tools like `x_timeline.py`
