# Changelog

All notable changes to Eonlet will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) starting at v1.0.0.

## [Unreleased] — toward v0.1.0

Remaining work for the v0.1.0 release tag (non-engineering):

- PyPI release (`uv build` / `uv publish`, version tag, classifier bump).
- 30-second README demo GIF.
- Two weeks of author dogfooding without a P0 bug. ADR-0004's 48-hour
  `x-digest` live-feed canary is part of this window.

## [0.0.10] — 2026-05-31 — Task scheduling (ADR-0007)

Implements [ADR-0007](docs/adr/0007-task-scheduling.md) (milestones M1–M4 per [`docs/plans/task-scheduling.md`](docs/plans/task-scheduling.md)) — the ROADMAP "task-orchestration" feature tier. An eonlet becomes a single human-like worker that runs a hierarchical task forest one task at a time, by priority, with cooperative preemption. Normative spec: [`docs/TASK_SPEC.md`](docs/TASK_SPEC.md).

### Added

- **Event-sourced task forest** — tasks are now a `fold` of the task event family (`TASK_CREATED` / `UPDATED` / `TRANSITIONED` / `CHECKPOINTED` / `DELETED`), like `AgentState`. The runtime owns `AgentRuntime.task_forest`; the old `tasks/todos.jsonl` store is retired. `EventKind` → **41 variants**.
- **Hierarchical tasks** — `task add` takes `parent_id`/`priority`/`goal`; tasks form a tree (forest across roots), traversed depth-first. `eonlet tasks <id>` renders the forest as a tree.
- **TaskScheduler** — when `tasks.scheduling.enabled`, the worker runs the next runnable task (highest-priority pending leaf, or a blocked parent ready to synthesize) when idle; queued user/cron triggers take precedence. Post-run the task is classified DONE / DECOMPOSED (→ block on children) / YIELDED (→ checkpoint + suspend). A task-scoped run sets an implicit *current task* so the agent says `task(done)` / `task(add …)` without restating the id.
- **Cooperative preemption** — at a turn boundary, a strictly-higher-priority runnable task pauses the current one (consent via the decision channel under `preempt: ask`, auto under `auto_by_priority`/`yolo`); the paused task is checkpointed and re-queued as pending to resume later. `preempt_cooldown` guards against thrash.
- **Schedule → task-template bridge** — `task add` with a `schedule` (cron + `timezone`) registers a recurring template; each fire hatches a fresh task instance (`origin=trigger`) with its own history. Templates persist across restarts. The low-level `schedule` tool is unchanged.
- **Anti-runaway guards** (`tasks.scheduling`) — `max_tree_depth` / `max_fanout` (reject at creation), `per_task_budget_tokens` (cap a run), `max_suspended` (cancel a no-progress yield when the backlog is full).
- **CLI ops** — `eonlet tasks <id> {suspend|resume|cancel|prio}` over the running worker.
- **FakeProvider variants** — `fake-task-done` / `fake-task-tree` / `fake-task-busy` / `fake-schedule` drive the scheduler paths deterministically.

### Changed

- `task` tool: event-only mutation (no JSONL double-write); lifecycle states `pending/active/suspended/blocked/done/cancelled`; `done` accepts a `result`.
- `assistant` template enables `tasks.scheduling` (`preempt: ask`) with prompt guidance on trivial-inline vs. task work; scheduled templates leave scheduling off.

## [0.0.9] — 2026-05-30 — Compaction triggers (ADR-0006)

Implements [ADR-0006](docs/adr/0006-compaction-triggers.md) (milestones M1–M4 per [`docs/plans/compaction-triggers.md`](docs/plans/compaction-triggers.md)). One tier-1 mechanism, **three triggers**, each with its own boundary policy: bounded-auto (keeps a ~30% tail), user-forced full `/compact` (empties the working window), and agent-proposed semantic compaction (consent-gated). Also lands the long-deferred interactive permission confirm.

### Added

