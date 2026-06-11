# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Eonlet** is a local-first runtime for stateful AI agents — described as "the systemd for agents." It lets long-lived, autonomous agents persist state, accumulate memory, run on cron schedules, and be managed from the terminal like OS processes.

**Status: Pre-alpha, v0.0.10 landed.** Memory subsystem complete (v0.0.6); web tools upgraded to ADR-0004 floor (v0.0.7); memory re-architected into the dual-axis model (episodic timeline + curated knowledge base) with tasks moved out of memory (v0.0.8, ADR-0005); compaction reworked into a three-trigger model with a blocking user-consent channel (v0.0.9, ADR-0006); task scheduling — event-sourced hierarchical forest, priority scheduler, cooperative preemption, schedule→task bridge (v0.0.10, ADR-0007). v0.1.0 blocked only on non-engineering work (demo GIF, PyPI release, two weeks of dogfooding without a P0 bug).

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

**v0.0.7** — web tools upgrade ([ADR-0004](docs/adr/0004-web-tools.md), three milestones M1–M3):
- **M1** — `src/eonlet/web/` package: `HTTPFetcher` (SSRF guard + 3-attempt retry on 5xx/transport + size cap + stable User-Agent), `ssrf.py` (IPv4/IPv6 classification, cloud-metadata host/IP block-list), `fetch.py` (`trafilatura`-based HTML→markdown + text/JSON passthrough), token-based `paginate`. New errors: `WebError` / `SSRFRejectedError` / `UnsupportedSchemeError` / `ResponseTooLargeError` / `HTTPFetchError`.
- **M2** — `web/search/{tavily,ddg}.py` (two backends, no abstraction; provider dispatch on `TAVILY_API_KEY` env presence). `tools/builtin/web.py` rewritten as a thin shim. `ToolContext.http_fetcher` injected by the worker; `AgentRuntime.http_fetcher` mirrors. New `EventKind` variants `WEB_SEARCH_PERFORMED` / `WEB_FETCH_PERFORMED` (summary-only — full body in `TOOL_RESULT`). `agent.yaml: web.fetch` config block (`max_bytes`, `max_tokens_per_call`, `timeout_seconds`, `allow_private_networks`, `user_agent`).
- **M3** — `templates/x-digest/tools/feed_read.py` as the canonical "extend Eonlet" example (lazy `feedparser` import, ~80 LOC). `TOOL_SPEC.md`, `AGENT_CONFIG_SPEC.md` §8.5, `SECURITY.md` §2.4 SSRF guard updated. ADR-0004 status flipped to Accepted.
- **New runtime dep**: `trafilatura>=2.0` (pulls 10 transitive packages ~17 MB; all permissive licenses; no native compilation required on cp311–cp313 thanks to lxml's manylinux wheels).
- **Test coverage**: 80 new tests across `tests/unit/web/` and a rewritten `tests/unit/test_web_tools.py`. `src/eonlet/web/` covered at 94%; `tools/builtin/web.py` at 91%. Total: 520+ tests.

**v0.0.8** — dual-axis memory ([ADR-0005](docs/adr/0005-dual-axis-memory.md), four milestones M1–M4; supersedes the ADR-0003 memory model):
- **M1** — Knowledge axis: `src/eonlet/memory/knowledge.py` (`KnowledgeStore`), the `knowledge` tool (`open`/`list`/`write`/`edit`/`delete`/`move`), `memory.knowledge` config block, `<knowledge_index>` injection (the curated `knowledge/index.md` map injected whole; bodies opened on demand), new `KB_WRITTEN`/`KB_DELETED`/`KB_MOVED` events + `KnowledgeError`/`KnowledgePathError`.
- **M2** — Episodic narrowing: LTM holds only the `episodic` category; tier-2 emits episodic-only; **tier-3 drops the `src:explicit` "never drop" exemption** (uniform recency/salience). Retired the `remember`/`note`/`forget` tools, `NotesStore`, the `mem_remember`/`mem_note_*` events, and the `memory.notes` block. `recall`'s notes scope → knowledge scope.
- **M3** — Tasks out of memory: new top-level `src/eonlet/tasks/` package (`TaskStore`, `TasksConfig`, `mint_task_id`, `tasks/todos.jsonl`). `todo` tool → `task` tool (+`cancel`); `mem_todo_*` → `task_added`/`task_updated`/`task_deleted`; pending tasks inject as a sibling `<tasks>` block **outside** `<memory>`. Top-level `tasks` config block; `ToolContext`/`AgentRuntime` gain `tasks_dir`.
- **M4** — `memory.conversation` → `memory.episodic` rename; bundled templates + specs + CHANGELOG updated; **`eonlet memory migrate` and `memory/migrate.py` removed** (pre-alpha needs no cross-version migration). `EventKind` settles at **36 variants**.
- **Test coverage**: new `tests/unit/memory/test_knowledge_store.py` + `test_tools_knowledge.py`, new `tests/unit/tasks/` package. Total: 521 tests; ruff + mypy clean.

**v0.0.9** — compaction triggers ([ADR-0006](docs/adr/0006-compaction-triggers.md), four milestones M1–M4; one tier-1 mechanism, three triggers, each with its own boundary policy):
- **M1** — Boundary modes + clean-slate `/compact`: `run_tier1` gains a `full` mode (empties the working window) wired to the user-forced `/compact`, which now also emits `session_ended`+`session_started` to mark an episode boundary. Per-turn timestamps: `memory.inject_turn_timestamps` prefixes each user message with its local datetime at render time (`Message.ts`, events stay immutable). Inert `propose_*` config added.
- **M2** — Blocking decision channel: `worker/decisions.py` (`DecisionBroker`) — a generic worker↔CLI consent round-trip (`decision/request` notification → block → `decision.respond`); no-session/detach auto-declines. **Closes the v0.0.2 interactive-permission-confirm TODO** — `ask`-mode destructive tools now prompt via this channel (`Decision.needs_prompt`); shared by compaction proposals.
- **M3** — Agent-proposed semantic compaction: `memory(action="propose_compact", boundary_event_id, reason)` — floor + dual cooldown guards, interactive-only, blocks on consent, `yolo` auto-approves with audit. `run_tier1` gains an explicit-`boundary` mode + `snap_boundary_safe`. New events `MEM_COMPACT_PROPOSED`/`_APPROVED`/`_DECLINED` (**EventKind 36 → 39**).
- **M4** — MEMORY_SPEC §4 trigger-matrix + consent + timestamps; AGENT_CONFIG/TOOL/CLI/SECURITY docs; templates (`assistant` enables `propose_semantic`, scheduled agents disable it, all stamp turns); CHANGELOG + this file.
- **Test coverage**: new `tests/unit/test_decisions.py` + `test_tools_propose_compact.py`; extended tier-1/events/config/permission/agent-injection tests. Total: 552 tests; ruff + mypy clean.

**v0.0.10** — task scheduling ([ADR-0007](docs/adr/0007-task-scheduling.md), four milestones M1–M4 per [`docs/plans/task-scheduling.md`](docs/plans/task-scheduling.md); the ROADMAP "task-orchestration" / v0.2 feature tier). Normative spec: [`docs/TASK_SPEC.md`](docs/TASK_SPEC.md):
- **M1** — Event-sourced task forest: `src/eonlet/tasks/forest.py` (`Task`, `TaskForest`, `fold_tasks`/`reduce_task`) replaces the `tasks/todos.jsonl` store; the runtime owns `AgentRuntime.task_forest`. Task event family redone: `TASK_CREATED`/`UPDATED`/`TRANSITIONED`/`CHECKPOINTED`/`DELETED` (**EventKind 39 → 41**). `task` tool gains `parent_id`/`priority`/`goal`; `eonlet tasks` tree view; `ToolContext.read_tasks`.
- **M2** — TaskScheduler (`tasks/scheduler.py`: `next_runnable`/`classify_post_run`) + worker integration: idle worker runs the next runnable task (pending leaf or synthesis-ready parent); post-run DONE/DECOMPOSED(→blocked)/YIELDED(→checkpoint+suspend). Per-task prompt (`tasks/context.py`); implicit *current task* (`ToolContext.current_task_id`) so `task(done)`/`task(add)` need no id.
- **M3** — Cooperative preemption (turn-boundary `pause_check` hook; `preemptor` = strictly-higher-priority outside the spine; consent via `DecisionBroker` under `preempt: ask`, auto under `auto_by_priority`/`yolo`; paused task re-queued → pending; `preempt_cooldown`). Schedule→task-template bridge: `task(add, schedule=…)` registers a recurring template that hatches a fresh instance per fire (`task_hatch` `TriggerItem`; persisted in the dynamic store). Reuses `TASK_TRANSITIONED` (no new EventKind).
- **M4** — Guards enforced (`max_tree_depth`/`max_fanout` at creation, `per_task_budget_tokens` per run, `max_suspended` backlog cap); CLI `eonlet tasks <id> {suspend|resume|cancel|prio}`; `docs/TASK_SPEC.md` + AGENT_CONFIG/CHANGELOG/templates.
- **Test coverage**: new `tests/unit/tasks/{test_forest,test_scheduler,test_context,test_schedule_bridge}.py` + `test_preemption.py`; worker integration tests for run-to-done, decompose/synthesis, yield, preemption, schedule-hatch, suspend/resume. Total: 605 tests; ruff + mypy clean.

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
| `docs/TASK_SPEC.md` | Task subsystem: event-sourced forest, scheduler, lifecycle, preemption, schedule bridge, guards (ADR-0007) |
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

Every state change is an immutable append to a per-agent SQLite log. `AgentState` is rebuilt by replaying events — no mutable in-memory state. `EventKind` has **41 variants** covering: conversation turns, tool calls, permissions, triggers, budget, sessions, errors, memory operations (`mem_compacted`/`mem_ltm_promoted`/`mem_ltm_forgotten`/`mem_recall_invoked`/`mem_paused`/`mem_resumed`), compaction proposals (`mem_compact_proposed`/`mem_compact_approved`/`mem_compact_declined`), knowledge writes (`kb_written`/`kb_deleted`/`kb_moved`), tasks (`task_created`/`task_updated`/`task_transitioned`/`task_checkpointed`/`task_deleted` — the event-sourced forest, ADR-0007 M1), and web tool summaries.

```
runtime/store.py   → SQLite append-only log
runtime/events.py  → EventKind enum (41 variants), Event model
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

### Memory: two axes + tasks (ADR-0005)

Memory splits into **two axes with two policies**, plus tasks (workflow state, not memory). Per-agent layout under `~/.eonlet/eonlets/<id>/`:

```
memory/
  short_term.md         → episodic STM: dated sections (tier-1 target)
  long_term.md          → episodic LTM: dated summaries only (tier-2/3 target)
  knowledge/            → AXIS 2 — curated, hierarchical, NEVER auto-deleted
    index.md            →   the agent-curated map; injected whole every call
    user.md, rules/…    →   one file per topic; bodies opened on demand
  index.sqlite          → SQLite FTS5 index over the event log
```

Tasks are **event-sourced** (ADR-0007): the live forest is a `fold` of the task
event family (`TASK_CREATED`/`UPDATED`/`TRANSITIONED`/`CHECKPOINTED`/`DELETED`)
over the per-agent event log — there is no `tasks/todos.jsonl` store any more.
The runtime owns the projection (`AgentRuntime.task_forest`) the same way it
owns `AgentState`.

- **Axis 1 — episodic** (`memory/`): the conversation timeline. Working → STM → LTM via the compaction cascade. It *decays* — that's correct for a timeline.
  1. **Tier-1** (`memory/tier1.py`): working memory → STM sections when working memory exceeds budget.
  2. **Tier-2** (`memory/tier2.py`): STM → dated `episodic` LTM bullets when STM exceeds budget.
  3. **Tier-3** (`memory/tier3.py`): LTM self-compaction (uniform recency/salience — no `src:explicit` exemption) when LTM exceeds budget.
- **Axis 2 — knowledge** (`memory/knowledge/`): durable facts/rules/preferences the agent curates deliberately via the `knowledge` tool. Never budget-forgotten. Only `index.md` (the map) is injected; bodies are opened on demand by path.

`memory/injection.py` builds the `<memory>` preamble (`<knowledge_index>` + `<short_term>` + `<long_term>`) and, separately, `build_tasks_block` injects a sibling `<tasks>` block from `tasks/` — **outside** `<memory>`.

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
├── memory/               — Dual-axis memory subsystem (v0.0.6, re-architected v0.0.8 / ADR-0005)
│   ├── config.py         — MemoryConfig (EpisodicMemoryConfig + KnowledgeMemoryConfig)
│   ├── stm.py            — STMStore (short_term.md sections)
│   ├── ltm.py            — LTMStore (long_term.md — episodic-only bullets)
│   ├── knowledge.py      — KnowledgeStore (curated knowledge tree + index.md map)
│   ├── recall.py         — RecallIndex (SQLite FTS5 search)
│   ├── injection.py      — build_memory_preamble + build_tasks_block
│   ├── compactor.py      — LLMCompactor (coordinates all tiers)
│   ├── tier1.py          — Tier-1 runner (working → STM)
│   ├── tier2.py          — Tier-2 runner (STM → episodic LTM)
│   ├── tier3.py          — Tier-3 runner (episodic LTM forgetting; no exemptions)
│   ├── storage.py        — atomic_write_text + file_lock
│   ├── watermark.py      — Watermark tracking
│   ├── tokens.py         — Token counting
│   └── paths.py          — Memory directory helpers (+ knowledge_root/index)
├── tasks/                — Task / workflow state (ADR-0005; event-sourced forest ADR-0007)
│   ├── forest.py         — Task, TaskForest, fold_tasks/reduce_task (projection of task events)
│   ├── config.py         — TasksConfig (inject_pending, archive_done_after_days)
│   └── ids.py            — mint_task_id
├── permissions/
│   └── gate.py           — Permission gate (ask / yolo modes + hardcoded deny list)
├── runtime/
│   ├── agent.py          — AgentRuntime (main loop, ~460 lines)
│   ├── definition.py     — Load agent.yaml + per-agent tools
│   ├── events.py         — EventKind (41 variants), Event model
│   ├── state.py          — AgentState (event-sourced)
│   └── store.py          — SQLite event store (append-only)
├── templates/            — Bundled example agents (config.yaml + 3 agent dirs)
│   ├── assistant/
│   ├── x-digest/
│   └── portfolio/
├── tools/
│   ├── protocol.py       — Tool interface, ToolContext, ToolResult
│   ├── registry.py       — Tool registration + @tool decorator
│   └── builtin/          — 11 modules
│       ├── bash.py       — bash (shell execution)
│       ├── files.py      — file_read, file_write, file_edit, glob, grep
│       ├── web.py        — web_search (Tavily / DDG), web_fetch (HTTPFetcher → trafilatura)
│       ├── email.py      — send_email
│       ├── sleep_tool.py — sleep
│       ├── skill_tool.py — load_skill
│       ├── schedule.py   — schedule (one-off future trigger)
│       ├── memory.py     — memory (show / compact / propose_compact / compact_ltm / pause / resume)
│       ├── knowledge.py  — knowledge (open / list / write / edit / delete / move)
│       ├── task.py       — task (add / list / done / cancel / update / delete)
│       └── recall.py     — recall (keyword + date; events / knowledge / tasks scopes)
├── trace/                — Context trace (ADR-0010): lineage-aware LLM request log
│   ├── config.py         — TraceConfig (`trace.enabled`, default off)
│   ├── recorder.py       — ContextTracer (delta-on-line / fork-on-rewrite), read_trace/fold_line
│   └── html.py           — render_html: self-contained HTML viewer (no deps, no server)
├── triggers/
│   ├── scheduler.py      — Cron scheduler (croniter + IANA tz, catch-up, backoff)
│   └── dynamic_store.py  — Persistent trigger state (last run, failure count)
├── web/                  — Web subsystem (v0.0.7, ADR-0004)
│   ├── ssrf.py           — SSRF classification (IPv4/IPv6, cloud-metadata)
│   ├── transport.py      — HTTPFetcher (httpx + retries + size cap)
│   ├── fetch.py          — extract_html / extract_text + ExtractedContent
│   ├── pagination.py     — Token-window paginate / PaginatedSlice
│   └── search/           — Search backends (two paths, no abstraction)
│       ├── types.py      — SearchHit / SearchResponse
│       ├── tavily.py     — Tavily REST API call via HTTPFetcher
│       └── ddg.py        — DuckDuckGo HTML scrape (fragile fallback)
└── worker/
    ├── main.py           — Worker entry point + _worker_main
    ├── ipc.py            — Unix socket JSON-RPC server + IPCClient demuxer
    ├── decisions.py      — DecisionBroker (blocking user-consent round-trip, ADR-0006)
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
    ├── memory/                          — memory subsystem tests
    │   ├── test_ltm.py                  — LTMStore (episodic-only) CRUD
    │   ├── test_stm.py                  — STMStore sections
    │   ├── test_knowledge_store.py      — KnowledgeStore CRUD + index sync + path safety
    │   ├── test_tools_knowledge.py      — knowledge tool dispatch + events
    │   ├── test_recall_index.py         — RecallIndex FTS5 search
    │   ├── test_recall_tool.py          — recall tool (events / knowledge / tasks)
    │   ├── test_tier1.py               — Working → STM compaction
    │   ├── test_tier2.py               — STM → episodic LTM promotion
    │   ├── test_tier3.py               — Episodic LTM forgetting
    │   ├── test_compactor.py           — Full compaction flow
    │   ├── test_agent_injection.py     — Full agent context injection
    │   ├── test_injection.py           — Preamble + <tasks> injection unit
    │   ├── test_config.py              — MemoryConfig validation
    │   ├── test_events.py              — Memory / knowledge / task events
    │   ├── test_storage.py             — Atomic file writes
    │   ├── test_watermark.py           — Watermark tracking
    │   └── test_paths.py              — Memory path helpers
    ├── tasks/                           — Task subsystem tests (v0.0.8)
    │   ├── test_forest.py              — TaskForest reducer + DFS + replay
    │   └── test_tools_task.py          — task tool dispatch + events
    ├── web/                             — Web subsystem tests (v0.0.7)
    │   ├── test_ssrf.py                 — IP classification + check_url
    │   ├── test_transport.py            — HTTPFetcher retries + size cap
    │   ├── test_extract.py              — HTML/text/JSON extraction
    │   ├── test_pagination.py           — Token-window slicing
    │   └── test_search.py               — Tavily + DDG backends
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

**Current coverage: 520+ tests, ≥72% branch coverage. CI gate: `--cov-fail-under=70`. Web subsystem (`src/eonlet/web/`) ≥94%; rewritten `tools/builtin/web.py` ≥91%.**

---

## Development Commands

The project uses **uv** (not pip/poetry/conda):

```bash
# One-time setup
uv venv
uv sync --extra dev    # install all deps including dev extras (dev is an extra, not a dependency-group — `--dev` silently installs nothing)
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
7. **Memory is the dual-axis model (ADR-0005), not a placeholder.** `src/eonlet/memory/` is the episodic axis (working→STM→LTM compaction) plus the curated `knowledge/` axis; tasks live in the separate `src/eonlet/tasks/` package, not memory. There is no `notes`/`remember`/`forget`/`todo` tool and no migration tool — don't reintroduce them.

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
