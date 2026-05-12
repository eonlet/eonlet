# Eonlet

> **Agents that live for ages.**
>
> The systemd for AI agents — spawn, attach, persist.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
![status](https://img.shields.io/badge/status-pre--alpha-orange)

Eonlet is a **local-first runtime for stateful AI agents**. Each agent runs as a long-lived OS process — what we call an *eonlet* — with its own event log, memory, and Unix socket. You define agents as plain YAML and Markdown files; the CLI spawns them, attaches to them like tmux sessions, and lets them keep working in the background between conversations.

## Why Eonlet?

Most agent frameworks help you write agents. Eonlet helps you **run** them.

```bash
# Define an agent: just files
~/.eonlet/agents/researcher/
├── agent.yaml       # config
├── system.md        # prompt
└── tools/           # custom Python tools

# Run it: a real OS process with persistent memory
$ eonlet create researcher --name=alice
alice ready (pid=12345)

$ eonlet attach alice
[alice]: Hi! What are we researching today?
> ...conversation...
[Ctrl+B D]  → detached, alice keeps running in background

# Next week
$ eonlet attach alice
[alice]: Welcome back. We left off looking at KV-cache eviction strategies.
         Want to continue or pivot?
```

Compared to alternatives:

| | Eonlet | Letta | Claude Code | LangGraph |
|---|---|---|---|---|
| Storage | Local filesystem | PostgreSQL | Local files | App-defined |
| Interaction | Terminal-native | REST + Web GUI | Terminal | Programmatic |
| Per-agent process | ✅ Yes | ❌ Shared server | ✅ Per session | ❌ |
| Scheduled triggers | ✅ MVP | ✅ | ✅ | ✅ |
| Attach/detach (tmux-style) | ✅ MVP | ❌ | ❌ | ❌ |
| Filesystem-defined agents | ✅ MVP | ❌ (DB-stored) | ✅ Partial | ❌ |
| Multi-agent | 🚧 v0.4 | ✅ | ✅ subagents | ✅ |
| Self-hosted | ✅ Required | ✅ Optional | N/A | ✅ |

## Quickstart (≤ 5 minutes)

```bash
# 1. Install
pip install eonlet

# 2. Initialize (creates ~/.eonlet/ with default templates)
eonlet init

# 3. Set your API key
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Create your first eonlet from the bundled assistant template
eonlet create assistant --name=alice

# 5. Talk to it
eonlet attach alice
```

You'll be in a conversation. `Ctrl+B D` to detach, `eonlet attach alice` to come back. Your conversation history and notes persist between sessions.

## Two Modes of Operation

Eonlets work in two modes, both first-class:

**Interactive eonlets** — you attach, you chat, you detach.
```bash
eonlet create assistant --name=alice
eonlet attach alice
```

**Scheduled eonlets** — they wake on cron triggers, do work, sleep again. You attach to inspect.
```bash
eonlet create x-digest --name=morning
# Every morning at 8am, this eonlet will fetch your X timeline,
# summarize it, and email you. You don't need to attach.
```

The bundled example agents include both modes — see [`agents/`](agents/) for templates you can copy and customize.

## What's in the Box (MVP — v0.1)

- ✅ Per-eonlet OS process with persistent state
- ✅ tmux-style attach/detach (Unix socket + JSON-RPC)
- ✅ Event-sourced SQLite store, full state restore on restart
- ✅ Scheduled triggers (cron syntax)
- ✅ Custom tools per agent (drop Python files in `tools/`)
- ✅ Skills as Markdown (Claude Code style)
- ✅ Permission system (`ask` / `yolo` modes + hardcoded deny list)
- ✅ Anthropic + OpenAI + any OpenAI-compatible endpoint (Ollama, vLLM)
- ✅ 13 builtin tools: `bash`, file ops, `web_search`, `web_fetch`, `send_email`, notes, `load_skill`, `sleep`
- ✅ Three example agents: `assistant`, `x-digest`, `portfolio`

What's deferred (and to when):

- 🚧 v0.2: MCP integration, vector memory, hooks, hibernate, textual TUI
- 🚧 v0.3: 5-layer compaction, skill marketplace, framework adapters
- 🚧 v0.4: Multi-eonlet runtime — discovery, messaging, A2A protocol
- 🚧 1.0: API freeze, security audit, cross-machine federation

See [`ROADMAP.md`](ROADMAP.md) for the full plan.

## Project Documents

- [**MANIFESTO.md**](MANIFESTO.md) — the why
- [**ROADMAP.md**](ROADMAP.md) — the when
- [**docs/SPEC.md**](docs/SPEC.md) — the master technical spec
- [**docs/AGENT_CONFIG_SPEC.md**](docs/AGENT_CONFIG_SPEC.md) — every field of `agent.yaml` explained
- [**docs/CLI_REFERENCE.md**](docs/CLI_REFERENCE.md) — every command
- [**docs/TOOL_SPEC.md**](docs/TOOL_SPEC.md) — tool interface and builtin tool catalog
- [**docs/TRIGGER_SPEC.md**](docs/TRIGGER_SPEC.md) — schedule, event, and interactive triggers
- [**docs/DIRECTORY_LAYOUT.md**](docs/DIRECTORY_LAYOUT.md) — all directories, where things go
- [**docs/SECURITY.md**](docs/SECURITY.md) — threat model and defenses
- [**docs/concepts/teams-and-organizations.md**](docs/concepts/teams-and-organizations.md) — long-term vision: specialists, teams, and organizations
- [**docs/adr/**](docs/adr/) — architecture decision records

## Long-term Direction

Eonlet's MVP is one eonlet, run well. But the project is built toward something larger: **a society of specialist agents**, organized into teams (small groups with leaders), organized into organizations (trees of teams).

We don't believe in "the one agent that does everything". We believe in many specialists, structured the way humans have always structured collaborative work. See [**MANIFESTO.md**](MANIFESTO.md) and [**docs/concepts/teams-and-organizations.md**](docs/concepts/teams-and-organizations.md) for the full picture.

Each MVP design decision keeps this future open. Today's `agent.yaml.metadata.specialty` and `metadata.capabilities` fields are forward-compatible declarations that future team-formation tools will use.

## Status

**Pre-alpha — pre-implementation.** The design is complete; code is being written. If you want to follow along or contribute, watch this repo and read the [MANIFESTO](MANIFESTO.md).

## License

Apache-2.0. See [LICENSE](LICENSE).

## Acknowledgments

Eonlet borrows ideas freely from many places. Most obviously:
- **tmux** for the attach/detach model
- **systemd** for declarative service definition
- **Erlang/OTP** for actor-style processes (Phase B)
- **Docker** for the image-vs-container abstraction
- **Letta / MemGPT** for stateful agent design (different path, same north star)
- **Claude Code** for skills, hooks, and permission patterns
- **OpenHands** for event-sourced state and immutable config
- **MCP** for tool protocol standardization
