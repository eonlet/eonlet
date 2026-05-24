# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Eonlet** is a local-first runtime for stateful AI agents — described as "the systemd for agents." It lets long-lived, autonomous agents persist state, accumulate memory, run on cron schedules, and be managed from the terminal like OS processes.

**Status: Pre-alpha, v0.0.6 landed.** Memory subsystem complete. v0.1.0 blocked only on non-engineering work (demo GIF, PyPI release, two weeks of dogfooding without a P0 bug).

Before writing or modifying code, **read the relevant spec** — the design is authoritative; the code follows it.

---

## Version History at a Glance

**v0.0.1 (Spine)** — event store, tool protocol, 10 offline builtins, Anthropic/OpenAI providers, agent loop, worker (anyio + Unix-socket JSON-RPC), permission gate, core CLI.

**v0.0.2** — cron scheduler (croniter + IANA tz, grace-period catch-up, ≥3-failure backoff), 16-slot `TriggerItem` queue, 3 network builtins (`web_search`/`web_fetch`/`send_email`), `eonlet fire`/`doctor`, `x-digest` template.

**v0.0.3** — debug + archive: IPCClient demuxer, `ps`/`tail`/`replay`/`export`/`import`, portfolio template (third bundled agent).

**v0.0.4** — LLM streaming: `LLMProvider.stream()` for Anthropic + OpenAI; `AgentRuntime.on_delta` callback; CLI `attach` prints token deltas inline. Token deltas are notifications, never events (SPEC §8.1).

**v0.0.5** — quality sprint (v0.1.0 SPEC §12 targets met):
- **`FakeProvider`** (`fake-echo`, `fake-tool-then-text`) — deterministic in-process LLM for tests; no API key required.
- **Worker integration tests** — `test_worker_subprocess.py` (real OS process) + `test_worker_inprocess.py` (pytest-cov-friendly, same `run_worker()` path).
- **LLM provider unit tests** — monkeypatched `_FakeAnthropicMessages` and `_FakeOAICompletions`, testing real provider code including chunked tool_call JSON reassembly.
- **mypy strict — zero errors**. SDK-adapter providers exempt from `union-attr`/`arg-type`/`attr-defined`/`call-overload`; croniter/apsw/msgpack/yaml have `ignore_missing_imports`.
- **Ruff strict — zero errors, formatter clean**.
- **CI gate: `--cov-fail-under=70`**. Measured 72.6% branch coverage, 89 tests.

**v0.0.6** — full memory subsystem (MEMORY_SPEC P1–P6, ADR-0003):
- **P1** — `src/eonlet/memory/` package: `MemoryConfig`, `STMStore` (short_term.md), `LTMStore` (long_term.md), atomic writes + file locking, watermark tracking. New events: `mem_compacted`, `mem_ltm_promoted`, `mem_ltm_forgotten`, `mem_note_*`, `mem_todo_*`, `mem_remember`, `mem_recall_invoked`, `mem_paused`, `mem_resumed`.
- **P2** — `NotesStore` (notes.md YAML-frontmatter entries) + `TodosStore` (todos.jsonl), `note` tool (add/get/list/delete), `todo` tool (add/done/cancel/list). Legacy `notes_read`/`notes_append` removed.
- **P3** — `RecallIndex` (SQLite FTS5 over event log + memory docs), `recall` tool (keyword + date + category filters).
- **P4** — Tier-1 compaction: LLM-driven working→STM; `LLMCompactor`; context injection preamble (working + STM + LTM + notes + todos); `/compact` slash command; worker cascade hooks.
- **P5** — Tier-2 (STM→LTM promotion) + Tier-3 (LTM forgetting); `remember` tool (`src:explicit` bullets); `forget` tool (dry-run + confirmed delete); `memory compact_ltm` action; full cascade: tier1 → tier2 → tier3.
- **P6** — `eonlet memory migrate <legacy_dir>` CLI command migrates Claude Code MEMORY.md files into LTM; `AGENT_CONFIG_SPEC.md` §8 rewritten; `CLI_REFERENCE.md` updated.
- **Test coverage**: 56 new unit tests across `tests/unit/memory/` (21 test files). Total: 145+ tests.

