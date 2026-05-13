# Eonlet — Product Specification

| Field | Value |
|---|---|
| Project | **Eonlet** |
| Tagline | Agents that live for ages |
| Spec version | 0.3.0 (DRAFT) |
| Status | Pre-implementation |
| License | Apache-2.0 |
| Language | Python ≥ 3.11 |
| Target platforms | macOS (≥14), Linux (glibc ≥ 2.35), Windows via WSL2 |

---

## 0. Reader Guide

This is the master spec — the source of truth for *what Eonlet is*. It is dense by intent. If you want a gentler entry:

- **First-time visitor** → [`README.md`](../README.md), [`MANIFESTO.md`](../MANIFESTO.md)
- **Building an agent** → [`AGENT_CONFIG_SPEC.md`](AGENT_CONFIG_SPEC.md), then the example agents in [`agents/`](../agents/)
- **Using the CLI** → [`CLI_REFERENCE.md`](CLI_REFERENCE.md)
- **Writing tools** → [`TOOL_SPEC.md`](TOOL_SPEC.md)
- **Understanding triggers** → [`TRIGGER_SPEC.md`](TRIGGER_SPEC.md)

---

## 1. Vision

Eonlet is a **local-first runtime for stateful AI agents**. Each agent runs as a long-lived OS process (an *eonlet*) with its own event log, memory, and Unix socket. Definitions live as YAML and Markdown on disk; the CLI spawns, attaches, schedules, and stops them.

**Positioning:** *not* an agent framework (you write the logic), *not* a hosted SaaS (we don't run it for you), *not* a workflow orchestrator (we don't dictate flow). Eonlet is the **runtime** — the OS-level scaffolding that lets agents live for ages.

**Long-term direction (Phases C/D):** Eonlet is built toward a model of **specialist agents organized into teams and organizations**. Each eonlet has a specialty, not all skills. Specialists form teams with leaders. Teams form trees of teams (organizations). This is documented in [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md). MVP does *not* build any of this, but MVP design decisions keep it open: stable eonlet IDs, A2A as substrate, forward-compatible `specialty`/`capabilities` metadata fields.

### 1.1 Core principles

1. **Local-first** — default home is your machine, cloud is optional.
2. **Filesystem-first** — definitions are YAML+Markdown; git-friendly, reviewable, hot-reloadable.
3. **Terminal-native** — CLI and TUI are first-class; web UI is a future bridge.
4. **Process-isolated** — one eonlet, one OS process; crashes don't propagate.
5. **Event-sourced** — every state change is an immutable event; `state = fold(events)`.
6. **Two modes, one model** — *interactive* and *scheduled* eonlets share the same runtime.
7. **Configuration over code** — YAML and Markdown define behavior; Python is the escape hatch.
8. **Dogfood-first** — author lives in it before release.

---

## 2. The Two Modes

A core insight from the MVP use cases is that eonlets operate in two distinct modes that share the same architecture:

### Interactive eonlets
- Triggered by user `attach` + chat
- Mostly waiting for user input
- Response goes back to user via attached session
- Example: `assistant`

### Scheduled eonlets
- Triggered by cron schedule (or other event source)
- Mostly idle between triggers
- Output goes to declared sinks (email, file, future: webhook)
- User can `attach` for inspection / ad-hoc queries
- Example: `x-digest`, `portfolio`

**Both modes use the same worker process, the same event store, the same tools.** The only difference is the `triggers` section in `agent.yaml`.

---

## 3. Goals & Non-Goals

### 3.1 MVP Goals (v0.1.0)

- **M1** User defines agents as YAML + Markdown + optional Python tool files.
- **M2** `pip install eonlet` works on macOS and Linux.
- **M3** Single machine hosts ≥ 5 eonlets concurrently (mostly idle).
- **M4** Eonlet state survives restart (event-sourced restore).
- **M5** Scheduled triggers fire reliably, autonomous execution succeeds without user attached.
- **M6** Custom Python tools load from `tools/` directory without code change to runtime.
- **M7** Three production-quality bundled agents demonstrate the model:
  `assistant` (interactive), `x-digest` (scheduled, simple), `portfolio` (scheduled, complex).
