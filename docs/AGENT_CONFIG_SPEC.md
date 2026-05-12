# Agent Configuration Specification

> **The single most important document in this repo.**
>
> Everything an Eonlet does is determined by an `agent.yaml` file plus its companion Markdown, Python, and configuration files. This spec is the contract between the user (the author of agents) and the runtime (the executor of agents).

| Field | Value |
|---|---|
| Spec version | 1 (`apiVersion: eonlet/v1`) |
| Status | DRAFT — accepting feedback |
| Applies to | Eonlet v0.1.0 and later |

---

## 1. Mental Model

An **agent definition** is a directory. The directory has a name; that name is the agent's `type`. Inside:

```
~/.eonlet/agents/<type>/
├── agent.yaml          # ◀── this spec describes this file
├── system.md           # the agent's identity and instructions
├── tools/              # optional Python files implementing custom tools
├── skills/             # optional Markdown files describing skills
├── prompts/            # optional templates referenced by system.md or tools
├── .env.example        # template for env vars; users copy to .env
└── README.md           # human-facing description
```

**Definitions are immutable templates.** When you run `eonlet create <type>`, the runtime creates an *eonlet instance* at `~/.eonlet/eonlets/<id>/` that points back to the definition. Changing the definition does not retroactively change running eonlets (until you `eonlet reload`, v0.2).

---

## 2. Top-level Structure

Every `agent.yaml` has this shape:

```yaml
apiVersion: eonlet/v1     # required, fixed for v0.1
kind: Agent               # required, fixed
metadata: {...}           # required
runtime: {...}            # required
triggers: [...]           # optional; if absent, eonlet is interactive-only
tools: {...}              # required
permissions: {...}        # optional; defaults to ask mode
memory: {...}             # optional
env: {...}                # optional
outputs: {...}            # optional (v0.2+)
hooks: {...}              # optional (v0.2+)
lifecycle: {...}          # optional
observability: {...}      # optional (v0.2+)
```

All sections are documented below with **MVP** / **v0.2+** / **v0.4+** labels.

---

## 3. `metadata` — required

Identity of the agent type.

```yaml
metadata:
  name: x-digest                        # required, must match directory name
  description: "Daily X timeline digest" # required, single line
  version: 0.1.0                         # required, SemVer
  authors:                               # optional
    - "ziyu@example.com"
  tags:                                  # optional
    - scheduled
    - personal

  # Forward-compatible fields (MVP ignores; Phase C/D uses them) ──────
  specialty: information_curation        # what this agent is good at
  capabilities:                          # what they advertise to teams
    - "summarize.social_media"
    - "produce.daily_digest"
  # ───────────────────────────────────────────────────────────────────

  homepage: "https://example.com"        # optional
```

### Field reference

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Snake-case or kebab-case identifier. Must equal directory name. |
| `description` | string | yes | One-line summary shown by `eonlet def ls`. |
| `version` | string | yes | SemVer. Bumped when behavior changes. |
| `authors` | list[string] | no | Email or handle of authors. |
| `tags` | list[string] | no | Free-form tags for filtering. |
| `specialty` | string | no | The agent's primary area of expertise. Forward-compat; used by team-formation tools in Phase C. |
| `capabilities` | list[string] | no | Dotted-notation actions this agent can perform. Forward-compat; used by team-leaders to discover/delegate. |
| `homepage` | string | no | URL with more documentation. |

### A note on `specialty` and `capabilities`

These fields are not used by the MVP runtime — they are **forward-compatible declarations** for the team-and-organization model coming in Phase C (see [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md)).

Even today, declaring them is good practice:

- **`specialty`**: one short phrase describing the agent's core expertise. Examples: `web_research`, `code_review`, `information_curation`, `portfolio_analysis`, `creative_writing`. Think of it as the answer to "what is this agent for, in three words?"
- **`capabilities`**: dotted-notation skills the agent can perform. The convention is `verb.object` (`summarize.long_documents`, `analyze.equities`, `draft.email`). When team coordination lands in Phase C, a team leader looking for "summarize.long_documents" will find any agent that lists that capability.

