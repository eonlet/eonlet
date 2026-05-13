# Bundled Example Agents

This directory contains three example agent definitions, shipped with Eonlet. They serve four purposes:

1. **They prove the spec works.** If we can't build these with `agent.yaml` + Markdown + a few Python files, the design is wrong.
2. **They demonstrate the two modes.** `assistant` is interactive; `x-digest` and `portfolio` are scheduled.
3. **They are real, useful agents** that the author uses daily.
4. **They are the canonical starting templates.** `eonlet def init my-thing --from-template=assistant` copies one of these.

## The Three Examples

### `assistant` — interactive

A general-purpose interactive assistant. The Eonlet equivalent of "a research and writing helper that knows me."

- **Mode:** interactive
- **Triggers:** none (waits for user)
- **Tools:** general filesystem, web, notes
- **Complexity:** simple — minimal `system.md`, no custom tools
- **Use case:** daily research, writing, ad-hoc questions

[→ `assistant/`](./assistant/)

### `x-digest` — scheduled (simple)

Daily summary of your X (Twitter) timeline, delivered by email.

- **Mode:** scheduled
- **Triggers:** one cron (`0 8 * * *` daily)
- **Tools:** one custom tool (`x_timeline.py`) + builtins
- **Complexity:** moderate — one custom tool, simple workflow
- **Use case:** reading X without spending hours scrolling

[→ `x-digest/`](./x-digest/)

### `portfolio` — scheduled (complex)

Daily portfolio analysis, market scan, and rebalancing suggestions for a personal US-equity portfolio.

- **Mode:** scheduled
- **Triggers:** two cron (pre-market briefing + post-close analysis)
- **Tools:** three custom tools + builtins; two skills
- **Complexity:** high — multiple data sources, complex analysis, sensitive permissions
- **Use case:** stay on top of holdings without watching screens all day

[→ `portfolio/`](./portfolio/)

## How to Use

### To run an example as-is

```bash
# 1. Make sure ~/.eonlet/ is initialized
eonlet init

# 2. Set up env vars for the agent
cd ~/.eonlet/agents/x-digest/
cp .env.example .env
vim .env  # fill in your X_BEARER_TOKEN, SMTP creds, etc.

# 3. Create an eonlet instance
eonlet create x-digest --name=morning

# 4. Test fire the trigger
eonlet fire x-digest.morning morning_digest

# 5. Check the output
ls ~/.eonlet/eonlets/x-digest.morning/workspace/outputs/
```

### To use as a starting template

```bash
eonlet def init my-news-agent --from-template=x-digest
# Edit ~/.eonlet/agents/my-news-agent/agent.yaml and system.md
```

## Design Patterns Demonstrated

| Pattern | Where to see it |
|---|---|
| Interactive agent | `assistant/system.md` |
| Single-trigger scheduled agent | `x-digest/agent.yaml` |
| Multi-trigger scheduled agent | `portfolio/agent.yaml` |
| Custom Python tool | `x-digest/tools/x_timeline.py` |
| Multi-file custom tools | `portfolio/tools/` |
| Skills (Markdown loaded on demand) | `portfolio/skills/` |
| Env-var-based secrets | `x-digest/.env.example` |
| User-editable memory files | `portfolio/watchlist.md.example` |
| Yolo mode + explicit deny | `portfolio/agent.yaml.permissions` |
| Cross-run state via memory files | `x-digest/system.md` reads `last_run.md` |

## What's NOT Demonstrated (Yet)

These will appear as new examples in v0.2+:

- MCP integration (any agent using a remote MCP tool)
- Hooks (`pre_tool_use` audit)
- Multi-eonlet collaboration (v0.4 — planner + worker pattern)
- File-watch triggers (v0.2 — "process this paper when it lands in Downloads")
- Code execution mode (v0.3)

## Contributing Examples

After v0.2, we'll accept PRs of new example agents. Criteria for inclusion:

- The agent does something concretely useful, not just "echo for tutorial"
- It demonstrates a pattern not already shown
- Custom tools have tests or documented manual test steps
- README explains setup including all env vars
- `.env.example` is complete; no secrets in `.env` or in `agent.yaml`

Until v0.2, the bundled set stays small to keep CI fast and the maintenance surface narrow.