- **M8** Author dogfoods two weeks without P0 bug.

### 3.2 Phase B Goals (v0.4–v0.5) — Multi-Eonlet Substrate

- Multi-eonlet discovery and messaging (peer-to-peer, unstructured)
- A2A protocol support
- Capability registry — `specialty` and `capabilities` fields become live

### 3.3 Phase C Goals (v0.6–v0.7) — Teams

- Team primitive: leader + members, declared in `~/.eonlet/teams/<name>/team.yaml`
- Leader-member message envelopes; common patterns (Lead-Worker, Pipeline, Critic-Producer) supported
- Shared team memory
- Team-level budget and audit

### 3.4 Phase D Goals (v0.8–v0.9) — Organizations

- Organization primitive: tree of teams
- Cross-team routing through common ancestor
- Federation across machines

See [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md) for the conceptual model behind Phase C/D.

### 3.5 Non-Goals

- **NG1** Not an agent programming framework (use LangGraph / smolagents / Pydantic AI for that)
- **NG2** Not model training or fine-tuning
- **NG3** Not a web UI as primary interface (HTTP bridge for third parties later)
- **NG4** Not native Windows (WSL2 only)
- **NG5** Not multi-tenant SaaS
- **NG6** Not GPU inference or local model loading (use OpenAI-compatible endpoints)
- **NG7** MVP does **not** include `eonletd` supervisor — CLI directly manages workers
- **NG8** MVP does **not** include multi-eonlet communication
- **NG9** MVP does **not** include textual TUI (plain CLI attach is sufficient)
- **NG10** MVP does **not** include hibernate (pause/resume via SIGSTOP/SIGCONT suffices)
- **NG11** MVP does **not** include MCP integration (v0.2)
- **NG12** MVP does **not** include vector memory (v0.2)

---

## 4. Three-Layer Architecture

```
Definition  (template, filesystem, immutable)
    │
    │ eonlet create
    ▼
Eonlet      (OS process, persistent state, event-sourced)
    │
    │ eonlet attach
    ▼
Session     (client connection, ephemeral)
```

- **Definition** — `~/.eonlet/agents/<type>/` directory containing `agent.yaml`, `system.md`, `tools/`, `skills/`, `prompts/`, `mcp.json` (v0.2+).
- **Eonlet** — `~/.eonlet/eonlets/<id>/` directory containing state, memory, and runtime files. One OS process per eonlet.
- **Session** — A client connection to an eonlet's `runtime.sock`. Multiple sessions can attach concurrently (one master, others read-only).

See [`DIRECTORY_LAYOUT.md`](DIRECTORY_LAYOUT.md) for the complete directory structure.

---

## 5. Process Model

### 5.1 MVP topology — no supervisor

```
┌──────────────────┐  fork+exec   ┌─────────────────┐
│  eonlet (CLI)    │ ───────────► │  eonlet-worker  │
│  create / ls /   │              │  (per eonlet)   │
│  kill / attach   │ ◄─runtime.sock                 │
└──────────────────┘              └────────┬────────┘
                                           │
                              ~/.eonlet/eonlets/<id>/
                              ├── pid          ← worker writes
                              ├── status       ← worker writes
                              ├── heartbeat    ← worker writes every 10s
                              ├── runtime.sock ← worker binds
                              └── state.db     ← worker reads/writes
```

`eonlet ls` works by **scanning the filesystem**: read `pid`, `status`, `heartbeat`; check process liveness with `kill -0`. No daemon needed.

### 5.2 Phase B topology — with supervisor

```
                    ┌────────────────┐
                    │   eonletd      │ ── routes inter-eonlet messages,
                    │  (supervisor)  │    runs A2A protocol, manages
                    └───┬──────┬─────┘    auto-restart, exposes registry
                        │      │
                   ┌────▼──┐ ┌─▼─────┐
                   │worker1│ │worker2│
                   └───────┘ └───────┘
```

Introduced in v0.4. Designed so v0.3 users can opt-in without breaking changes.

---

## 6. Triggers (MVP)

