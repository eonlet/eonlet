# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status: v0.0.6 Landed — full memory subsystem complete

**v0.0.1 (Spine)** — event store, tool protocol, 10 offline builtins, Anthropic/OpenAI providers, agent loop, worker (anyio + Unix-socket JSON-RPC), permission gate, core CLI.

**v0.0.2** — cron scheduler (croniter + IANA tz, grace-period catch-up, ≥3-failure backoff), 16-slot `TriggerItem` queue refactor, 3 network builtins (`web_search`/`web_fetch`/`send_email`), `eonlet fire`/`doctor`, `x-digest` template.

**v0.0.3** — debug + archive: IPCClient demuxer, `ps`/`tail`/`replay`/`export`/`import`, portfolio template (third bundled agent).

**v0.0.4** — LLM streaming: `LLMProvider.stream()` for Anthropic + OpenAI; `AgentRuntime.on_delta` callback; CLI `attach` prints token deltas inline. Token deltas are notifications, never events (SPEC §8.1).

**v0.0.5** — quality sprint, v0.1.0 SPEC §12 targets all met:
- **`FakeProvider`** (`fake-echo`, `fake-tool-then-text`) — in-process LLM selectable by `fake-*` model prefix. Powers deterministic integration tests without an API key.
- **Worker integration tests**, two flavors: `test_worker_subprocess.py` spawns `python -m eonlet.worker.main` as a real OS process and exercises the full IPC stack; `test_worker_inprocess.py` calls the refactored `run_worker(eonlet_id, shutdown, install_signal_watcher=False)` inside a task group so pytest-cov can attribute it. The factored `run_worker` is the same function `_worker_main` wraps — production path unchanged.
- **LLM provider unit tests** stand up `_FakeAnthropicMessages` and `_FakeOAICompletions` via `monkeypatch.setitem(sys.modules, ...)`, exercising real `anthropic_provider.py` / `openai_provider.py` code: message mapping, tool_result block shape, streaming text chunk emission, **chunked tool_call JSON reassembly by index** for OpenAI.
- **mypy strict — zero errors**. `pyproject.toml` excludes `src/eonlet/templates/` (user-facing example code) and adds a per-module override for the two SDK-adapter providers that disables `union-attr`/`arg-type`/`attr-defined`/`call-overload` (the SDKs' generated union types are too rich to narrow exhaustively at our boundary; the providers are fully exercised by tests). croniter/apsw/msgpack/yaml get `ignore_missing_imports`.
- **Ruff strict — zero errors, zero warnings, formatter clean**. Per-file ignores for typer's `B008` canonical pattern (`def cmd(x = typer.Option(...))`) and `config.py`'s `apiVersion` mixedCase (matches the YAML field).
- **CI gate raised to `--cov-fail-under=70`**. Measured **72.6% coverage** with 89 passing tests across `tests/unit/` (16 files) and `tests/integration/` (2 files).
- `fail()` typed `NoReturn`; `IPCClient.notifications()` returns a typed `MemoryObjectReceiveStream[dict[str, Any]]` so mypy can verify CLI callers.

**v0.0.6** — full memory subsystem (MEMORY_SPEC P1–P6, ADR-0003):
- **P1** — `src/eonlet/memory/` package: `MemoryConfig` (token budgets, enabled flag, compaction_model), `STMStore` (short_term.md sections), `LTMStore` (long_term.md bullets with category/src/ts), `atomic_write_text` + `file_lock`, watermark tracking. New events: `mem_compacted`, `mem_ltm_promoted`, `mem_ltm_forgotten`, `mem_remember`, `mem_paused`, `mem_resumed`.
- **P2** — `NotesStore` (`notes.md` YAML-frontmatter entries) + `TodosStore` (`todos.jsonl` with status/due), `note` tool (add/get/list/delete), `todo` tool (add/done/cancel/list), legacy `notes_read`/`notes_append` removed.
- **P3** — `RecallIndex` (SQLite FTS5 over event log + memory docs), `recall` tool (keyword + date + category filters).
- **P4** — Tier-1 compaction: LLM-driven working→STM pass; `LLMCompactor`; context injection preamble (working memory + STM + LTM + notes + todos); `/compact` slash command wired to `memory` tool; worker cascade hooks.
- **P5** — Tier-2 (STM→LTM promotion when STM over budget) + Tier-3 (LTM forgetting when LTM over budget); `remember` tool (`src:explicit` bullets); `forget` tool (dry-run + confirmed delete, M-I7 event digest); `memory compact_ltm` action; worker cascade: tier1 → tier2 → tier3.
- **P6** — `eonlet memory migrate <legacy_dir>` CLI command migrates Claude Code auto-memory files (MEMORY.md + per-fact .md with YAML frontmatter) into LTM; `AGENT_CONFIG_SPEC.md` §8 rewritten with new schema (legacy `notes_files`/`recent_messages_in_context` removed); `CLI_REFERENCE.md` updated with memory commands section.
- **Test coverage**: 56 new unit tests across `tests/unit/memory/` (ltm, stm, tier2, tier3, remember/forget, migrate + existing tier1/config/storage tests).

**v0.1.0 still owes** (no engineering — these are operational):
- 30-second README demo GIF (author has to record it).
- PyPI release: `uv build`/`uv publish`, version tag, changelog entry.
- ROADMAP "Done condition": two weeks of author dogfooding without a P0 bug.
- All v0.2+ items per ROADMAP (MCP, hooks, vector memory, TUI, hibernate) remain out.

Before writing or modifying code, read the relevant spec — the design is authoritative; the code follows it.

The order of implementation for the v0.1 MVP is fixed in `src/eonlet/README.md` ("Order of Implementation"):
event store → tool protocol/loader → builtin tools → LLM providers → agent loop → worker + IPC → CLI lifecycle → triggers → permissions. Do not skip ahead unless asked — earlier layers are dependencies of later ones.

## Key Documents (read these before non-trivial work)

- `docs/SPEC.md` — master technical spec (all subsystems)
- `docs/AGENT_CONFIG_SPEC.md` — every field of `agent.yaml`
- `docs/TOOL_SPEC.md` — Tool protocol + builtin tool catalog
- `docs/TRIGGER_SPEC.md` — schedule/event/interactive triggers
- `docs/DIRECTORY_LAYOUT.md` — where files live at runtime (`~/.eonlet/`)
- `docs/SECURITY.md` — permission model + threat model
- `docs/adr/` — architecture decision records; propose a new ADR before any architectural change
- `ROADMAP.md` — what belongs in which version (v0.1 MVP vs v0.2/0.3/0.4 deferred)
- `MANIFESTO.md` — north star: many specialist agents, organized into teams; today's `metadata.specialty` / `metadata.capabilities` fields are forward-compatible declarations for that future.

`src/eonlet/README.md` mirrors the planned package layout and phase-0 scope — consult it to know which module a piece of new code belongs in, and which features are explicitly **out of scope** for v0.1 (compaction, MCP, hooks, semantic memory, TUI, supervisor, A2A). Note: `src/eonlet/memory/` exists as an empty placeholder package; do not put v0.1 code there — semantic/vector memory is a v0.2+ concern.

## Architecture at a Glance

Two processes per running agent:

- `eonlet` (CLI, `src/eonlet/cli/`, console script `eonlet = eonlet.cli.main:cli_main`; the Typer object itself is `eonlet.cli.main:app`) — user-facing. Spawns workers, attaches/detaches via Unix sockets, manages definitions.
- `eonlet-worker` (`src/eonlet/worker/`, entry point `eonlet.worker.main:main`) — one long-lived OS process per agent ("eonlet"). Owns the agent loop, IPC socket, and SQLite event store.

State is **event-sourced**: every step is appended to a per-agent SQLite log; `AgentState` is rebuilt by replaying events. This is why `runtime/events.py` + `runtime/store.py` are implemented first — everything else writes events through them.

An agent is defined entirely by files on disk (no DB-stored config):

```
<agent_dir>/
├── agent.yaml      # config + triggers + permissions + metadata
├── system.md       # system prompt
├── tools/*.py      # custom Python tools (imported by runtime/definition.py; builtins live in tools/builtin/ and self-register via @tool through tools/registry.py)
├── skills/*.md     # Claude Code–style skills, loaded via load_skill tool
└── prompts/        # optional, agent-specific prompt fragments
```

The three reference agents in `agents/` (`assistant`, `x-digest`, `portfolio`) are both **examples** and **the canonical fixtures** the runtime is being designed against — when in doubt about a config field's intended shape, check how these agents use it.

Two operating modes are both first-class: interactive (user attaches via `eonlet attach`, tmux-style) and scheduled (cron triggers wake the agent; it works and goes idle). The runtime, event store, and tool surface are the same for both.

## Development Commands

The project uses **uv** (not pip/poetry):

```bash
uv venv
uv sync --dev          # install with dev extras from pyproject.toml
pre-commit install
pytest                 # full suite
pytest tests/path/to/test_file.py::test_name   # single test
ruff check .           # lint
ruff format .          # format
mypy src               # strict-mode type check (configured in pyproject.toml)
```

Test config (`pyproject.toml`): `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `--strict-markers`. Coverage is branch-mode over `src/eonlet`.

## Coding Standards (from `docs/SPEC.md` §15 / `CONTRIBUTING.md`)

These are project-specific rules that override common Python defaults — apply them without being asked:

- Python ≥ 3.11; `from __future__ import annotations` at the top of every module.
- Async: use **anyio**, not raw `asyncio`. Never call `asyncio.run` — use `anyio.run`.
- Logging: **structlog** only. No `print()` anywhere. CLI user-facing output goes through **rich**.
- Errors: no bare `except Exception`. Raise from a project-specific exception hierarchy (define alongside the subsystem that owns the error).
- Type annotations required on all public APIs; mypy runs in strict mode (`disallow_any_unimported`, `warn_return_any`).
- Ruff lint set is broad (`E,F,W,I,N,UP,B,C4,SIM,RET,PTH,ASYNC,RUF`); `E501` is disabled because the formatter owns line length (100).
- Minimal dependencies. Do **not** pull in langchain, transformers, or other heavy ML frameworks — see `CONTRIBUTING.md` "What Not to Send".

## Working with Agent Definitions

When editing or adding example agents under `agents/`:

- The fields in `agent.yaml` are normative — validate against `docs/AGENT_CONFIG_SPEC.md` rather than inventing new keys.
- Custom tools follow the protocol in `docs/TOOL_SPEC.md` and are imported per-agent by `runtime/definition.py`; builtins live in `tools/builtin/` and self-register through `tools/registry.py` at import time.
- `.env.example` files document required secrets — never commit a real `.env`.
- `metadata.specialty` and `metadata.capabilities` look optional but are deliberately forward-compatible with the future team-formation system; preserve them when restructuring.