Authoring these fields now means your agent will participate naturally in future team formation without any rewrite.

---

## 4. `runtime` — required

The agent's execution envelope: which model, what limits, what to do when limits hit.

```yaml
runtime:
  model: claude-sonnet-4-6              # required
  fallback_model: claude-haiku-4-5      # optional
  max_context_tokens: 180000            # optional
  max_steps_per_run: 100                # optional
  max_wall_clock_per_run: 30m           # optional
  budget:
    daily_usd: 3.0
    monthly_usd: 50.0
    on_exceed: warn                     # warn | kill | pause
```

### Field reference

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `model` | string | yes | – | Model identifier. See *Model Strings* below. |
| `fallback_model` | string | no | – | Used when primary fails (rate-limit, 5xx). |
| `max_context_tokens` | int | no | 180_000 | Triggers compaction (v0.3) or truncation (MVP). |
| `max_steps_per_run` | int | no | 200 | Hard cap on tool-use iterations per run. |
| `max_wall_clock_per_run` | duration | no | 1h | Hard cap; SIGTERMs current run. |
| `budget.daily_usd` | float | no | none | Token cost cap per UTC day. |
| `budget.monthly_usd` | float | no | none | Token cost cap per calendar month. |
| `budget.on_exceed` | enum | no | `warn` | What to do: `warn`, `kill`, `pause`. |

### Model strings

`runtime.model` uses `<model>@<provider>` syntax. Two categories of provider:

#### Built-in providers

No config.yaml entry needed. Credentials come from standard environment variables.

| Provider | Env var | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | Anthropic Messages API |
| `openai` | `OPENAI_API_KEY`, `OPENAI_BASE_URL` (optional) | OpenAI Chat Completions |
| `fake` | — | In-process stub for tests/demos |

```yaml
runtime:
  model: claude-sonnet-4-6@anthropic
  fallback_model: claude-haiku-4-5-20251001@anthropic
  # or
  model: gpt-4o@openai
  model: fake-echo@fake
```

#### Custom providers (config.yaml §providers)

Add any OpenAI-compatible or Anthropic-compatible endpoint under `providers:` in
`~/.eonlet/config.yaml`, then reference it with `model@provider` in any agent.

```yaml
# ~/.eonlet/config.yaml
providers:
  openrouter:
    api: openai                            # which wire protocol to reuse
    base_url: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY        # optional; defaults to OPENROUTER_API_KEY

  deepseek:
    api: openai
    base_url: https://api.deepseek.com
    # api_key_env omitted → defaults to DEEPSEEK_API_KEY

  my-proxy:
    api: anthropic
    base_url: https://my-proxy.internal/v1
    api_key_env: PROXY_API_KEY
```

```yaml
# agent.yaml
runtime:
  model: anthropic/claude-sonnet-4-6@openrouter
  fallback_model: deepseek-chat@deepseek
```

`api_key_env` names the environment variable holding the key (never the key
itself). If omitted, the default is `<PROVIDER_NAME_UPPER>_API_KEY`.

#### Prefix-inferred strings (legacy)

For convenience, the `@provider` suffix can be omitted:
- `claude-*` → `anthropic`
- `fake-*` → `fake`
- anything else → `openai`

```yaml
runtime:
  model: claude-sonnet-4-6   # inferred: anthropic
  model: gpt-4o              # inferred: openai
```

### Duration syntax

For `max_wall_clock_per_run`, `lifecycle.idle_timeout`, etc., accept human strings: `30s`, `5m`, `2h`, `1d`. Parsed via a simple regex.

---

## 5. `triggers` — optional

What causes the agent to *act*. If absent, the eonlet is purely interactive (only reacts to user messages via `attach` or `send`).