- **Agent-proposed semantic compaction** — `memory(action="propose_compact", boundary_event_id, reason)`. The agent's only path to compaction: it proposes folding stale context up to a chosen boundary and **blocks for the user's consent** before acting. Gated by a token floor (`propose_floor_tokens`) and a dual cooldown (`propose_min_interval_seconds` wall-clock + `propose_min_turns` turns) since the last compaction. Interactive only — a no-op in headless/cron runs. Under `yolo` it auto-approves (still recorded for audit).
- **Blocking decision channel** (`worker/decisions.py`, `DecisionBroker`) — a generic worker↔CLI round-trip: push a `decision/request` notification, block the turn, resume on the CLI's `decision.respond`. First responder wins; no attached session (or a mid-wait detach) auto-declines so a headless worker never hangs. Shared by compaction proposals and the permission confirm.
- **Interactive permission confirm** — `ask`-mode destructive tools now genuinely prompt the attached user via the decision channel (closing the v0.0.2 `permissions/gate.py` TODO) instead of silently auto-allowing. `Decision` gains `needs_prompt`.
- **Per-turn timestamps** — `memory.inject_turn_timestamps` prefixes each user message rendered into the working window with its local datetime (`[2026-05-30 14:23 +08:00]`), render-time only (events stay immutable). `Message` gains `ts`.
- **New events** `mem_compact_proposed` / `mem_compact_approved` / `mem_compact_declined` (**EventKind 36 → 39**). User-forced full `/compact` emits `session_ended` + `session_started` to mark the episode boundary.
- **Config:** `memory.inject_turn_timestamps` and `memory.episodic.propose_semantic` / `propose_floor_tokens` / `propose_min_interval_seconds` / `propose_min_turns`.

### Changed

- **`/compact` is now a full, clean-slate compaction** — it summarizes the whole working window into STM and empties it (was: keep a ~30% tail), marking a new episode. The bounded-auto post-turn trigger is unchanged. `run_tier1` gains `full` and explicit-`boundary` modes alongside the default tail boundary.
- Bundled templates updated: `assistant` enables `propose_semantic`; the scheduled `x-digest` / `portfolio` set `propose_semantic: false`; all three set `inject_turn_timestamps: true`.

### Test coverage

- New `tests/unit/test_decisions.py` (broker + real-socket round-trip) and `tests/unit/test_tools_propose_compact.py`; extended tier-1, events, config, permission, and agent-injection tests. Total: 552 tests; ruff + mypy clean.

## [0.0.8] — 2026-05-29 — Dual-axis memory (ADR-0005)

Implements [ADR-0005](docs/adr/0005-dual-axis-memory.md) in full (milestones M1–M4 per [`docs/plans/dual-axis-memory.md`](docs/plans/dual-axis-memory.md)). Memory splits into two axes with two different policies, and tasks move out of memory entirely. **Supersedes the ADR-0003 memory model.** Pre-alpha: no migration path — rewrite `agent.yaml` and start fresh.

### Added

- **Knowledge axis** (`src/eonlet/memory/knowledge.py`, `KnowledgeStore`). A curated, hierarchical tree of markdown files under `memory/knowledge/` that is **never auto-deleted**. `knowledge/index.md` is an agent-curated map (one line per file) injected whole into every LLM call as `<knowledge_index>`; file bodies are opened on demand by path (like `load_skill`), never injected.
- **`knowledge` tool** — single durable-write surface: `open` / `list` / `write` (full body + index hook) / `edit` (string-replace, `files.py` semantics) / `delete` / `move`. Path-confined to the tree (`..`, absolute, and the reserved `index.md` are rejected).
- **`tasks` axis** — new top-level `src/eonlet/tasks/` package (`TaskStore`, `TasksConfig`, `mint_task_id`) storing `tasks/todos.jsonl`, a sibling of `memory/`. Pending tasks inject as a `<tasks>` block **outside** `<memory>` (`build_tasks_block`, gated by `tasks.inject_pending`). New `task` tool (`add`/`list`/`done`/`cancel`/`update`/`delete`) replaces `todo`.
- **New events** `KB_WRITTEN` / `KB_DELETED` / `KB_MOVED`; new errors `KnowledgeError` / `KnowledgePathError`.
- **Config:** `memory.knowledge` block (`inject_index`, `index_max_tokens`, `warn_file_tokens`) and a top-level `tasks` block (`inject_pending`, `archive_done_after_days`). `ToolContext` / `AgentRuntime` gain `tasks_dir`.

