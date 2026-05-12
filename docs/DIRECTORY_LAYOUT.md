# Directory Layout

This document is the single source of truth for **every file and directory** Eonlet creates or expects. If a file's purpose isn't here, it shouldn't exist.

## 1. Eonlet Home: `~/.eonlet/`

```
~/.eonlet/
├── config.yaml                 # global config (model API keys, defaults)
├── agents/                     # AGENT DEFINITIONS (templates)
│   ├── assistant/              # bundled examples
│   ├── x-digest/
│   ├── portfolio/
│   └── <your-custom-type>/
├── eonlets/                    # RUNNING / EXISTING EONLETS (instances)
│   ├── assistant.alice/
│   ├── x-digest.morning/
│   └── portfolio.main/
├── teams/                      # ◀── Phase C (v0.6+): team definitions
│   └── research-and-write/     #     team.yaml describing leader + members
├── orgs/                       # ◀── Phase D (v0.8+): organization definitions
│   └── investment-office/      #     org.yaml describing tree of teams
├── shared/                     # cross-eonlet shared assets (v0.2+)
│   ├── skills/
│   └── tools/
└── logs/                       # global logs (CLI errors, etc.)
    └── cli.log
```

The `teams/` and `orgs/` directories are reserved but unused in MVP. They appear in Phase C and D respectively. See [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md) for the full vision.

`~/.eonlet/` is the **single user-level root**. Eonlet does not write outside of it (except via tools the agent invokes, which respect the workspace boundary).

System-level installation at `/etc/eonlet/` is reserved for future multi-user setups, not used in MVP.

## 2. Agent Definition: `~/.eonlet/agents/<type>/`

This is what the user authors. **Immutable from the runtime's perspective.**

```
~/.eonlet/agents/x-digest/
├── agent.yaml                  # required — see AGENT_CONFIG_SPEC.md
├── system.md                   # required — system prompt with frontmatter
├── README.md                   # optional but recommended
├── .env.example                # template for env vars; users copy to .env
├── .env                        # local secret (gitignored!)
├── tools/                      # optional — custom Python tools
│   ├── x_timeline.py
│   └── format_digest.py
├── skills/                     # optional — Markdown skills loaded on demand
│   ├── content_classification.md
│   └── digest_formatting.md
├── prompts/                    # optional — auxiliary templates
│   └── digest_template.md
└── mcp.json                    # optional — MCP server declarations (v0.2+)
```

### Required files

- **`agent.yaml`** — the configuration ([spec](AGENT_CONFIG_SPEC.md))
- **`system.md`** — the system prompt; agent identity and behavior. Supports YAML frontmatter for variable substitution (v0.2+)

### Optional but conventional

- **`README.md`** — human description, setup steps, what env vars to set
- **`.env.example`** — template showing required vars (with placeholder values)
- **`tools/*.py`** — custom Python tools (one tool class per file recommended)
- **`skills/*.md`** — Markdown skill references the agent can `load_skill(...)`
- **`prompts/*.md`** — templates the system prompt or tools may reference
- **`mcp.json`** — v0.2+ MCP server declarations

### File naming conventions

- Type directory name: `kebab-case` (`x-digest`, not `X_Digest` or `xDigest`)
- Python tool files: `snake_case.py`
- Skill / prompt files: `snake_case.md`
- YAML keys: `snake_case`

## 3. Eonlet Instance: `~/.eonlet/eonlets/<id>/`

This is what the runtime owns. Users may inspect but should not modify (with one important exception: `memory/*.md` files are user-editable).

`<id>` convention: `<type>.<name>` (e.g., `x-digest.morning`).

```
~/.eonlet/eonlets/x-digest.morning/
├── meta.json                   # runtime metadata; written at create time
├── state.db                    # SQLite event store (WAL2 mode)
├── state.db-shm
├── state.db-wal
├── .env                        # optional, overrides definition's .env
├── memory/                     # PERSISTENT MEMORY
│   ├── notes.md                # agent-managed, user-editable
│   ├── todo.md                 # ditto
│   ├── last_run.md             # ditto (used by scheduled agents)
│   └── attachments/            # large blobs (screenshots, PDFs)
│       └── 2026-05-12_chart.png
├── workspace/                  # agent's working directory
│   ├── outputs/                # generated artifacts (digests, reports)
│   │   └── 2026-05-12-digest.md
│   └── tmp/                    # transient files (cleared on restart)
├── logs/
│   ├── current.log             # active log
│   └── archive/
│       └── 2026-05-11.log.gz
├── runtime.sock                # Unix socket — clients connect here
├── pid                         # current process ID (absent if not running)
├── heartbeat                   # timestamp file, updated every 10s by worker
└── status                      # single-string file: running|paused|dead|hibernated
```

### File semantics

| File | Writer | Reader | Notes |
|---|---|---|---|
| `meta.json` | CLI (at create) | CLI, worker | Mostly read-only after create |
| `state.db` | Worker only | Worker (write), CLI (read for replay) | Single writer |
| `.env` | User | Worker | Overrides definition's `.env` |
| `memory/*.md` | Worker (via tools), user | Worker, user | **User edits allowed** |
| `workspace/` | Worker | Worker, user | User can browse outputs |
| `logs/` | Worker | User (via `eonlet logs`) | Rotated 50MB × 3 |
| `runtime.sock` | Worker (bind) | CLI (connect) | Deleted on clean exit |
| `pid` | Worker | CLI | Absent = not running |
| `heartbeat` | Worker | CLI | Stale > 30s = unresponsive |
| `status` | Worker | CLI | One of: `running`, `paused`, `dead`, `hibernated` |

### `meta.json` schema

