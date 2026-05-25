# Documentation Index

> The single page that points to every other page. Bookmark this.

## Start Here

| Document | What's In It |
|---|---|
| [`../README.md`](../README.md) | Project overview, quickstart, 30-second pitch |
| [`../MANIFESTO.md`](../MANIFESTO.md) | The "why" — design philosophy and convictions |
| [`../ROADMAP.md`](../ROADMAP.md) | Timeline, phased milestones, what's coming when |

## Technical Specifications (read in this order)

| # | Document | What's In It |
|---|---|---|
| 1 | [`SPEC.md`](SPEC.md) | Master technical spec — the single source of truth |
| 2 | [`AGENT_CONFIG_SPEC.md`](AGENT_CONFIG_SPEC.md) | The `agent.yaml` schema in full detail. **The most important doc for users.** |
| 3 | [`DIRECTORY_LAYOUT.md`](DIRECTORY_LAYOUT.md) | Every file and directory Eonlet creates or expects |
| 4 | [`CLI_REFERENCE.md`](CLI_REFERENCE.md) | Every CLI command and option |
| 5 | [`TOOL_SPEC.md`](TOOL_SPEC.md) | Tool interface and the 21+ builtin tools |
| 6 | [`TRIGGER_SPEC.md`](TRIGGER_SPEC.md) | Cron and interactive triggers |
| 7 | [`MEMORY_SPEC.md`](MEMORY_SPEC.md) | Memory subsystem — storage layout, three-tier compaction, FTS5 recall |
| 8 | [`SECURITY.md`](SECURITY.md) | Threat model, defenses, and explicit limits |

## Plans (in-flight design work)

| Document | What's In It |
|---|---|
| [`plans/web-tools.md`](plans/web-tools.md) | Implementation plan for the v0.1 web-tools upgrade (companion to ADR-0004) |

## Architecture Decision Records

| # | Title | Status |
|---|---|---|
| [0001](adr/0001-no-supervisor-mvp.md) | No Supervisor in MVP | Accepted |
| [0002](adr/0002-dynamic-triggers.md) | Dynamic Triggers — In-Conversation Schedule Management | Accepted (shipped v0.0.2) |
| [0003](adr/0003-memory-system.md) | Memory System — Compaction, LTM, Notes, TODOs, Recall | Accepted (shipped v0.0.6) |
| [0004](adr/0004-web-tools.md) | Web Tools — Search Provider Abstraction and Structured Fetch Pipeline | Proposed |

ADRs document *why* we made specific architectural decisions. New ADRs follow the template in 0001.

## Concepts

| Document | What's In It |
|---|---|
| [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md) | **Long-term vision (Phases C/D):** specialist agents → teams → organizations. Forward-looking; MVP doesn't implement, but design accommodates. |

Additional concept docs will be added with v0.2+ when implementation matures:

- `concepts/eonlet.md` — the eonlet primitive in depth
- `concepts/event-sourcing.md` — why event sourcing, how it works here
- `concepts/triggers.md` — narrative explanation of the trigger model
- `concepts/definition-vs-instance.md` — the immutable/mutable boundary

## Tutorials (Future)

Coming with v0.1.0 release:

- `tutorials/01-first-eonlet.md` — "Hello, alice"
- `tutorials/02-build-x-digest.md` — from scratch
- `tutorials/03-build-portfolio.md` — multi-tool, multi-trigger

## Contributing

| Document | When to Read |
|---|---|
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | Before opening any issue or PR |

## Doc Maintenance Rules

- Specs (`SPEC.md`, `AGENT_CONFIG_SPEC.md`, etc.) state intent. Code matches them, not the other way around.
- Major changes to specs require a new ADR explaining why.
- All docs have a `Last updated` line; update it when changing.
- Cross-references use relative paths; check them when restructuring.
- Code examples in docs must compile (we will have a CI check for this post-v0.2).

## Doc Map

```
eonlet/
├── README.md                   ← landing page
├── MANIFESTO.md                ← values
├── ROADMAP.md                  ← timeline
├── CONTRIBUTING.md             ← how to participate
├── CHANGELOG.md                ← release notes
└── docs/
    ├── INDEX.md                ← (this file) you are here
    ├── SPEC.md                 ← master spec
    ├── AGENT_CONFIG_SPEC.md    ← agent.yaml deep dive
    ├── DIRECTORY_LAYOUT.md     ← all file layouts
    ├── CLI_REFERENCE.md        ← CLI man page
    ├── TOOL_SPEC.md            ← tool interface + builtins
    ├── TRIGGER_SPEC.md         ← schedule and event triggers
    ├── SECURITY.md             ← threat model
    ├── MEMORY_SPEC.md          ← memory subsystem spec
    ├── adr/                    ← architecture decisions
    │   ├── 0001-no-supervisor-mvp.md
    │   ├── 0002-dynamic-triggers.md
    │   ├── 0003-memory-system.md
    │   └── 0004-web-tools.md
    ├── plans/                  ← in-flight implementation plans
    │   └── web-tools.md
    ├── concepts/                ← conceptual narrative docs
    │   └── teams-and-organizations.md
    └── tutorials/              ← (future) step-by-step guides
```