### Changed

- **Episodic LTM is now a single population.** `long_term.md` holds only dated `episodic` summaries; the five semantic categories (`user`/`feedback`/`project`/`reference`/`fact`) are gone from LTM. Tier-2 promotion emits episodic-only; **tier-3 forgetting drops the `src:explicit` "never drop" exemption** — every bullet is uniformly forgettable by recency/salience (the special-casing ADR-0005 set out to remove).
- **`memory.conversation` → `memory.episodic`** (config key + `EpisodicMemoryConfig`), matching the dual-axis vocabulary.
- `recall`'s `notes` scope → `knowledge` scope (returns paths to `knowledge.open`); `todos` scope → `tasks` scope.
- `memory` tool `show` is now `stm`/`ltm`/`all` (knowledge and tasks have their own tools); `mem_ltm_forgotten` cause is `tier3`-only.
- CLI: `/knowledge` replaces `/note`; `/task` replaces `/todo`; worker IPC `memory.note.*` → `memory.knowledge.*` and `memory.todo.*` → `task.*`.
- Bundled templates (`assistant` / `x-digest` / `portfolio`) updated to the `knowledge` + `task` tools and the `episodic`/`knowledge`/`tasks` config blocks.

### Removed

- The `remember`, `note`, `forget`, and `todo` builtin tools; `memory/notes.py` (`NotesStore`), `memory/todos.py` (`TodosStore`), `memory/ids.py`.
- The `mem_remember` and `mem_note_*` events; `mem_todo_*` renamed to `task_added` / `task_updated` / `task_deleted`.
- The `memory.notes` / `memory.todos` config blocks (now rejected).
- **The `eonlet memory migrate` command** and `memory/migrate.py` — pre-alpha needs no cross-version migration.

### Tests

- New `tests/unit/memory/test_knowledge_store.py`, `test_tools_knowledge.py`; new `tests/unit/tasks/` package (`test_store.py`, `test_tools_task.py`). Memory/injection/events/config/recall/status/CLI suites updated for the dual-axis model. **521 tests pass**; ruff + mypy clean.

## [0.0.7] — 2026-05-28 — Web tools upgrade (ADR-0004)

Implements [ADR-0004](docs/adr/0004-web-tools.md) in full (milestones M1–M3 per [`docs/plans/web-tools.md`](docs/plans/web-tools.md)).

### Added

- **`src/eonlet/web/` package.** `HTTPFetcher` (SSRF guard, scheme allow-list, 3-attempt retry on 5xx / transport errors with 0.5 / 1 / 2 s backoff, streaming size cap, stable User-Agent), `ssrf.py` (IPv4/IPv6 classification + cloud-metadata block-list), `fetch.py` (`trafilatura`-based HTML→markdown + plain-text / JSON passthrough), token-based `paginate` reusing the `memory/tokens.py` estimator.
- **`web/search/`** — two backends, no abstraction. `tavily.py` is used when `TAVILY_API_KEY` is set; `ddg.py` is the fragile zero-config fallback. Provider selection is purely env-var-based — there is no `provider="…"` argument.
- **`tools/builtin/web.py`** rewritten as a thin shim (~260 LOC). `WebSearchArgs` gains `include_raw_content` (Tavily-only; emits a `raw_content_unavailable_on_ddg` warning when ignored). `WebFetchArgs` adds `max_tokens` / `offset_tokens` for paged reading.
- **`ToolContext.http_fetcher: HTTPFetcher | None`** — the worker constructs one fetcher per agent from `agent.yaml: web.fetch` and threads it through every tool call. Mirrored on `AgentRuntime.http_fetcher`.
- **`agent.yaml: web.fetch`** config block — `max_bytes` (default 10 MB), `max_tokens_per_call` (default 4000), `timeout_seconds` (default 30), `allow_private_networks` (SSRF escape hatch — metadata + link-local stay blocked regardless), `user_agent`.
- **`EventKind.WEB_SEARCH_PERFORMED` / `WEB_FETCH_PERFORMED`** — summary-only events (full responses go through the normal `TOOL_RESULT`). Payloads carry provider, query, hit_count, url, content_type, bytes_in, offset_tokens, total_tokens, truncated, error.
- **`templates/x-digest/tools/feed_read.py`** as the canonical extensibility example — a ~80-LOC `feedparser` wrapper (lazy import; clear error if not installed). Documented in `TOOL_SPEC.md` as the pattern for extending Eonlet beyond the built-in floor.
- **New error types:** `WebError` (base), `SSRFRejectedError`, `UnsupportedSchemeError`, `ResponseTooLargeError`, `HTTPFetchError`.