Triggers are how eonlets *start doing things*. Every eonlet has at least an implicit *interactive trigger* (user attaches and sends a message). Additional triggers are declared in `agent.yaml`.

### MVP supported trigger kinds

- **`cron`** — fires on cron schedule with timezone support
- **`interactive`** — implicit; user attaches and sends message
- **(v0.2)** `webhook` — HTTP endpoint fires the eonlet
- **(v0.2)** `file_watch` — filesystem path changes fire the eonlet
- **(v0.4)** `peer_message` — another eonlet sends a message

When a trigger fires, the worker injects a `<trigger>` block into the conversation as a user-role message:

```xml
<trigger kind="cron" id="daily_digest" fired_at="2026-05-12T08:00:00+09:00">
  Last successful run: 2026-05-11T08:01:23+09:00
  Message: It's time for the daily digest. Fetch tweets since your last run,
           group by theme, write to workspace, and email me.
</trigger>
```

The agent's `system.md` is written to recognize and act on these trigger blocks. See [`TRIGGER_SPEC.md`](TRIGGER_SPEC.md) for details.

---

## 7. Core Components

For each component, the spec defines MVP scope and notes deferred capabilities.

### 7.1 CLI (`eonlet`) — MVP

Responsibilities:
- Definition management (`def ls`, `def init`, `def validate`)
- Eonlet lifecycle (`create`, `ls`, `pause`, `resume`, `kill`, `rm`)
- Interaction (`attach`, `send`, `logs`, `inspect`)
- System (`init`, `version`, `doctor`)

See [`CLI_REFERENCE.md`](CLI_REFERENCE.md) for the full command surface.

Implementation: thin client. All logic lives in the worker. CLI commands either send signals (`pause`, `kill`), read filesystem state (`ls`, `inspect`), or connect to `runtime.sock` (`attach`, `send`).

### 7.2 Worker (`eonlet-worker`) — MVP

Per-eonlet process. Runs:

```python
async def main():
    cfg = load_definition_and_state()
    runtime = AgentRuntime(cfg)
    
    async with TaskGroup() as tg:
        tg.start(serve_runtime_socket)   # client RPC
        tg.start(heartbeat_loop)         # write heartbeat every 10s
        tg.start(trigger_scheduler)      # fire cron triggers
        tg.start(main_loop)              # process trigger queue
```

The four tasks communicate via `anyio.MemoryObjectStream`s. Critical: **`serve_runtime_socket` must never block on `main_loop`** — clients must be able to attach mid-LLM-call.

### 7.3 Trigger Scheduler — MVP

Runs inside the worker. Implementation:

```python
# Pseudocode
async def trigger_scheduler():
    for trigger in cfg.triggers:
        if trigger.kind == "cron":
            schedule.add(croniter(trigger.schedule, tz=trigger.timezone))
    
    while not stopped:
        next_fire = schedule.next()
        await anyio.sleep_until(next_fire)
        await trigger_queue.send(make_trigger_event(...))
```

Triggers go into a queue that the main loop reads. Main loop interleaves trigger events with user messages (when a session is attached).

### 7.4 Event Store — MVP

