# Source Code

This directory is currently a stub.

The Eonlet project is in **design phase**. All design documents in [`docs/`](../../docs/) and example agents in [`agents/`](../../agents/) are complete. Code implementation starts after spec review.

## Planned Structure

```
src/eonlet/
├── __init__.py
├── cli/                    # `eonlet` CLI (entry point: cli.main:app)
│   ├── main.py
│   ├── commands/
│   │   ├── init.py
│   │   ├── definitions.py
│   │   ├── lifecycle.py    # create, ls, pause, resume, kill, rm
│   │   ├── interact.py     # attach, send, logs
│   │   └── debug.py        # inspect, replay
│   └── ui/                 # TUI components (v0.2)
│
├── worker/                 # `eonlet-worker` (entry point: worker.main:main)
│   ├── main.py
│   ├── ipc.py              # serve runtime.sock
│   ├── heartbeat.py
│   └── lifecycle.py
│
├── runtime/                # core
│   ├── agent.py            # the main loop
│   ├── state.py            # AgentState (event-sourced)
│   ├── events.py           # Event types
│   ├── store.py            # SQLite event store
│   └── definition.py       # loading agent.yaml + system.md + tools
│
├── tools/                  # builtin tools + Tool protocol
│   ├── protocol.py         # Tool, ToolContext, ToolResult, etc.
│   ├── registry.py
│   ├── builtin/
│   │   ├── bash.py
│   │   ├── file_ops.py
│   │   ├── notes.py
│   │   ├── web.py
│   │   ├── email.py
│   │   ├── sleep.py
│   │   └── skills.py
│   └── loader.py           # discovers and imports custom tools
│
├── triggers/               # scheduler
│   ├── scheduler.py
│   ├── cron.py
│   └── interactive.py
│
├── permissions/
│   ├── gate.py
│   └── patterns.py
│
├── memory/
│   ├── procedural.py       # notes.md, todo.md
│   └── episodic.py         # event-store-based memory access
│
└── llm/                    # provider abstraction
    ├── protocol.py
    ├── anthropic_provider.py
    └── openai_provider.py
```

## Order of Implementation (Phase 0)

Per [`ROADMAP.md`](../../ROADMAP.md), the order is:

1. `runtime/events.py` + `runtime/store.py` — event store (foundation)
2. `tools/protocol.py` + `tools/loader.py` — tool interface
3. `tools/builtin/` — implement the 13 builtins
4. `llm/` — provider abstraction
5. `runtime/agent.py` — main loop
6. `worker/main.py` + IPC — process container
7. `cli/main.py` + lifecycle commands — user surface
8. `triggers/` — scheduler
9. `permissions/` — gate

Each step gets unit and integration tests before the next starts.

## What's NOT In Scope for Phase 0

The implementation plan above is for v0.1.0 MVP only. The following land in later versions:

- `runtime/compaction.py` — v0.3
- `tools/mcp.py` — v0.2
- `runtime/hooks.py` — v0.2
- `memory/semantic.py` — v0.2 (sqlite-vec)
- `cli/ui/` — v0.2 (textual TUI)
- `supervisor/` — v0.4
- `protocols/a2a.py` — v0.4

See [`docs/ROADMAP.md`](../../ROADMAP.md) for full phasing.