### Changed

- `docs/TOOL_SPEC.md` §6.7 and §6.8 rewritten to reflect the new contract; both gain a "When the built-in isn't enough" subsection pointing at custom tools (with `feed_read.py` as the example) and MCP (v0.2 placeholder).
- `docs/AGENT_CONFIG_SPEC.md` §8.5 added documenting the `web` block.
- `docs/SECURITY.md` §2.4 expanded with a full SSRF block-list, the `allow_private_networks` escape-hatch semantics, and a DNS-rebinding residual-risk note.

### Removed

- `WebFetchArgs.prompt` — was unused since v0.0.2. Existing agents that pass it as a no-op will fail input validation; remove the field from their calls.

### Dependencies

- **New runtime dep:** `trafilatura>=2.0`. Pulls 10 transitive packages totalling ~17 MB of wheels (lxml 5 MB + babel 10 MB dominate). All permissive licenses (Apache 2.0 / BSD / MIT, with `tld` offering MPL-1.1 OR GPL-2.0 OR LGPL-2.1+). No source compilation required — lxml ships manylinux wheels for cp311–cp313.

### Tests + quality gates

- 80 new tests under `tests/unit/web/` plus a rewritten `tests/unit/test_web_tools.py`. Total: **520+ tests**.
- Coverage: `src/eonlet/web/` at 94%, `tools/builtin/web.py` at 91%.
- ruff strict (lint + format) and mypy strict pass on all new code.

## [0.0.6] — 2026-05-22 — Memory subsystem

Implements [ADR-0003](docs/adr/0003-memory-system.md) and [`docs/MEMORY_SPEC.md`](docs/MEMORY_SPEC.md) in full (phases P1–P6).

### Added

- `src/eonlet/memory/` package (16 modules): `MemoryConfig`, `STMStore`, `LTMStore`, `NotesStore`, `TodosStore`, `RecallIndex` (SQLite FTS5), `LLMCompactor`, `tier1`/`tier2`/`tier3` runners, `injection`, atomic-write + file-lock storage, watermark tracking, Claude Code migration.
- New builtin tools: `note` (add/get/list/delete), `todo` (add/done/cancel/list), `recall` (keyword + date + category search), `remember` (explicit LTM write), `forget` (dry-run + confirmed delete), `memory` (compact / pause / resume).
- Three-tier compaction cascade: working → STM (LLM-driven), STM → LTM (promotion), LTM → LTM (self-compaction).
- Context injection: working + STM + LTM + notes + todos preamble on every LLM call.
- `eonlet memory migrate <legacy_dir>` CLI command to import Claude Code MEMORY.md files into LTM.
- `/compact` slash command.
- 11 new `EventKind` variants (37 total): `mem_compacted`, `mem_ltm_promoted`, `mem_ltm_forgotten`, `mem_note_*`, `mem_todo_*`, `mem_remember`, `mem_recall_invoked`, `mem_paused`, `mem_resumed`.
- 56 new unit tests under `tests/unit/memory/` (21 test files). Total: 145+ tests.