SQLite + WAL2 + apsw. Schema in [`AGENT_CONFIG_SPEC.md`](AGENT_CONFIG_SPEC.md#appendix-event-store-schema).

Operations:
- `append(event) -> id` — single writer (the worker main loop)
- `read(since=cursor, limit=N)` — for streaming to clients
- `restore() -> AgentState` — full replay on worker startup

MVP performance target: ≥ 1000 events/sec append; restore 100k events ≤ 5s.

### 7.5 Memory — MVP (simplified)

Three forms:

- **Working** (in-context messages) — last N messages + recent tool results
- **Episodic** (event store) — full history queryable by `events.replay`
- **Procedural / Notes** (filesystem) — `memory/notes.md`, `memory/todo.md`, `memory/<custom>.md` — agent reads/writes via builtin tools, user can edit by hand

MVP **does not** include semantic / vector memory (v0.2 with sqlite-vec) or compaction (v0.3).

### 7.6 Tool System — MVP

Tools are the agent's effector interface. See [`TOOL_SPEC.md`](TOOL_SPEC.md) for the full interface.

Sources (loader priority):
1. **Builtin** — shipped with Eonlet (~12 tools, see below)
2. **Custom** — Python files in the agent definition's `tools/` directory

Custom tools are loaded by importing each `.py` file in `tools/` and discovering classes decorated with `@tool`. Each must implement the `Tool` protocol.

Builtin tools (MVP):
- `bash`, `file_read`, `file_write`, `file_edit`, `glob`, `grep`
- `web_search`, `web_fetch`
- `notes_read`, `notes_append`
- `send_email`
- `sleep`

Phase B: tools sourced from MCP servers.

### 7.7 Permission System — MVP (simplified)

Two modes:

- **`ask`** — destructive tool calls prompt the user via attached session; auto-denied if no session attached
- **`yolo`** — all tool calls auto-allowed, except hardcoded deny list

Hardcoded deny list (always enforced, regardless of mode):

```
Bash(rm -rf /*)         Bash(sudo*)
Bash(rm -rf ~*)         Bash(curl * | sh)
Bash(:(){*)             Bash(wget * | sh)
FileWrite(/etc/**)      FileWrite(~/.ssh/**)
FileWrite(~/.aws/**)    FileWrite(~/.eonlet/**)
```

Scheduled eonlets typically run `mode: yolo` since no user is attached.

Phase B: pattern-based allow/deny, `read_only` and `plan` modes, hook-based custom gates.

### 7.8 Hook System — v0.2

Not in MVP.

### 7.9 Compaction Engine — partial in MVP

MVP implements only L1 (tool output truncation > 25k tokens) and L2 (old tool result clearing). Full 5-layer compaction lands in v0.3.

### 7.10 Skill System — MVP (simplified)

Skills are Markdown files in the agent definition's `skills/` directory. They're discovered at startup and **listed in the system prompt** with a one-line description. The agent decides when to load a skill's full body using the `load_skill` builtin tool.

Example:
```
~/.eonlet/agents/portfolio/skills/
├── technical_analysis.md
└── fundamental_analysis.md
```

System prompt automatically gets:
```
## Available Skills
- technical_analysis — How to do technical analysis with chart patterns and indicators
- fundamental_analysis — How to evaluate companies from financial statements
```

Agent calls `load_skill(name="technical_analysis")` when relevant.

---

## 8. Protocols

### 8.1 Runtime plane (client ↔ eonlet) — MVP

JSON-RPC 2.0 over Unix socket at `~/.eonlet/eonlets/<id>/runtime.sock`, with server-initiated event stream.

**Client → eonlet methods:**

| Method | Params | Returns |
|---|---|---|
| `session.start` | `{client_id, since_event?}` | `{session_id, state}` |
| `session.end` | `{session_id}` | `{ok}` |
| `message.send` | `{content}` | `{event_id}` |
| `message.interrupt` | – | `{ok}` |
| `state.get` | – | `{...}` |
| `events.replay` | `{from, to?}` | `[events]` |
| `trigger.fire` | `{trigger_id, payload?}` | `{event_id}` |

**Eonlet → client notifications:**

| Notification | Payload |
|---|---|
| `event` | full event object |
| `token_delta` | `{delta_text}` |
| `tool_use_started` | `{tool_call_id, tool_name, args}` |
| `tool_use_finished` | `{tool_call_id, ok, output}` |
| `state_changed` | `{new_status}` |
| `trigger_fired` | `{trigger_id, fired_at}` |

### 8.2 Control plane — Phase B

Not in MVP.

### 8.3 MCP integration — v0.2

Not in MVP.

---

## 9. Data Models

See [`AGENT_CONFIG_SPEC.md`](AGENT_CONFIG_SPEC.md) for `agent.yaml` schema and event schema.

See [`DIRECTORY_LAYOUT.md`](DIRECTORY_LAYOUT.md) for all directory and file layouts.

---

## 10. Security Model

See [`SECURITY.md`](SECURITY.md) for threat model and defenses.

MVP highlights:
- Hardcoded deny patterns enforced regardless of mode
- Tool outputs tagged in prompt as untrusted
- Environment variable secret management; no secrets in definitions
- Workspace path isolation for file operations

---

## 11. Observability — MVP (minimal)

- **Logs:** `structlog` writes to `logs/current.log`, rotates at 50MB × 3
- **Metrics:** none in MVP (v0.2)
- **Traces:** none in MVP (v0.2 with Logfire / OTel)

---

## 12. Testing Strategy

| Layer | Scope | Tools |
|---|---|---|
| Unit | Pure functions: reducer, parser, permission gate | pytest |
| Component | Single component: event store, CLI commands | pytest + tmp SQLite |
| Integration | Worker full flow, LLM mocked | pytest + pytest-recording |
| E2E | Real LLM, real sockets, budget-limited | pytest, slow-tagged, nightly CI |

Invariants verified every PR:
- **I1** `append → restore` round-trip equality (property-based)
- **I2** Worker SIGKILL → restart → no data loss past last user message
- **I3** Hardcoded deny patterns cannot be bypassed
- **I4** Detach → worker still alive, heartbeat updating
- **I5** `eonlet ls` returns in < 100ms with 100 eonlets present

Coverage target: ≥ 70% overall; ≥ 90% for event store, main loop, IPC, permission.

---

## 13. MVP Definition (v0.1.0 release scope)

This section is the line in the sand. Anything not listed here is **not** in v0.1.0.

### 13.1 Architecture

- ✅ Single-eonlet operation
- ✅ CLI directly manages workers (no supervisor)
- ✅ Unix socket runtime IPC
- ✅ Event-sourced SQLite store
- ✅ PID + heartbeat + status files for `eonlet ls`
- ✅ SIGSTOP/SIGCONT pause/resume
- ✅ SIGTERM graceful kill

### 13.2 Definitions

- ✅ `agent.yaml` (MVP schema)
- ✅ `system.md` system prompt
- ✅ Custom tools (Python files in `tools/`)
- ✅ Skills (Markdown files in `skills/`)
- ✅ Environment variable declaration and validation
- ✅ Three bundled examples: `assistant`, `x-digest`, `portfolio`

### 13.3 Triggers

- ✅ `interactive` (implicit, on attach)
- ✅ `cron` with timezone support

### 13.4 Tools (12 builtins)

`bash`, `file_read`, `file_write`, `file_edit`, `glob`, `grep`, `web_search`, `web_fetch`, `notes_read`, `notes_append`, `send_email`, `sleep`, `load_skill`.

### 13.5 Models

- Anthropic API
- OpenAI API (and any OpenAI-compatible endpoint: Ollama, vLLM, etc.)

### 13.6 Memory

- Episodic (event store replay)
- Procedural (notes.md / todo.md / custom Markdown)
- Simple working memory truncation

### 13.7 Permissions

- `ask` mode, `yolo` mode
- Hardcoded deny list

### 13.8 CLI

`init`, `version`, `doctor`, `def ls`, `def init`, `def validate`, `def show`, `create`, `ls`, `ps`, `pause`, `resume`, `kill`, `rm`, `attach`, `send`, `logs`, `inspect`.

### 13.9 Distribution

- `pip install eonlet` (PyPI)
- macOS + Linux tested in CI
- README with 5-line quickstart + 30-second demo GIF
- `docs/` complete

### 13.10 Done verification (8 checks)

In order:

1. **Installs cleanly** on fresh macOS and fresh Ubuntu.
2. **Quickstart works** — 5 lines, < 5 min to first reply.
3. **Survives restart** — create eonlet, talk, kill terminal, reopen, attach → history intact.
4. **Detach is clean** — Ctrl+B D leaves worker alive; reattach continues.
5. **Pause works** — `eonlet pause` puts process in `T` state; resume responds instantly.
6. **Kill is graceful** — `eonlet kill` exits within 5s; `eonlet rm` cleans up.
7. **No data loss** — SIGKILL the worker; restart; state up to last user message preserved.
8. **Two weeks dogfood** — author uses Eonlet instead of Claude Code for 14 consecutive days, P0 bugs all fixed.

When 8/8 ✅ → v0.1.0 ships to PyPI.

---

## 14. Release & Versioning

See [`ROADMAP.md`](../ROADMAP.md) for the full release plan.

Versioning: `0.MAJOR.MINOR` until 1.0; SemVer strictly enforced after.

API compatibility:
- 0.x: each minor MAY break the schema. Provide migration scripts.
- 1.0+: schema evolves via `apiVersion` field; old definitions continue to work.

---

## 15. Coding Standards

- Python ≥ 3.11; `from __future__ import annotations` everywhere
- Type annotations on all public APIs (mypy strict)
- Lint: ruff (with isort); format: black-equivalent (`ruff format`)
- Pre-commit: ruff + mypy + conventional commits
- Docstrings: Google style; required on public APIs
- Errors: custom hierarchy rooted at `EonletError`; never raise plain `Exception`
- Async: `anyio`, not raw asyncio; never `asyncio.run` (use `anyio.run`)
- Logging: `structlog`, never `print`; CLI output via `rich`
- No `from x import *`
- Package management: `uv` only

---

## 16. Open Questions

### 16.1 MVP must resolve

- **Q1** subprocess vs. multiprocessing for worker spawn? *Lean subprocess (cleaner, independent venv possible).*
- **Q2** Who cleans up stale socket/pid? *CLI on `ls` does lazy cleanup.*
- **Q3** `prompt_toolkit` or readline for `attach`? *prompt_toolkit (streaming output handling).*
- **Q4** Default model? *`claude-sonnet-4-6` (balance of cost / latency / capability).*
- **Q5** How does the scheduler survive a worker restart that overlaps a scheduled fire? *On restart, check `last_fired` per trigger in event store; if missed window > grace period, log and skip; if within grace, fire once.*
- **Q6** Where does `.env` file live? *`agents/<type>/.env` for type defaults, `eonlets/<id>/.env` for instance override (instance wins).*

### 16.2 Phase A later versions

- **Q7** Skill loading: full file vs. summary on demand? *Summary in system prompt, full body via `load_skill` tool.*
- **Q8** TUI panel layout (v0.2)?
- **Q9** Compaction triggers — auto vs. user-controlled?

### 16.3 Phase B (v0.4+)

- **Q10** Supervisor opt-in path without breaking v0.3 users
- **Q11** Sandbox runtime selection (subprocess+seccomp / Docker / E2B)
- **Q12** Federation discovery (mDNS / peer list)

---

## 17. References

Implementations to study (priority order):

1. **Letta** — closest competitor; their wrong-for-us choices clarify our right ones
2. **Claude Code** reverse engineering (arXiv:2604.14228) — 5-layer compaction, permission system
3. **OpenHands SDK v1** (arXiv:2511.03690) — event sourcing, immutable config
4. **MCP SDK** — protocol reference
5. **smolagents** — minimal agent loop (~1k lines)
6. **OpenAI Agents SDK** — handoff, guardrail, tracing
7. **tmux** `server.c` / `client.c` — detach/attach IPC
8. **systemd** unit files — declarative service spec
9. **Erlang OTP** — supervisor tree (Phase B)

---

## Appendix A: Term Glossary

| Term | Definition |
|---|---|
| **Eonlet** | (1) The project. (2) One agent instance — an OS process with persistent state. |
| **Definition** | A directory under `~/.eonlet/agents/<type>/` describing an agent type. |
| **Session** | A client connection to an eonlet's runtime socket. |
| **Worker** | The per-eonlet process binary (`eonlet-worker`). |
| **Supervisor** | The daemon binary `eonletd`, introduced in v0.4. |
| **Event** | An immutable record of a state change. |
| **Trigger** | What causes an eonlet to start processing — cron, interactive, etc. |
| **Tool** | A capability available to the agent (file, web, email, custom). |
| **Skill** | A Markdown reference loaded on-demand via the `load_skill` tool. |
| **Workspace** | The eonlet's private working directory at `eonlets/<id>/workspace/`. |
| **Hibernate** | (v0.2+) Serialize state to disk and exit; restore by replay. |

---

**Spec ends. Implementation begins.**