**v0.1.0 still owes** (non-engineering):
- 30-second README demo GIF.
- PyPI release (`uv build`/`uv publish`, version tag, changelog).
- Two weeks of author dogfooding without a P0 bug.
- All v0.2+ items per ROADMAP (MCP, hooks, vector memory, TUI, hibernate) remain deferred.

---

## Key Documents (read before non-trivial work)

| Document | Purpose |
|---|---|
| `docs/SPEC.md` | Master technical spec — all subsystems, principles, two-process model |
| `docs/AGENT_CONFIG_SPEC.md` | Every field of `agent.yaml` (normative) |
| `docs/TOOL_SPEC.md` | Tool protocol + builtin tool catalog |
| `docs/TRIGGER_SPEC.md` | Cron, interactive, and event triggers |
| `docs/MEMORY_SPEC.md` | Memory subsystem: storage, tiers, compaction, FTS5 recall |
| `docs/DIRECTORY_LAYOUT.md` | Runtime filesystem layout (`~/.eonlet/`) |
| `docs/SECURITY.md` | Permission model + threat model |
| `docs/CLI_REFERENCE.md` | All CLI commands including memory subcommands |
| `docs/adr/` | Architecture decisions — **propose a new ADR before any architectural change** |
| `ROADMAP.md` | Version gates and feature assignments (v0.1 MVP vs v0.2/0.3/0.4) |
| `MANIFESTO.md` | North star: specialist agents → teams → organizations |

`src/eonlet/README.md` mirrors the planned package layout and implementation order. Consult it to know which module a new piece of code belongs in and which features are explicitly out of scope for v0.1.

---

## Architecture at a Glance

### Two-Process Model

Every running agent is two OS processes:

- **`eonlet` (CLI)** — `src/eonlet/cli/` — user-facing. Spawns workers, attaches/detaches via Unix sockets, manages definitions. Console script: `eonlet = eonlet.cli.main:cli_main`. The Typer app object is `eonlet.cli.main:app`.
- **`eonlet-worker`** — `src/eonlet/worker/` — one long-lived OS process per agent. Owns the agent loop, IPC socket, and SQLite event store. Entry point: `eonlet.worker.main:main`.

### Worker Internals (four concurrent anyio tasks)

```
WorkerProcess
├── serve_ipc         — Unix socket JSON-RPC server (worker/ipc.py)
├── heartbeat_loop    — writes heartbeat every 10 s (worker/lifecycle.py)
├── trigger_scheduler — fires cron triggers (triggers/scheduler.py)
└── main_loop         — consumes TriggerItem queue → AgentRuntime
```

### Event-Sourced State

Every state change is an immutable append to a per-agent SQLite log. `AgentState` is rebuilt by replaying events — no mutable in-memory state. `EventKind` has 37 variants covering: conversation turns, tool calls, permissions, triggers, budget, sessions, errors, and all memory operations.

```
runtime/store.py   → SQLite append-only log
runtime/events.py  → EventKind enum (37 variants), Event model
runtime/state.py   → AgentState (replay-derived)
runtime/agent.py   → AgentRuntime (orchestrates LLM calls, tool execution, permission gates)
```

### Agent Definition Layout (on disk)

```
<agent_dir>/
├── agent.yaml       # config + triggers + permissions + metadata (normative per AGENT_CONFIG_SPEC)
├── system.md        # system prompt
├── tools/*.py       # custom Python tools (imported by runtime/definition.py)
├── skills/*.md      # skills loaded into context on-demand via load_skill tool
└── prompts/         # optional agent-specific prompt fragments
```

Builtin tools live in `tools/builtin/` and self-register via `@tool` through `tools/registry.py` at import time.

The three reference agent templates in `src/eonlet/templates/` (`assistant`, `x-digest`, `portfolio`) are both **usage examples** and **canonical fixtures** against which the runtime is designed. When in doubt about a config field's shape, check how these agents use it.

### Memory Subsystem (three storage files + three compaction tiers)

Per-agent memory lives under `~/.eonlet/eonlets/<id>/memory/`:

```
working_memory.md   → recent conversation context (in-process buffer)
short_term.md       → STM: dated sections (tier-1 compaction target)
long_term.md        → LTM: categorized bullets with src/ts (tier-2 target)
notes.md            → user-curated notes; YAML frontmatter; never auto-deleted
todos.jsonl         → action items with status/due/priority
recall.db           → SQLite FTS5 index over event log + memory files
```

