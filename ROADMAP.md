# Eonlet Roadmap

This roadmap is published intentionally — to set expectations, to invite feedback, and to make the project's pace transparent.

**Working principle:** ship Phase A (single-eonlet excellence) before starting Phase B (multi-eonlet). Don't promise what isn't proven yet.

## Phase A — Single Eonlet Excellence

The goal of Phase A is one eonlet so good that the author lives inside it daily, and so polished that a stranger arriving from Hacker News stays for the second session.

### v0.0.x — Personal Prototype (Month 1)

**Goal:** the author can run an eonlet locally and use it for real work.

- [x] Repository scaffolding, CI, lint, type-check
- [x] Worker process: agent loop, event store, Anthropic + OpenAI calls
- [x] CLI: `init`, `create`, `attach`, `ls`, `kill`, `rm`
- [x] Runtime IPC: Unix socket + JSON-RPC + event stream
- [x] Pause / resume via SIGSTOP / SIGCONT
- [x] Builtin tools (v0.0.6: **21+ tools**, up from the v0.0.1 plan of 13): `bash`, `file_read`, `file_write`, `file_edit`, `glob`, `grep`, `web_search`, `web_fetch`, `send_email`, `sleep`, `load_skill`, `schedule`, `memory`, `note`, `todo`, `recall`, `remember`, `forget` (legacy `notes_read`/`notes_append` removed in v0.0.6 — superseded by `note`/`todo`/`recall`)
- [x] Default `assistant` agent template (plus `x-digest` and `portfolio` shipped early in v0.0.2/v0.0.3)
- [x] LLM streaming (v0.0.4 — `LLMProvider.stream()` + `on_delta` callback)
- [x] FakeProvider + worker integration tests + mypy strict + ruff strict + ≥70% branch coverage (v0.0.5)
- [x] Full memory subsystem — STM/LTM/notes/todos/FTS5 recall + three-tier compaction cascade (v0.0.6; see [ADR-0003](docs/adr/0003-memory-system.md))

**Done condition:** author replaces Claude Code with Eonlet for daily research and writing tasks, runs continuously for one week, no P0 blocker.

**Deviation from original v0.0.x plan:** memory was originally a two-tool slice (`notes_read`/`notes_append`) scheduled for v0.1, with vector memory promised for v0.2. The two-tool slice was scrapped in v0.0.6 in favour of a complete hierarchical memory subsystem (working → STM → LTM + notes + todos + FTS5 recall + LLM-driven compaction), absorbing what would have been the v0.2 vector-memory slot's keyword/structural half. Vector/semantic recall remains a v0.2+ item and will live alongside the v0.0.6 system, not replace it.

### v0.1.0 — MVP, Installable Alpha (Month 2)