### Removed

- Legacy `notes_read` and `notes_append` tools (superseded by `note`/`todo`/`recall`).

### Changed

- `AGENT_CONFIG_SPEC.md` §8 rewritten for the `memory:` block.
- `CLI_REFERENCE.md` documents the new `memory` subcommands.

## [0.0.5] — Quality sprint

### Added

- `FakeProvider` (`fake-echo`, `fake-tool-then-text`) — deterministic in-process LLM for tests; no API key required.
- Worker integration tests: `test_worker_subprocess.py` (real OS process via `python -m eonlet.worker.main`) and `test_worker_inprocess.py` (pytest-cov-friendly).
- LLM provider unit tests with monkeypatched `_FakeAnthropicMessages` and `_FakeOAICompletions`, exercising real provider code including chunked tool-call JSON reassembly.

### Changed

- **mypy strict — zero errors.** SDK-adapter providers exempt from `union-attr` / `arg-type` / `attr-defined` / `call-overload` (SDK union types are too rich to narrow exhaustively). `croniter`, `apsw`, `msgpack`, `yaml` have `ignore_missing_imports`.
- **Ruff strict — zero errors, formatter clean.**
- CI gate: `--cov-fail-under=70`. Measured 72.6% branch coverage on 89 tests.

## [0.0.4] — LLM streaming

### Added

- `LLMProvider.stream()` for Anthropic and OpenAI providers.
- `AgentRuntime.on_delta` callback hook.
- CLI `attach` now prints token deltas inline.

### Architectural invariant

- Token deltas are **notifications, never events** (SPEC §8.1) — `ASSISTANT_TOKEN_DELTA` is not persisted to SQLite.

## [0.0.3] — Debug + archive

### Added

- `IPCClient` event demuxer.
- CLI commands: `ps`, `tail`, `replay`, `export`, `import`.
- Third bundled agent template: `portfolio` (joining `assistant` and `x-digest`).

## [0.0.2] — Cron triggers + network tools

Implements [ADR-0002](docs/adr/0002-dynamic-triggers.md).

### Added

- Cron scheduler: `croniter` + IANA timezone, grace-period catch-up, ≥3-failure exponential backoff.
- 16-slot `TriggerItem` queue (bounded backpressure).
- Network builtin tools: `web_search` (Tavily + DDG HTML fallback), `web_fetch` (httpx + regex tag-strip), `send_email`.
- CLI commands: `eonlet fire` (manual trigger), `eonlet doctor` (diagnostics).
- Second bundled agent template: `x-digest`.

### Known limitation (slated for v0.1 — see ADR-0004)

- `web_search` DDG fallback uses fragile regex scraping. `web_fetch` strips HTML to plain text, destroying link and structural information. Both are placeholder-grade.

## [0.0.1] — Spine

### Added

- SQLite append-only event store.
- Tool protocol + `@tool` decorator + auto-registration via `tools/registry.py`.
- 10 offline builtin tools: `bash`, `file_read`, `file_write`, `file_edit`, `glob`, `grep`, `sleep`, `load_skill`, `notes_read`, `notes_append` (last two removed in v0.0.6).
- Anthropic + OpenAI LLM providers.
- Agent loop (`AgentRuntime`).
- Worker process: anyio + Unix-socket JSON-RPC.
- Permission gate: `ask` + `yolo` modes + hardcoded deny list.
- Core CLI: `init`, `create`, `attach`, `ls`, `kill`, `rm`.
- Default `assistant` agent template.
- Pause / resume via SIGSTOP / SIGCONT.

### Architectural foundations

- **No supervisor in MVP** — CLI directly manages worker processes; `eonletd` deferred to v0.4+ ([ADR-0001](docs/adr/0001-no-supervisor-mvp.md)).
- **Event sourcing throughout** — every state change appends an immutable row; `AgentState` is replay-derived.
- **anyio everywhere** — no raw `asyncio` primitives.
- **structlog only** — no `print()` calls; CLI output routed through `rich`.