**Compaction cascade (tier1 → tier2 → tier3):**
1. **Tier-1** (`memory/tier1.py`): LLM-driven working memory → STM sections. Fires automatically when working memory exceeds budget.
2. **Tier-2** (`memory/tier2.py`): STM sections → LTM bullets when STM exceeds budget.
3. **Tier-3** (`memory/tier3.py`): LTM self-compaction (LLM-driven deletion) when LTM exceeds budget.

`memory/injection.py` injects the working + STM + LTM + notes + todos preamble into each LLM call.

---

## Package Layout

```
src/eonlet/
├── cli/                  — Typer CLI (main.py, commands.py, status.py, util.py)
├── config.py             — YAML config models (MemoryConfig, AgentConfig, TriggerConfig …)
├── errors.py             — Project exception hierarchy
├── paths.py              — Filesystem path helpers
├── llm/
│   ├── protocol.py       — LLMProvider abstract interface + stream()
│   ├── anthropic_provider.py
│   ├── openai_provider.py
│   ├── fake_provider.py  — Deterministic FakeProvider (fake-echo, fake-tool-then-text)
│   └── factory.py        — Provider selection by model prefix
├── memory/               — Full memory subsystem (v0.0.6)
│   ├── config.py         — MemoryConfig (budgets, enabled, compaction_model)
│   ├── stm.py            — STMStore (short_term.md sections)
│   ├── ltm.py            — LTMStore (long_term.md bullets)
│   ├── notes.py          — NotesStore (notes.md YAML-frontmatter)
│   ├── todos.py          — TodosStore (todos.jsonl)
│   ├── recall.py         — RecallIndex (SQLite FTS5 search)
│   ├── injection.py      — Context preamble injection
│   ├── compactor.py      — LLMCompactor (coordinates all tiers)
│   ├── tier1.py          — Tier-1 runner (working → STM)
│   ├── tier2.py          — Tier-2 runner (STM → LTM)
│   ├── tier3.py          — Tier-3 runner (LTM forgetting)
│   ├── migrate.py        — Claude Code memory migration
│   ├── storage.py        — atomic_write_text + file_lock
│   ├── watermark.py      — Watermark tracking
│   ├── tokens.py         — Token counting
│   ├── ids.py            — ID generation
│   └── paths.py          — Memory directory helpers
├── permissions/
│   └── gate.py           — Permission gate (ask / yolo modes + hardcoded deny list)
├── runtime/
│   ├── agent.py          — AgentRuntime (main loop, ~460 lines)
│   ├── definition.py     — Load agent.yaml + per-agent tools
│   ├── events.py         — EventKind (37 variants), Event model
│   ├── state.py          — AgentState (event-sourced)
│   └── store.py          — SQLite event store (append-only)
├── templates/            — Bundled example agents (config.yaml + 3 agent dirs)
│   ├── assistant/
│   ├── x-digest/
│   └── portfolio/
├── tools/
│   ├── protocol.py       — Tool interface, ToolContext, ToolResult
│   ├── registry.py       — Tool registration + @tool decorator
│   └── builtin/          — 13 modules, 21+ individual tools
│       ├── bash.py       — bash (shell execution)
│       ├── files.py      — file_read, file_write, file_edit, glob, grep
│       ├── web.py        — web_search (Tavily), web_fetch (httpx)
│       ├── email.py      — send_email
│       ├── sleep_tool.py — sleep
│       ├── skill_tool.py — load_skill
│       ├── schedule.py   — schedule (one-off future trigger)
│       ├── memory.py     — memory (compact / pause / resume)
│       ├── note.py       — note (add / get / list / delete)
│       ├── todo.py       — todo (add / done / cancel / list)
│       ├── recall.py     — recall (keyword + date + category search)
│       ├── remember.py   — remember (explicit LTM write)
│       └── forget.py     — forget (dry-run + confirmed LTM delete)
├── triggers/
│   ├── scheduler.py      — Cron scheduler (croniter + IANA tz, catch-up, backoff)
│   └── dynamic_store.py  — Persistent trigger state (last run, failure count)
└── worker/
    ├── main.py           — Worker entry point + _worker_main
    ├── ipc.py            — Unix socket JSON-RPC server + IPCClient demuxer
    └── lifecycle.py      — write_pid, write_status, write_heartbeat, read_meta
```