**Goal:** anyone can `pip install eonlet` and reach the [MVP user story](docs/SPEC.md#3-personas--mvp-user-story) in five minutes.

- [ ] PyPI release with macOS + Linux wheels
- [x] **Scheduled triggers** (cron syntax in `agent.yaml`) — v0.0.2; see [ADR-0002](docs/adr/0002-dynamic-triggers.md)
- [x] **Custom tools per agent** (Python files in `tools/`) — v0.0.1
- [x] **Skills** (Markdown files loaded into context on-demand) — v0.0.1
- [x] **Environment variable handling** (declared in `env.required`, validated at startup) — v0.0.1
- [x] Permission system: `ask` + `yolo` modes + hardcoded deny list — v0.0.1
- [x] Three production-quality example agents: `assistant`, `x-digest`, `portfolio` — v0.0.3
- [ ] **Web tools — serious design** (provider abstraction + structured fetch pipeline) — see [ADR-0004](docs/adr/0004-web-tools.md) and [`plans/web-tools.md`](docs/plans/web-tools.md). Promoted into v0.1 scope on 2026-05-26 because reliable `web_search`/`web_fetch` is the foundation of autonomous research, which is one of v0.1's promised user stories.
- [ ] Complete `docs/` site (concepts, tutorial, reference)
- [ ] 30-second demo GIF in README

**Done condition:** five external alpha users active. Author dogfoods two weeks with no P0 bug.

**Engineering surplus already delivered beyond original v0.1 scope:** LLM streaming (v0.0.4), `FakeProvider` + worker integration tests + mypy/ruff strict + ≥72.6% branch coverage (v0.0.5), full memory subsystem (v0.0.6, originally a v0.2 item). Remaining v0.1 work is mostly non-engineering (PyPI release, demo GIF, two-week dogfood) plus the web-tools upgrade.

### v0.2.0 — Polish (Month 3–4)

**Goal:** make v0.1 not just usable but pleasant.

- [ ] MCP integration (client mode — connect to external MCP servers)
- [ ] sqlite-vec semantic memory + `memory_save` / `memory_search` tools
- [ ] Hooks (`pre_tool_use`, `post_tool_use`, `on_error`)
- [ ] Permission allow/deny patterns + `read_only` and `plan` modes
- [ ] textual TUI for `eonlet attach` (split panes, progress bars, notes preview)
- [ ] Multi-session attach (one master, multiple read-only followers)
- [ ] Hibernate / resume (serialize state, free RAM)
- [ ] OpenTelemetry tracing (default: Logfire)

**Done condition:** v0.2 ships when v0.1 has stabilized — typically 4–6 weeks after v0.1.

### v0.3.0 — Public Launch (Month 5–6)

**Goal:** Hacker News front page, ~3000 stars, ~10 external contributors.

- [ ] 5-layer compaction (Claude Code style)
- [ ] Skill marketplace / registry conventions
- [ ] Framework adapter for smolagents (run smolagents inside eonlet)
- [ ] Performance: 100 concurrent eonlets on one machine
- [ ] Polished documentation site (mkdocs-material on `eonlet.dev`)
- [ ] 5+ technical blog posts published
- [ ] Public launch on HN / Reddit / Twitter / lobste.rs

**Done condition:** post-launch 30 days: stars > 2000, ≥10 contributor PRs merged.

### v0.3.x — Stabilize (Month 7–8)

Bug fixes, performance, polish. No new features. Goal is to make v0.3 rock-solid before Phase B.

**Phase A → Phase B gate** (end of Month 8): unless v0.3 is widely used and stable, *do not* start Phase B. Continue polishing Phase A.

## Phase B — Multi-Eonlet Runtime

Only begin Phase B after Phase A is unambiguously successful (≥ 3000 stars, active community, no major instability).

### v0.4.0 — Multi-Eonlet Basics (Month 9–10)

- [ ] Introduce `eonletd` supervisor daemon
- [ ] Local eonlet discovery (registry maintained by supervisor)
- [ ] Synchronous inter-eonlet RPC
- [ ] `discover` and `topology` CLI commands
- [ ] Migration path: v0.3 deployments work without changes; supervisor is opt-in

### v0.5.0 — Inter-Eonlet Messaging (Month 11)

- [ ] Asynchronous message mailbox
- [ ] A2A protocol compatibility (agent card, JSON-RPC endpoints)
- [ ] Topology visualization
- [ ] Inter-eonlet security boundary (peer messages tagged as low-trust)
- [ ] Capability registry — `specialty` and `capabilities` from `agent.yaml` become discoverable

**Phase B → Phase C gate:** at least one organic community usage of peer messaging documented before designing Phase C.

## Phase C — Teams (Specialist Coordination)

The first structural primitive above the individual eonlet. See [`docs/concepts/teams-and-organizations.md`](docs/concepts/teams-and-organizations.md) for the full vision.

### v0.6.0 — Team Primitive (Month 12–13)

- [ ] `~/.eonlet/teams/<name>/team.yaml` schema
- [ ] `eonlet team create / list / status / send / disband` commands
- [ ] Team leader designation; leader-member message envelopes
- [ ] Shared team memory (`team_notes.md`)
- [ ] Capability-based discovery within a team
- [ ] Two bundled example teams (e.g., `research-and-write`, `news-curation`)

### v0.7.0 — Team Patterns and Polish (Month 14)

- [ ] Lead-Worker pattern primitives (parallel delegation)
- [ ] Pipeline pattern primitives (sequential handoff)
- [ ] Critic-Producer pattern primitives
- [ ] Per-team budget accounting
- [ ] Team-level audit log and observability

## Phase D — Organizations (Trees of Teams)

### v0.8.0 — Organization Primitive (Month 15–16)

- [ ] `~/.eonlet/orgs/<name>/org.yaml` schema (tree of teams)
- [ ] `eonlet org create / list / topology / send` commands
- [ ] Cross-team routing through common ancestor
- [ ] Org-level resource arbitration
- [ ] One bundled example org (e.g., `investment-office`)

### v0.9.0 — Federation and Remote (Month 16–17)

- [ ] Cross-machine peer list
- [ ] Federated team discovery
- [ ] Remote attach over TLS
- [ ] Optional mDNS for LAN auto-discovery

## 1.0.0 — Stable (Month 18+)

- [ ] API freeze; SemVer strict from here on
- [ ] Third-party security audit
- [ ] Migration guides from all major competitors (Letta, Claude Code subagents, raw scripts)
- [ ] Documentation in Chinese and Japanese
- [ ] At least three published case studies of production users
- [ ] 1.0 launch blog and conference talk

## After 1.0

Possible directions (not commitments):

- Code execution mode (CodeAct-style — runs in sandbox)
- Pluggable sandbox runtimes (subprocess + seccomp / Docker / E2B)
- Framework adapters: LangGraph, AutoGen, Pydantic AI (run their agents as eonlets)
- Hosted offering (`eonlet.cloud`) for users who want managed eonlets
- Enterprise edition (SSO, audit log export, policy engine)
- Skill marketplace
- Web UI as full first-class interface
- Mobile clients for "attach from phone"

## How to Contribute

Phase A is built primarily by the founding author. External contribution gates open at v0.2:

- **v0.0–v0.1:** code contributions limited (architecture stability matters more than help). Bug reports, feature requests, and documentation PRs warmly welcomed.
- **v0.2+:** code contributions open. Look for `good-first-issue` labels.
- **v0.3+:** roadmap input. RFC process opens, community votes on priorities.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for details.

## How This Roadmap Will Change

This is a living document. It will be revised in two cases:

1. **Reality says no.** A feature turns out to be much harder, the design needs rework. We will update timelines transparently.
2. **Users say no.** If real usage data shows a planned feature isn't wanted, we drop it or move it down the list.

This document will *not* be revised because:

- Someone promises money for a faster timeline
- A competitor ships a similar feature
- A trending topic on Twitter suggests new directions

Focus is a feature.
