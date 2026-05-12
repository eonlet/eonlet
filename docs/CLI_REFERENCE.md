# CLI Reference

The Eonlet CLI is the user's primary interface. This document is the complete reference, organized by command group.

## Conventions

- `<required>` — must be provided
- `[optional]` — may be omitted
- `--flag` — long-form flag
- `-f` — short form (where given)
- `[args...]` — variadic
- `<id>` arguments refer to an eonlet by `<type>.<name>` (e.g. `assistant.alice`); short forms (just `alice` if unambiguous) accepted

## Global flags

| Flag | Description |
|---|---|
| `--config <path>` | Use a non-default config file |
| `--log-level <level>` | `debug` / `info` / `warn` / `error` |
| `--json` | Machine-readable JSON output (for scripting) |
| `--help` | Show command help |
| `--version` | Show version |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | User error (bad arguments, validation) |
| 2 | System error (filesystem, network) |
| 3 | Not found |
| 4 | Conflict (already exists, busy) |
| 130 | Interrupted (Ctrl+C) |

---

## System Commands

### `eonlet init`

Set up `~/.eonlet/` for first use.

```bash
eonlet init [--force]
```

What it does:
1. Creates `~/.eonlet/` and all sub-directories
2. Writes default `config.yaml` (with placeholders for API keys)
3. Prompts to install bundled example agents (`assistant`, `x-digest`, `portfolio`)
4. Prints next-steps

Refuses to run if `~/.eonlet/` already exists, unless `--force` (which will *not* delete existing data — only overlay missing files).

### `eonlet version`

```bash
eonlet version
```

Prints version info:
```
eonlet 0.1.0
spec eonlet/v1
Python 3.12.4 (Darwin arm64)
```

### `eonlet doctor`

```bash
eonlet doctor
```

Runs self-checks:
- `~/.eonlet/` exists and is writable
- API keys (Anthropic / OpenAI) reachable
- SQLite version supports WAL2
- Cron parsing works
- All bundled agents validate
- No orphan socket files

Reports each as `✓` or `✗` with remediation hints.

---

## Definition Commands

Definitions = agent templates. These commands manage `~/.eonlet/agents/`.

### `eonlet def ls`

List all defined agent types.

```bash
eonlet def ls
```

Output:
```
NAME         VERSION  TRIGGERS  TOOLS  DESCRIPTION
assistant    0.1.0    -         11     General-purpose interactive assistant
x-digest     0.1.0    cron(1)   8      Daily X timeline digest
portfolio    0.1.0    cron(2)   13     Portfolio analysis and rebalancing
```

### `eonlet def init <name>`

Scaffold a new agent definition.

```bash
eonlet def init my-agent [--from-template=<type>]
```

What it does:
- Creates `~/.eonlet/agents/<name>/`
- Copies template files (default: `assistant`)
- Opens `agent.yaml` in `$EDITOR`

### `eonlet def validate <path>`

Validate an agent.yaml for syntax and semantic correctness.

```bash
eonlet def validate ~/.eonlet/agents/x-digest
# or by type name
eonlet def validate x-digest
```