---

## Test Suite Structure

```
tests/
├── conftest.py                          — Shared pytest fixtures
├── integration/
│   ├── test_worker_inprocess.py         — run_worker() inside pytest (pytest-cov friendly)
│   └── test_worker_subprocess.py        — python -m eonlet.worker.main real OS process
└── unit/
    ├── memory/                          — 21 test files for memory subsystem
    │   ├── test_ltm.py                  — LTMStore CRUD + budgeting
    │   ├── test_stm.py                  — STMStore sections
    │   ├── test_notes_store.py          — NotesStore YAML frontmatter
    │   ├── test_todos_store.py          — TodosStore JSONL
    │   ├── test_recall_index.py         — RecallIndex FTS5 search
    │   ├── test_recall_tool.py          — recall tool behavior
    │   ├── test_remember_forget.py      — remember/forget tool pair
    │   ├── test_tier1.py               — Working → STM compaction
    │   ├── test_tier2.py               — STM → LTM promotion
    │   ├── test_tier3.py               — LTM forgetting
    │   ├── test_compactor.py           — Full compaction flow
    │   ├── test_agent_injection.py     — Full agent context injection
    │   ├── test_injection.py           — Preamble injection unit
    │   ├── test_tools_note_todo.py     — note + todo tool integration
    │   ├── test_migrate.py             — Claude Code memory migration
    │   ├── test_config.py              — MemoryConfig validation
    │   ├── test_events.py              — Memory-related events
    │   ├── test_storage.py             — Atomic file writes
    │   ├── test_watermark.py           — Watermark tracking
    │   └── test_paths.py              — Memory path helpers
    ├── test_event_store.py             — SQLite event store
    ├── test_providers.py               — Anthropic + OpenAI adapters
    ├── test_fake_provider.py           — FakeProvider determinism
    ├── test_definition.py              — Agent definition loading
    ├── test_tools_builtin.py           — Core builtin tools
    ├── test_tools_memory_builtin.py    — Memory tool surface
    ├── test_tools_schedule_builtin.py  — Schedule tool
    ├── test_web_tools.py               — web_search, web_fetch
    ├── test_email.py                   — send_email
    ├── test_scheduler.py               — Cron + catch-up + backoff
    ├── test_dynamic_store.py           — Dynamic trigger state
    ├── test_permissions.py             — Permission gate (ask/yolo)
    ├── test_streaming.py               — Token delta streaming
    ├── test_ipc_client.py              — IPC JSON-RPC client
    ├── test_lifecycle.py               — Worker startup/shutdown
    ├── test_replay.py                  — Event replay
    ├── test_export_import.py           — Agent export/import
    ├── test_status.py                  — Status formatting
    ├── test_cli_runner.py              — CLI command execution
    ├── test_cli_offline.py             — CLI without worker
    └── test_cli_commands_extra.py      — Additional CLI commands
```

**Current coverage: 145+ tests, ≥72.6% branch coverage. CI gate: `--cov-fail-under=70`.**

---

## Development Commands

The project uses **uv** (not pip/poetry/conda):

```bash
# One-time setup
uv venv
uv sync --dev          # install all deps including dev extras
pre-commit install

# Daily development
pytest                                           # full suite
pytest tests/unit/memory/test_ltm.py::test_name # single test
pytest tests/unit/ -k "recall"                  # filter by name
pytest --cov --cov-report=term-missing           # with coverage

# Quality gates (all must pass before committing)
ruff check .           # lint (broad rule set; see pyproject.toml)
ruff format .          # format (owns line length at 100)
mypy src               # strict-mode type check
```

Test configuration (`pyproject.toml`):
- `asyncio_mode = "auto"` — all async tests run without explicit markers
- `testpaths = ["tests"]`
- `--strict-markers` — no undeclared pytest marks
- Branch coverage over `src/eonlet`

---

## Coding Standards

These are project-specific rules that **override common Python defaults** — apply them without being asked:

| Rule | Detail |
|---|---|
| **Python version** | ≥ 3.11; `from __future__ import annotations` at top of every module |
| **Async** | Use **anyio** everywhere. Never `asyncio.run` — use `anyio.run`. No raw `asyncio` primitives |
| **Logging** | **structlog** only. No `print()` anywhere. CLI output goes through **rich** |
| **Errors** | No bare `except Exception`. Raise from the project exception hierarchy in `errors.py` |
| **Types** | Annotations on all public APIs. Mypy runs in strict mode (`disallow_any_unimported`, `warn_return_any`) |
| **Lint** | Ruff rule set: `E,F,W,I,N,UP,B,C4,SIM,RET,PTH,ASYNC,RUF`. `E501` disabled (formatter owns line length) |
| **Dependencies** | Minimal. Never add langchain, transformers, or heavy ML frameworks (see `CONTRIBUTING.md`) |

**mypy overrides to know about:**
- `eonlet.llm.anthropic_provider` and `eonlet.llm.openai_provider` have `union-attr`/`arg-type`/`attr-defined`/`call-overload` disabled — SDK union types are too rich to narrow exhaustively.
- `src/eonlet/templates/` is excluded from mypy entirely (user-facing example code).
- `croniter`, `apsw`, `msgpack`, `yaml` have `ignore_missing_imports`.

**Ruff per-file ignores:**
- `src/eonlet/cli/main.py`: `B008` (Typer's canonical `typer.Option(...)` in defaults)
- `src/eonlet/config.py`: `N815` (`apiVersion` mixedCase matches YAML field)
- `tests/**`: `ASYNC110`, `ASYNC240`, `SIM115`, `SIM117`, `PTH111`, `PTH123`

---

## Working with Agent Definitions

When editing or adding example agents under `src/eonlet/templates/`:

- **Fields in `agent.yaml` are normative** — validate against `docs/AGENT_CONFIG_SPEC.md` rather than inventing new keys.
- **Custom tools** follow the protocol in `docs/TOOL_SPEC.md` and are imported per-agent by `runtime/definition.py`. Builtins self-register through `tools/registry.py` at import time.
- **`.env.example` files** document required secrets — never commit a real `.env`.
- **`metadata.specialty` and `metadata.capabilities`** look optional but are deliberately forward-compatible with the future team-formation system (Phase C); preserve them when restructuring.
- The `assistant`, `x-digest`, and `portfolio` templates are the canonical design fixtures — check them when a config field's intended shape is unclear.

---

## Key Invariants (don't break these)

1. **Events are append-only.** Never update or delete rows in the event store. State is derived by replay.
2. **Token deltas are notifications, not events.** `ASSISTANT_TOKEN_DELTA` is never stored in SQLite (SPEC §8.1).
3. **Memory files use atomic writes.** Always use `storage.atomic_write_text()` to avoid partial writes — never open memory files with plain `open(..., "w")`.
4. **Tool registration is automatic.** Importing `eonlet.tools.builtin` registers all builtins. Never manually call `registry.register()` for builtins.
5. **anyio, not asyncio.** Every async primitive must be from `anyio` or `anyio.abc`. Mixed usage breaks the backend abstraction.
6. **No supervisor in v0.1.** The CLI directly manages worker processes. `eonletd` is a v0.4+ concern (ADR-0001).
7. **Memory package is fully implemented.** `src/eonlet/memory/` is not a placeholder — it is the complete v0.0.6 memory subsystem with 16 modules and 56 unit tests.

---

## Out of Scope for v0.1

Per `src/eonlet/README.md` and ROADMAP:

- MCP client integration (v0.2)
- Semantic / vector memory (v0.2, will live alongside but not replace `src/eonlet/memory/`)
- Hooks (`pre_tool_use`, `post_tool_use`, `on_error`) (v0.2)
- Textual TUI for `eonlet attach` (v0.2)
- Agent hibernation / resume from disk (v0.2)
- OpenTelemetry tracing (v0.2)
- `eonletd` supervisor daemon (v0.4)
- Inter-eonlet messaging (v0.5)
- Teams and organizations (v0.6+)
- A2A protocol compatibility (v0.5)

Do **not** add these features unless explicitly asked, regardless of how naturally they seem to fit.