```yaml
triggers:
  - id: morning_digest                  # required, unique per agent
    kind: cron                          # required
    schedule: "0 8 * * *"               # cron expression (5-field)
    timezone: Asia/Tokyo                # IANA tz; required
    enabled: true                       # optional, default true
    message: |                          # required; what the agent receives
      Time for the morning digest.
      Fetch X tweets from people I follow since your last successful run,
      group by theme, write to workspace, and email me.
    grace_period: 1h                    # optional; if a fire is missed by less
                                        # than this, fire on next start; else skip
  
  - id: market_close
    kind: cron
    schedule: "30 16 * * 1-5"           # weekdays 16:30
    timezone: America/New_York
    message: |
      US market just closed. Run the full portfolio analysis.
```

### Trigger kinds (MVP)

| Kind | When it fires | MVP? |
|---|---|---|
| `cron` | On cron schedule, in declared timezone | ✅ MVP |
| `interactive` | When user sends a message (implicit, always present) | ✅ MVP (implicit) |
| `webhook` | HTTP POST to declared endpoint | 🚧 v0.2 |
| `file_watch` | Filesystem path changes | 🚧 v0.2 |
| `peer_message` | Another eonlet sends a message | 🚧 v0.4 |

### How the agent receives a trigger

When a trigger fires, the worker injects a user-role message:

```
<trigger kind="cron" id="morning_digest" fired_at="2026-05-12T08:00:00+09:00">
  Previous successful run: 2026-05-11T08:01:23+09:00
  Time since last run: 23h 59m
  
  Time for the morning digest. Fetch X tweets from people I follow
  since your last successful run, group by theme, write to workspace,
  and email me.
</trigger>
```

The agent's `system.md` is written to recognize this block. See [`TRIGGER_SPEC.md`](TRIGGER_SPEC.md) for the full lifecycle.

### Cron expression

Standard 5-field POSIX cron: `minute hour day-of-month month day-of-week`.

Common patterns:
- `0 8 * * *` — daily at 08:00
- `0 */4 * * *` — every 4 hours
- `30 16 * * 1-5` — weekdays at 16:30
- `0 9 * * MON` — Monday at 09:00

Special strings (parsed by `croniter`): `@hourly`, `@daily`, `@weekly`, `@monthly`.

### `grace_period` semantics

If the worker is restarted (or was hibernated) and a scheduled fire was missed by less than `grace_period`, fire once on startup. If missed by more, skip and log an `event(kind="trigger_missed")`. This prevents "thundering herd" of catch-up firings after long downtime.

Default grace period: `1h`.

---

## 6. `tools` — required

Which tools the agent can use.

```yaml
tools:
  builtin:                              # list of builtin tool names
    - file_read
    - file_write
    - notes_read
    - notes_append
    - web_search
    - web_fetch
    - send_email
  custom:                               # list of paths (relative to definition)
    - ./tools/x_timeline.py
    - ./tools/format_digest.py
  # mcp_servers: ./mcp.json             # v0.2+
```

### Builtin tools (MVP — 13 tools)

| Tool | Annotations | Description |
|---|---|---|
| `bash` | destructive, network-capable | Run a shell command in workspace |
| `file_read` | read-only | Read a file (paginated) |
| `file_write` | destructive | Write/replace a file |
| `file_edit` | destructive | SEARCH/REPLACE edit |
| `glob` | read-only | Find files by glob |
| `grep` | read-only | Search file contents (ripgrep) |
| `web_search` | read-only, network | Search the web |
| `web_fetch` | read-only, network | Fetch a URL |
| `notes_read` | read-only | Read from `memory/` markdown files |
| `notes_append` | destructive | Append to `memory/` markdown files |
| `send_email` | network | Send an email via configured SMTP |
| `sleep` | read-only | Pause execution (for retry backoffs) |
| `load_skill` | read-only | Load a skill's full body into context |

See [`TOOL_SPEC.md`](TOOL_SPEC.md) for each tool's input schema and behavior.

### Custom tools

Each `custom:` entry is a path (relative to the definition directory) to a Python file. At eonlet startup, each file is imported; classes decorated with `@tool` are registered.

Minimal tool example:

```python
# tools/my_tool.py
from eonlet.tools import tool, ToolContext
from pydantic import BaseModel

class MyArgs(BaseModel):
    query: str

@tool
class MyTool:
    name = "my_tool"
    description = "Do something useful."
    input_schema = MyArgs
    annotations = {"read_only": True, "network": False}
    
    async def __call__(self, args: MyArgs, ctx: ToolContext) -> str:
        return f"Result for: {args.query}"
```

See [`TOOL_SPEC.md`](TOOL_SPEC.md#custom-tools) for the full interface.

---

## 7. `permissions` — optional

Controls what tool calls require user confirmation.

```yaml
permissions:
  mode: yolo                            # ask | yolo (MVP)
  extra_deny:                           # MVP supports user-extended deny
    - "Bash(npm publish*)"
    - "FileWrite(/Users/ziyu/important.txt)"
  # extra_allow: [...]                  # v0.2+: pattern-based allow
```

### Modes (MVP)

| Mode | Behavior |
|---|---|
| `ask` | Each tool call with `destructive: true` prompts the attached session. If no session is attached, the call is auto-denied and the agent gets an error. |
| `yolo` | Tool calls auto-execute. **Hardcoded deny list** still applies. Suitable for scheduled agents. |

Default: `ask`.

### Hardcoded deny (always enforced)

```
Bash(rm -rf /*), Bash(rm -rf ~*),
Bash(:(){*),
Bash(sudo*),
Bash(curl * | sh), Bash(wget * | sh),
FileWrite(/etc/**),
FileWrite(~/.ssh/**),
FileWrite(~/.aws/**),
FileWrite(~/.eonlet/**)
```

Cannot be overridden. (Hardcoded patterns are versioned with the runtime; future versions may expand the list, never contract.)

### `extra_deny`

User-added deny patterns, evaluated AFTER hardcoded deny. Pattern syntax:
- `Tool(pattern)` where `pattern` is a glob over the tool's input string representation
- `*` matches anything; `**` matches recursively for paths

### v0.2+ permission features

Will add: `extra_allow` patterns, `read_only` mode, `plan` mode (no execution, just plan output), hook-based custom gates.

---

## 8. `memory` — optional

How the agent's memory subsystem is configured (see `MEMORY_SPEC.md` for the full spec).

```yaml
memory:
  enabled: true                          # set false to disable all memory (M-I8)

  conversation:
    working_memory_tokens: 8000          # sliding window of raw events
    keep_recent_messages_min: 4          # always keep at least N turns
    short_term_tokens: 4000              # STM budget; tier-2 triggers when exceeded
    long_term_tokens: 8000               # LTM budget; tier-3 triggers when exceeded

  compaction_model: claude-haiku-4-5-20251001  # model used for tier-1/2/3 compaction

  # v0.2+
  # semantic_store: sqlite-vec
  # embedding_model: voyage-3
```

### Field reference

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Disable all memory writes and events (M-I8) |
| `conversation.working_memory_tokens` | int | `8000` | Token budget for raw recent events injected as working memory |
| `conversation.keep_recent_messages_min` | int | `4` | Minimum number of recent message turns always kept in working memory |
| `conversation.short_term_tokens` | int | `4000` | STM token budget; tier-2 compaction runs when `tokens(short_term.md)` exceeds this |
| `conversation.long_term_tokens` | int | `8000` | LTM token budget; tier-3 forgetting runs when `tokens(long_term.md)` exceeds this |
| `compaction_model` | string | `"claude-haiku-4-5-20251001"` | Model used for tier-1, tier-2, and tier-3 compaction passes |

### Memory layout per eonlet

```
~/.eonlet/eonlets/<id>/memory/
├── short_term.md       # compressed conversation summaries (STM)
├── long_term.md        # durable knowledge (LTM)
├── notes.md            # user-curated notes (never auto-deleted)
├── todos.jsonl         # structured to-do items
└── index.sqlite        # FTS5 recall index
```

### Removed legacy fields

The fields `recent_messages_in_context` and `notes_files` (v0.0.x schema) are **removed**.
The config loader raises `ConfigError` if either is present. Use `eonlet memory migrate` to
convert Claude Code auto-memory files to the new LTM format.

---

## 9. `env` — optional

Environment variables the agent needs. **Declared, validated at startup, never embedded.**

```yaml
env:
  required:
    - X_BEARER_TOKEN
    - SMTP_HOST
    - SMTP_USER
    - SMTP_PASSWORD
    - EMAIL_TO
  optional:
    - DEBUG_MODE
    - SMTP_PORT                         # has a default
  defaults:
    SMTP_PORT: "587"
```

### Semantics

- At eonlet creation (`eonlet create`), the runtime checks `env.required` are all set (either in shell, in `~/.eonlet/agents/<type>/.env`, or in `~/.eonlet/eonlets/<id>/.env`). Missing variables cause creation to fail with a clear error.
- Resolution order (highest wins):
  1. Environment variables set in the shell when `eonlet create` is run
  2. `~/.eonlet/eonlets/<id>/.env` (instance-specific)
  3. `~/.eonlet/agents/<type>/.env` (type-default)
- The runtime passes the resolved env to the worker process. It does **not** inject them into the LLM prompt.
- The agent accesses env via Python's `os.environ` inside custom tools.

### Why declared?

Failing fast at `eonlet create` is far better than the agent silently 5xx'ing at 3am because `SMTP_PASSWORD` wasn't set.

---

## 10. `outputs` — v0.2+ (declarative output channels)

Reserved for v0.2. Idea: declare "this agent emits to channels X, Y, Z" so the framework can manage credentials, retries, formatting.

```yaml
# v0.2+ — NOT in MVP
outputs:
  email:
    to: ziyu@example.com
    smtp_env_prefix: SMTP_              # uses SMTP_HOST, SMTP_USER, SMTP_PASSWORD
  file:
    base_dir: ./workspace/outputs
    naming: "{date}-{trigger_id}.md"
```

In MVP, output delivery is done via tools (`send_email`, `file_write`).

---

## 11. `hooks` — v0.2+

Not in MVP. v0.2 will add hooks at `pre_tool_use`, `post_tool_use`, `on_error`, `on_trigger`. Each hook is a path to a Python or shell script.

---

## 12. `lifecycle` — optional

Eonlet-instance behavior, mostly relevant in v0.2+ when hibernation lands.

```yaml
lifecycle:
  idle_timeout: 30m                     # v0.2+: auto-hibernate after idle
  pause_to_hibernate_after: 5m          # v0.2+: paused too long → hibernate
  max_lifetime: 365d                    # warn at 80% of this; never auto-kill
  on_crash: warn                        # MVP: warn | exit
                                        # v0.4+: restart (via supervisor)
```

MVP only honors `on_crash` (default `exit`). All other fields are documented but reserved for v0.2+.

---

## 13. `observability` — v0.2+

```yaml
# v0.2+ — NOT in MVP
observability:
  log_level: info
  trace_to: logfire
  trace_endpoint: "${LOGFIRE_TOKEN}"
```

In MVP, logs go to `eonlets/<id>/logs/current.log` only.

---

## 14. Full Example: Minimal Interactive Agent

```yaml
apiVersion: eonlet/v1
kind: Agent

metadata:
  name: assistant
  description: General-purpose interactive assistant
  version: 0.1.0
  specialty: general_assistance
  capabilities:
    - "general.conversation"
    - "manage.notes"

runtime:
  model: claude-sonnet-4-6
  max_steps_per_run: 200
  budget:
    daily_usd: 5.0

tools:
  builtin: [bash, file_read, file_write, file_edit, glob, grep,
            web_search, web_fetch, note, todo, recall, remember, forget, load_skill]

permissions:
  mode: ask

memory:
  enabled: true
  conversation:
    working_memory_tokens: 8000
    short_term_tokens: 4000
    long_term_tokens: 8000
```

Plus `system.md` with the agent's instructions.

## 15. Full Example: Scheduled Agent

```yaml
apiVersion: eonlet/v1
kind: Agent

metadata:
  name: x-digest
  description: Daily summary of X timeline
  version: 0.1.0
  specialty: information_curation
  capabilities:
    - "fetch.social_media_timeline"
    - "summarize.daily_digest"
    - "deliver.email"

runtime:
  model: claude-sonnet-4-6
  max_steps_per_run: 50
  budget:
    daily_usd: 1.0
    on_exceed: warn

triggers:
  - id: morning_digest
    kind: cron
    schedule: "0 8 * * *"
    timezone: Asia/Tokyo
    message: |
      Time for the morning digest. Fetch X tweets from people I follow
      since your last successful run, group by theme, write to workspace,
      and email me.

tools:
  builtin: [file_read, file_write, note, recall, send_email]
  custom:
    - ./tools/x_timeline.py

env:
  required:
    - X_BEARER_TOKEN
    - SMTP_HOST
    - SMTP_USER
    - SMTP_PASSWORD
    - EMAIL_TO

permissions:
  mode: yolo

memory:
  enabled: true

lifecycle:
  on_crash: warn
```

---

## 16. Validation

`eonlet def validate <path>` enforces:

- All required top-level fields present
- `metadata.name` matches directory name
- `metadata.version` is valid SemVer
- `runtime.model` resolves to a known provider
- All trigger IDs are unique within the agent
- Cron expressions parse via croniter
- IANA timezone strings are valid
- All `tools.custom` paths exist and are valid Python
- All `tools.builtin` names are in the registry
- Permission patterns syntactically valid
- Memory `notes_files` are paths (no traversal)
- Env-referenced variables (`${VAR}`) exist somewhere in `env.required + env.optional`

Errors are reported with file:line where possible.

---

## 17. Forward Compatibility

Unknown fields are **warned but accepted** in MVP. A definition with v0.2 fields can be loaded by v0.1 runtime (those fields are silently ignored). This means upgrading the runtime *does not* break old definitions.

The reverse — a v0.1 definition on a v0.2+ runtime — is always supported.

`apiVersion: eonlet/v1` is the only currently valid value. Future major schema changes will use `eonlet/v2` and require explicit migration.

---

## Appendix: Event Store Schema

For completeness — this is what the runtime persists, not what the user writes.

```sql
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,        -- unix microseconds
    kind        TEXT NOT NULL,
    payload     BLOB NOT NULL,           -- msgpack
    parent_id   INTEGER,
    trigger_id  TEXT,                    -- if event was part of a triggered run
    cost_usd    REAL,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    FOREIGN KEY (parent_id) REFERENCES events(id)
);
CREATE INDEX events_ts_idx        ON events(ts);
CREATE INDEX events_kind_idx      ON events(kind, id);
CREATE INDEX events_trigger_idx   ON events(trigger_id, id);

CREATE TABLE messages (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER UNIQUE NOT NULL,
    role        TEXT NOT NULL,           -- user|assistant|tool|system
    content     BLOB NOT NULL,           -- msgpack
    tokens      INTEGER,
    FOREIGN KEY (event_id) REFERENCES events(id)
);

CREATE TABLE trigger_state (
    trigger_id          TEXT PRIMARY KEY,
    last_fired_at       INTEGER,
    last_success_at     INTEGER,
    last_failure_at     INTEGER,
    consecutive_failures INTEGER DEFAULT 0
);
```

The `trigger_state` table is critical for the trigger system — used to compute "since last run" times, missed fires, and backoff after failures.

---

## Appendix: EventKind enumeration (MVP)

```
user_message              assistant_message         assistant_token_delta
tool_call                 tool_result               tool_error
permission_requested      permission_granted        permission_denied
memory_write              memory_read
trigger_fired             trigger_skipped           trigger_missed
budget_warning            budget_exceeded
session_started           session_ended
error                     log
```

v0.2+ adds: `compaction_*`, `hibernated`, `resumed`, `memory_evict`.
v0.4+ adds: `peer_message_in`, `peer_message_out`, `peer_message_error`.

---

**This spec defines the contract. The example agents in [`agents/`](../agents/) are working proofs.**