```json
{
  "uuid": "01HXXXXXXX...",
  "name": "morning",
  "type": "x-digest",
  "definition_version": "0.1.0",
  "definition_path": "/Users/ziyu/.eonlet/agents/x-digest",
  "created_at": "2026-05-10T14:00:00Z",
  "last_active_at": "2026-05-12T08:01:23Z",
  "spec_version": "eonlet/v1"
}
```

### Status file values

```
running       — worker process alive, accepting work
paused        — worker process in SIGSTOP state (RAM held)
dead          — worker exited; state.db intact, can be restarted
hibernated    — (v0.2+) worker exited cleanly, state serialized
```

## 4. Repository Layout (this repo)

What the project itself looks like on disk.

```
eonlet/                         # repo root
├── README.md
├── MANIFESTO.md
├── ROADMAP.md
├── LICENSE
├── CONTRIBUTING.md
├── CHANGELOG.md
├── pyproject.toml
├── .gitignore
├── .pre-commit-config.yaml     # (to be added)
├── .github/                    # CI workflows (to be added)
├── docs/
│   ├── SPEC.md
│   ├── AGENT_CONFIG_SPEC.md
│   ├── DIRECTORY_LAYOUT.md     ← (this file)
│   ├── CLI_REFERENCE.md
│   ├── TOOL_SPEC.md
│   ├── TRIGGER_SPEC.md
│   ├── SECURITY.md
│   ├── concepts/
│   ├── tutorials/
│   └── adr/
├── agents/                     # BUNDLED EXAMPLE AGENTS
│   ├── README.md
│   ├── assistant/              # default interactive agent
│   ├── x-digest/               # MVP use case 1
│   └── portfolio/              # MVP use case 2
├── src/eonlet/                 # source code
│   ├── __init__.py
│   ├── cli/                    # CLI implementation
│   ├── worker/                 # worker process
│   ├── runtime/                # core runtime (event store, agent loop)
│   ├── tools/                  # builtin tools
│   ├── triggers/               # trigger system
│   ├── permissions/            # permission gate
│   └── (later) supervisor/     # v0.4+
└── tests/
    ├── unit/
    ├── integration/
    └── e2e/
```

## 5. Lifecycle: What happens when

### `eonlet init` creates

```
~/.eonlet/
├── config.yaml                 # populated with sensible defaults
├── agents/                     # empty initially
└── eonlets/                    # empty initially
```

Then prompts to install bundled agents (assistant, x-digest, portfolio). Each "install" copies from the package into `~/.eonlet/agents/<type>/`.

### `eonlet create <type> --name=<name>` creates

```
~/.eonlet/eonlets/<type>.<name>/
├── meta.json                   ← CLI writes
├── memory/                     ← CLI creates empty
│   └── notes.md                ← touch
├── workspace/                  ← CLI creates empty
├── logs/                       ← CLI creates empty
└── status                      ← worker writes "running" after start
```

Then CLI forks worker. Worker:
- Opens `state.db` (creates if absent, runs migrations)
- Binds `runtime.sock`
- Writes `pid` and starts `heartbeat` task
- Sets `status` to `running`
- Enters main loop

### `eonlet pause <id>`

CLI sends SIGSTOP. Worker is frozen in place. CLI writes `status=paused`.

### `eonlet resume <id>`

CLI sends SIGCONT. Worker resumes. CLI writes `status=running`.

### `eonlet kill <id>`

CLI sends SIGTERM. Worker has 5s to:
- Flush events
- Close DB cleanly
- Delete `runtime.sock`
- Write `status=dead`
- Exit

If exit doesn't happen in 5s, CLI sends SIGKILL and writes `status=dead` itself.

### `eonlet rm <id>`

Refuses if status != dead. With `--with-data` flag, removes the entire directory; without, removes only the metadata files and keeps `memory/` and `workspace/` for the user to retrieve.

## 6. Global Config: `~/.eonlet/config.yaml`

```yaml
defaults:
  model: claude-sonnet-4-6
  budget:
    daily_usd: 5.0
  permissions:
    mode: ask

providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY
  openai:
    api_key_env: OPENAI_API_KEY
    base_url_env: OPENAI_BASE_URL          # for Ollama, vLLM, etc.

paths:
  agents_dir: ~/.eonlet/agents
  eonlets_dir: ~/.eonlet/eonlets

editor: ${EDITOR:-vim}                     # for `eonlet def edit`

logging:
  level: info                              # debug | info | warn | error
  cli_log: ~/.eonlet/logs/cli.log
```

Settings here are **defaults**. Individual agent.yaml files override per-agent.

## 7. Environment Files (`.env`)

Eonlet supports `.env` files at three levels (highest precedence wins):

1. **Process environment** (`export X=...` in shell before `eonlet create`)
2. **Instance `.env`** (`~/.eonlet/eonlets/<id>/.env`)
3. **Definition `.env`** (`~/.eonlet/agents/<type>/.env`)

Use `.env.example` in definitions as a template; never commit real `.env` to git.

The definition's `.env` is useful for type-level defaults (e.g., `SMTP_HOST` is the same for all instances of that type). Instance `.env` overrides per-instance (different `EMAIL_TO` for two instances of `x-digest`).

## 8. What Eonlet Does NOT Touch

To make boundaries explicit:

- ❌ Anywhere outside `~/.eonlet/` (except tool calls the agent makes — those respect permissions)
- ❌ System paths (`/etc`, `/usr`, etc.)
- ❌ User home outside `.eonlet/` (`~/.ssh`, `~/.aws`, etc. — hardcoded deny)
- ❌ Network endpoints (unless via a `network: true` tool, gated by permissions)

This is the contract that makes Eonlet safe to run unattended scheduled agents on your daily-driver machine.