Checks all rules in [AGENT_CONFIG_SPEC §16](AGENT_CONFIG_SPEC.md#16-validation). Exits 0 on success, prints all errors and warnings.

### `eonlet def show <type>`

Show effective configuration for an agent type.

```bash
eonlet def show x-digest [--format=yaml|json]
```

Shows the merged configuration with all defaults applied, environment substitutions resolved, etc.

### `eonlet def edit <type>`

Open the definition directory in `$EDITOR`.

```bash
eonlet def edit x-digest
```

Opens `agent.yaml` in `$EDITOR`. Vim users get `~/.eonlet/agents/x-digest/` as the working directory.

---

## Eonlet Lifecycle Commands

### `eonlet create <type>`

Spawn a new eonlet from a definition.

```bash
eonlet create <type> [--name=<name>] [--no-start] [-e VAR=value ...]
```

Flags:
- `--name=<name>` — Instance name; if omitted, generated (`<type>.<random>`)
- `--no-start` — Create directory and config but don't start the worker
- `-e VAR=value` — Set environment variable for this instance
- `--from-env=<path>` — Read env vars from a file

Workflow:
1. Validate the type's definition.
2. Resolve required env vars; error if missing.
3. Create `~/.eonlet/eonlets/<type>.<name>/`.
4. Fork worker (unless `--no-start`).
5. Wait up to 5s for `runtime.sock` to appear; report ready or timeout.

Example:
```bash
eonlet create x-digest --name=morning
eonlet create x-digest --name=tech-only --from-env=./tech-digest.env
```

### `eonlet ls`

List all eonlets.

```bash
eonlet ls [--all] [--filter=<status>]
```

Flags:
- `--all` — Include dead and hibernated
- `--filter=running|paused|dead|hibernated`

Output:
```
ID                       STATUS    UPTIME    LAST ACTIVE  BUDGET TODAY
assistant.alice          running   2d 4h     5m ago       $0.42 / $5.00
x-digest.morning         running   2d 4h     1h ago       $0.18 / $1.00
x-digest.evening         paused    1d        12h ago      $0.05 / $1.00
portfolio.main           running   2d 4h     8h ago       $1.45 / $3.00
```

### `eonlet ps`

`docker ps`-style detailed listing. Includes PID, CPU%, RSS, last action.

```bash
eonlet ps [--all]
```

Output:
```
ID                 PID    STATUS   CPU   RSS    LAST ACTION
assistant.alice    12345  running  0.1%  62MB   tool:web_fetch (3s ago)
x-digest.morning   12347  running  0.0%  48MB   idle (waiting for trigger)
portfolio.main     12349  running  0.2%  78MB   llm_call (5s ago)
```

### `eonlet pause <id>`

Send SIGSTOP to the worker. RAM is held; instant resume.

```bash
eonlet pause <id>
```

### `eonlet resume <id>`

Send SIGCONT. Worker resumes from where it was.

```bash
eonlet resume <id>
```

### `eonlet stop <id>`

Graceful shutdown. SIGTERM → 5s grace → SIGKILL.

```bash
eonlet stop <id> [--force]
```

Flags:
- `--force` — Skip SIGTERM, send SIGKILL directly. Data may be lost.

### `eonlet rm <id>`

Remove a dead eonlet's directory.

```bash
eonlet rm <id> [--with-data] [-y]
```

Flags:
- `--with-data` — Also delete `memory/` and `workspace/`
- `-y` — Skip confirmation

Refuses if eonlet status != `dead`. Run `eonlet stop <id>` first.

### `eonlet start <id>`

Start (or restart) a stopped eonlet without touching its data.

```bash
eonlet start <id>
```

---

## Interaction Commands

### `eonlet attach <id>`

Connect to an eonlet for live interaction.

```bash
eonlet attach <id> [--readonly] [--from=<event_id>]
```

Flags:
- `--readonly` — Watch only, no input
- `--from=<event_id>` — Replay history starting from this event

Enters an interactive REPL:
- Type messages, press Enter to send
- Streaming output renders in real time
- `Ctrl+B D` to detach (worker keeps running)
- `Ctrl+C` to interrupt current LLM call (does NOT exit attach)
- `Ctrl+D` to detach and quit
- `/help` to list slash commands

#### Slash commands (in attach mode)

| Command | Action |
|---|---|
| `/help` | Show available commands |
| `/state` | Print current eonlet state |
| `/notes` | Print contents of `memory/notes.md` |
| `/notes append "text"` | Append to notes.md |
| `/todo` | Print `memory/todo.md` |
| `/budget` | Show today's and month's spending |
| `/triggers` | List configured triggers and next fire times |
| `/fire <trigger_id>` | Manually fire a configured trigger (great for testing) |
| `/permissions` | Show current permission mode and deny list |
| `/skill ls` | List loaded skills |
| `/exit` | Detach |

### `eonlet send <id> "<message>"`

Send a one-shot message without entering attach mode.

```bash
eonlet send <id> "summarize today's portfolio activity"
```

Prints the eonlet's response and exits. Useful for shell scripts.

### `eonlet logs <id>`

View the eonlet's log file.

```bash
eonlet logs <id> [--follow] [--since=<time>] [--tail=<n>]
```

Flags:
- `--follow` / `-f` — Live tail
- `--since=<duration>` — e.g. `1h`, `2d`
- `--tail=<n>` — Last n lines

### `eonlet go <id>`

Open an interactive shell inside the eonlet's instance directory (`~/.eonlet/eonlets/<id>/`).

```bash
eonlet go assistant.alice
```

Spawns `$SHELL` (fallback: `/bin/sh`) with its CWD set to the instance directory. Type `exit` or press `Ctrl+D` to return to the original shell. Useful for inspecting `state.db`, `memory/`, `workspace/`, and log files directly.

### `eonlet inspect <id>`

Dump static configuration and resource layout as JSON.

```bash
eonlet inspect <id>
```

Output: identity (id, name, type, created_at), process status, message count, memory file list, workspace file list. Answers "what is this agent configured to be?" — use `eonlet status` for runtime metrics.

### `eonlet status <id>`

Show detailed runtime status in a rich terminal layout, or as JSON with `--json`.

```bash
eonlet status <id> [--json]
```

Answers "what is this agent doing right now?". Sections:

| Section | Contents |
|---|---|
| **PROCESS** | status, pid, uptime, heartbeat age |
| **TOKENS** | tokens in/out (total), cost today/total, last-turn in/out, turn count |
| **MEMORY** | per-tier token usage vs budget (working/STM/LTM/notes), todos active/done/cancelled, compact-paused flag |
| **TRIGGERS** | per-trigger: schedule, next fire, total fires, consecutive failures; data source `live` (IPC) or `offline` (event store) |
| **RECENT ACTIVITY** | last 10 events with kind, age, and payload preview |

Token totals are read from the event store — no LLM API call required. Working memory is estimated using the same 4-chars/token heuristic as the compaction budget; other tiers use file-read estimates. Trigger data is fetched live via IPC when the worker is running; falls back to the `trigger_state` table otherwise.

`--json` outputs the same data as a structured JSON object, one key per section. The schema is additive — new fields may be added in future versions.

---

## Memory Commands

### `eonlet memory migrate <legacy_dir>`

Migrate Claude Code auto-memory files into an eonlet's long-term memory (LTM).

```bash
eonlet memory migrate <legacy_dir> --eonlet <id> [--force] [--dry-run]
```

What it does:
1. Reads `<legacy_dir>/MEMORY.md` and the per-fact `.md` files it links to.
2. Maps each fact's frontmatter `metadata.type` → LTM category (`user` / `feedback` / `project` / `reference`; anything else becomes `fact`).
3. Writes each fact as one bullet in `memory/long_term.md` with trailer `[src:explicit, ts:<mtime>]`.

Flags:
- `--eonlet <id>` — **required**. Target eonlet instance (e.g. `assistant.alice`).
- `--force` — overwrite an existing `long_term.md`. Without this flag the command exits with code 4 if LTM already exists.
- `--dry-run` — preview the bullets that would be written without touching disk.

Exit codes: 0 on success, 3 if `legacy_dir` not found, 4 if LTM exists and `--force` not set.

Example:

```bash
# Preview what would be migrated
eonlet memory migrate ~/.claude/projects/my-project/memory --eonlet assistant.alice --dry-run

# Run the migration
eonlet memory migrate ~/.claude/projects/my-project/memory --eonlet assistant.alice
```

---

## Trigger Commands (MVP — minimal)

### `eonlet fire <id> <trigger_id>`

Manually fire a configured trigger (testing scheduled agents without waiting for cron).

```bash
eonlet fire x-digest.morning morning_digest
```

This is one of the most valuable commands during development.

---

## Debug Commands

### `eonlet replay <id>`

Read the event log and print every event in range. Read-only — never
re-executes LLM calls or tools.

```bash
eonlet replay <id> [--from=<event_id>] [--to=<event_id>]
                   [--format human|jsonl|json] [--compact]
                   [--head=<N>] [--tail=<N>]
```

Flags:

- `--from` / `--to` — inclusive event id range.
- `--format human` (default) — block-per-event, **full content**, no
  truncation. Every byte the LLM saw is rendered verbatim, so a truncated
  tool result in the log can never hide a truncated tool result in the
  actual conversation.
- `--format jsonl` — one JSON object per line; pipe into `jq`.
- `--format json` — single JSON array; convenient for `cat | jq`.
- `--compact` / `-c` — one-line-per-event summary (the old behaviour;
  useful for grep/scan).
- `--head N` / `--tail N` — show only first/last N matching events.

The verbose human format surfaces `tokens_in`/`tokens_out`/`cost_usd`,
`parent_id`, and `trigger_id` on the event header, and renders multi-line
`args` and tool `output` as fenced blocks so paths/scripts stay intact.

### `eonlet tail <id>`

Live event stream (like `logs --follow` but events, not logs).

```bash
eonlet tail <id>
```

Each line is one event with `kind`, `ts`, summary.

### `eonlet export <id>`

Export an eonlet's full state to a tar archive (for backup, sharing, or moving between machines).

```bash
eonlet export <id> --output=<file.tar.gz>
```

### `eonlet import`

Restore from an export.

```bash
eonlet import <file.tar.gz> [--as=<new-name>]
```

---

## v0.2+ Commands (NOT in MVP)

Documented for forward visibility:

- `eonlet hibernate <id>` — serialize and exit worker
- `eonlet permissions show/allow/deny` — fine-grained pattern management
- `eonlet skill ls / install / update` — manage skills from registry
- `eonlet fork <id> --as=<new>` — copy an eonlet for experimentation
- `eonlet trace <id>` — show last N traces (with OTel)

## v0.4+ Commands (Phase B, NOT in MVP)

- `eonlet discover` — find eonlets by capability
- `eonlet topology` — visualize inter-eonlet messages
- `eonlet supervisor start/stop/status` — manage `eonletd`

## v0.6+ Commands (Phase C — Teams, NOT in MVP)

- `eonlet team create <name>` — register a team from `team.yaml`
- `eonlet team list` — show all teams
- `eonlet team status <name>` — current work-in-progress
- `eonlet team send <name> "<task>"` — task routes to team leader
- `eonlet team disband <name>` — archive a team

## v0.8+ Commands (Phase D — Organizations, NOT in MVP)

- `eonlet org create <name>` — register an organization from `org.yaml`
- `eonlet org list` — show all organizations
- `eonlet org topology <name>` — render the tree
- `eonlet org send <name> "<task>"` — task routes to org leader, who decides which team

See [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md) for the conceptual model behind these commands.

---

## Examples: Common Workflows

### First-time setup

```bash
pip install eonlet
eonlet init
export ANTHROPIC_API_KEY=sk-ant-...
eonlet doctor
```

### Create an interactive assistant

```bash
eonlet create assistant --name=alice
eonlet attach alice
# ... chat ...
# Ctrl+B D to detach; alice keeps running
```

### Set up the daily X digest

```bash
# Edit ~/.eonlet/agents/x-digest/.env with your X token, SMTP creds
cp ~/.eonlet/agents/x-digest/.env.example ~/.eonlet/agents/x-digest/.env
vim ~/.eonlet/agents/x-digest/.env

# Create the eonlet
eonlet create x-digest --name=morning

# Test that the trigger works (don't wait until 8am)
eonlet fire x-digest.morning morning_digest

# Inspect output
ls ~/.eonlet/eonlets/x-digest.morning/workspace/outputs/

# Now it'll run every morning automatically
```

### Watch what an autonomous eonlet is doing

```bash
# Tail events in real time
eonlet tail portfolio.main

# Or full attach to watch + ask questions
eonlet attach portfolio.main --readonly
```

### Recover from a crash

```bash
# Eonlet shows as dead in `eonlet ls`
eonlet ls

# Inspect to understand why
eonlet inspect portfolio.main
eonlet logs portfolio.main --tail=100

# Restart — state is preserved
eonlet stop portfolio.main
eonlet start portfolio.main
```

### Backup before tinkering

```bash
eonlet export assistant.alice --output=alice-backup.tar.gz
# ... experiment ...
# If you break things:
eonlet rm assistant.alice --with-data
eonlet import alice-backup.tar.gz
```
