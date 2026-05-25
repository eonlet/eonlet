# Changelog

All notable changes to Eonlet will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) starting at v1.0.0.

## [Unreleased] — toward v0.1.0

### Designed

- **ADR-0004: Web Tools — Minimal Built-in, Extensible via Skills and MCP.** Promotes `web_search` and `web_fetch` from v0.0.2 placeholders to a deliberately minimal but reliable in-tree implementation: `HTTPFetcher` with SSRF + retries + size cap, `trafilatura`-based HTML→markdown extraction, two search paths (Tavily + DDG fallback), token-based pagination. Explicitly **does not** ship PDF / RSS / multi-search-provider abstraction in v0.1 — that's an extensibility concern (custom tools today, MCP at v0.2). See [`docs/plans/web-tools.md`](docs/plans/web-tools.md) for the three-milestone implementation plan (~2 days).

### Remaining for v0.1.0

- Land ADR-0004 (web tools upgrade).
- PyPI release (`uv build` / `uv publish`, version tag).
- 30-second README demo GIF.
- Two weeks of author dogfooding without a P0 bug.

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
