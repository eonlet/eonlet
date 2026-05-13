# Concept: Specialty, Teams, and Organizations

> The long-term vision for Eonlet beyond v1.0. This document is **forward-looking** — none of these capabilities exist in MVP or even in Phase B. They are documented now to give the project a clear north star and to ensure today's decisions don't foreclose tomorrow's possibilities.

| Field | Value |
|---|---|
| Status | Vision — not yet implemented |
| Implementation horizon | Phase C (v0.6+) for teams, Phase D (v0.8+) for organizations |
| Affects MVP? | No — forward-compatible metadata fields only |
| Affects design today? | Yes — we make MVP decisions that keep this future open |

## 1. The Core Belief

> **An agent should be a specialist, not a god.**

Most agent demos today try to build the One Agent To Rule Them All — an LLM with every tool, every skill, infinite context, and the wisdom of all professions. This fails at scale for two reasons:

1. **Context dilution.** Telling a model it's "an expert in everything" produces an agent that is mediocre at most things. Specialists outperform generalists in any specific task.
2. **Operational opacity.** When one super-agent does everything, you can't see, evaluate, or improve any one capability in isolation. You can only thumb-down the whole thing.

Eonlet's bet is the opposite: **many small specialists, structured into teams, structured into organizations**, with explicit relationships between them. The same shape human organizations have found over millennia, applied to AI agents.

## 2. Three Levels of Aggregation

```
                    Organization
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
            Team A      Team B     Team C
              │           │           │
         ┌────┴───┐   ┌───┴───┐   ┌──┴──┐
         ▼        ▼   ▼       ▼   ▼     ▼
     Eonlet  Eonlet  Eonlet  Eonlet  ...
```

### Level 1: Eonlet (the individual)

A single agent, defined by its `agent.yaml`. It has:

- **A specialty** — what it's best at (e.g., `code_review`, `web_research`, `portfolio_analysis`)
- **A set of capabilities** — concrete actions it can perform (e.g., `analyze.python_code`, `find.security_issues`)
- **Tools** to execute its capabilities
- **Memory** of its past work
- **No knowledge of org structure** — it just does its job when asked

An eonlet operates well standalone (Phase A) or as part of a team (Phase C).

### Level 2: Team (the collaboration unit)

A **team** is a small group of eonlets coordinated by a designated **leader eonlet**. Teams exist when one agent isn't enough — when a problem decomposes into pieces requiring different specialists.

Concrete structure:

- **Leader** — one eonlet, designated by team config. Receives external requests, decomposes work, delegates to members, synthesizes results.
- **Members** — N eonlets (typically 2–7), each a specialist. Receive sub-tasks from the leader, execute, return results.
- **Team memory** — shared notes the team accumulates over time (separate from individual eonlets' private memory).
- **Team identity** — a name, a charter, a track record.

Example: a **research-and-write team**:
- *Leader*: `editor` — receives "write a 1500-word piece on X", decomposes into research/draft/polish
- *Member*: `researcher` — does web research, hands back structured findings
- *Member*: `writer` — drafts from findings
- *Member*: `fact_checker` — verifies claims before publication

The team has continuity. Same `editor` eonlet, same `researcher` eonlet, week after week. They build up shared context.

### Level 3: Organization (the structure of structures)

An **organization** is a tree of teams. Like a company:

```
Investment Office (org)
├── Research Department (team)
│   ├── Equity Research (sub-team)
│   │   ├── analyst.tech_specialist (eonlet)
│   │   └── analyst.macro_specialist (eonlet)
│   └── News Curation (sub-team)
│       └── x-digest (eonlet)
├── Execution Department (team)
│   ├── portfolio (eonlet)
│   └── trade_logger (eonlet)
└── Operations (team)
    ├── compliance_reviewer (eonlet)
    └── tax_tracker (eonlet)
```

Each box at the team level has its own leader, its own charter, its own memory. Communication flows up and down the tree by default; cross-team communication is mediated through the common ancestor.

The organization is **declarative** — described by an `org.yaml` at `~/.eonlet/orgs/<name>/`. The organization itself isn't a process; it's a description of how teams are arranged.

## 3. Why Hierarchy Matters

There's a school of thought in multi-agent systems that says "let agents communicate freely, emergence will arise". Eonlet rejects this approach.

**Flat networks scale poorly.** With N agents, there are N² possible communication paths. Each agent must reason about all the others. This is exactly why human organizations stopped being flat tribes once they exceeded ~150 people (Dunbar's number). Hierarchy is a *scaling solution*.

**Flat networks have no chain of accountability.** When a flat agent network produces a wrong output, who do you blame? In a hierarchy, the team leader owns the team's output. The org leader owns the org's output. You can debug, you can improve, you can replace.

**Flat networks blur specialization.** When every agent can talk to every agent, each one drifts toward being a generalist (to handle the variety of conversations). Hierarchy enforces the discipline: members talk to leaders, leaders talk to peer leaders, specialization is preserved.

That said, Eonlet's hierarchy is not rigid. The model is:
- **Routine work** flows up and down the tree (most communication)
- **Emergency / opportunistic work** allows lateral peer messaging (sparingly)
- **Discovery / federation** allows any agent to find any other via the capability registry (read, not write)

## 4. The Forward-Compatible Fields

Even today, in Phase A, agent definitions should declare:

```yaml
metadata:
  specialty: portfolio_analysis           # one short phrase
  capabilities:                            # dotted-notation verbs
    - "read.broker_positions"
    - "analyze.equity_holdings"
    - "scan.market_news"
    - "report.investment_decisions"
```

The MVP runtime ignores these. But authoring them now means:

1. **Self-documentation.** The author thinks about what the agent is *for*, not just what it does.
2. **Future-readiness.** When team-formation tools land in Phase C, your existing agent is discoverable.
3. **Discipline.** An agent whose `capabilities` list grows beyond ~8 entries is probably not a specialist anymore — refactor it into two.

### Capability naming convention

`<verb>.<object>` in lowercase snake_case:

| Good | Bad |
|---|---|
| `analyze.equity_holdings` | `do_analysis` |
| `summarize.long_documents` | `summarize` |
| `draft.email` | `email` |
| `review.python_code` | `code` |

The first word is what the agent *does*. The second is what it does it *to*. Two-part. Specific. Searchable.

When a team needs "someone to summarize long documents", the leader will search the capability registry for `summarize.long_documents` — exact match wins, prefix match (`summarize.*`) is fallback.

## 5. How Teams Form (Phase C preview)

A team is described by `~/.eonlet/teams/<name>/team.yaml`:

```yaml
apiVersion: eonlet/v1
kind: Team
metadata:
  name: research-and-write
  description: "Research a topic, draft a piece, fact-check, publish."

leader: editor.alice                       # an eonlet id (must exist)

members:
  - role: researcher
    eonlet: researcher.bob
    capabilities_used:
      - "research.web"
      - "summarize.findings"
  - role: writer
    eonlet: writer.carol
    capabilities_used:
      - "draft.long_form"
  - role: fact_checker
    eonlet: fact_checker.dave
    capabilities_used:
      - "verify.claims"

memory:
  shared_notes: team_notes.md              # team-wide memory

charter: |
  This team produces 1-2 long-form pieces per week.
  Leader receives the topic, decomposes into research + draft + check.
  Cycle target: 48 hours from topic to published draft.
```

Team operations:

- `eonlet team create <name>` — register a team
- `eonlet team list` — see all teams
- `eonlet team send <name> "<task>"` — give the team a task (routes to leader)
- `eonlet team status <name>` — see what's in progress

Internal communication uses the same A2A protocol as peer-to-peer (v0.4), but with team-specific message envelopes (`leader → member`, `member → leader`).

## 6. How Organizations Form (Phase D preview)

An organization is `~/.eonlet/orgs/<name>/org.yaml`:

```yaml
apiVersion: eonlet/v1
kind: Organization
metadata:
  name: investment-office
  description: "My personal investment operation"

structure:
  - team: research_department
    children:
      - team: equity_research
      - team: news_curation
  - team: execution_department
    children: []
  - team: operations
    children: []

org_leader: research_department.editor    # team.role addressing
```

The organization is a *structural description* — it doesn't run anything. It just tells the framework how teams relate.

Operations:

- `eonlet org create <name>`
- `eonlet org topology <name>` — render the tree
- `eonlet org send <name> "<task>"` — task routes to org-leader, who decides which team

## 7. Implications for MVP Design

Even though we don't implement teams in Phase A, the MVP design must not foreclose this future. Specifically:

- **Eonlets are addressable by stable IDs** (`<type>.<name>`) — these become team membership references.
- **Event store records who did what** — when teams form, we have an audit trail of each member's work.
- **A2A protocol (v0.4)** is the inter-eonlet substrate — teams and orgs sit on top of it, not parallel to it.
- **Capabilities are declared in `agent.yaml.metadata`** — forward-compat field, populated by users today.
- **Permission system is local to each eonlet** — when teams form, each member still owns its permissions; the team leader cannot bypass them.

## 8. Patterns We Expect

When teams arrive, we expect to see these recurring patterns:

### 8.1 The Lead-Worker pattern (Anthropic's multi-agent style)

A leader receives a task, spawns N workers in parallel, synthesizes. Best for "embarrassingly parallel" work like research-across-many-sources.

### 8.2 The Pipeline pattern (assembly line)

Member A produces, hands to member B who refines, hands to member C who polishes. Best for sequential work with clear handoffs.

### 8.3 The Critic-Producer pattern

A producer drafts, a critic reviews, producer revises, critic re-reviews. Two eonlets, two roles, one task — explicit dialectic.

### 8.4 The Manager-of-Managers pattern (organization-level)

A high-level leader doesn't talk to specialists directly — they talk to team leaders who manage the specialists. Useful when the total number of agents exceeds a span of control (~7).

## 9. Anti-Patterns We'll Resist

### Don't recreate the bureaucracy

The temptation in a hierarchical multi-agent system is to add layers, approval gates, formal handoff procedures. We will resist this. A team has a leader and members, period. An org has team-of-teams structure, period. No "manager track" agents that don't do specialist work.

### Don't have every agent be a leader

A team without specialists is just a leader passing tasks to itself. A specialist who's also a leader is a specialist who's distracted. **Most eonlets should be pure specialists.** Leaders are a small minority.

### Don't try to be a knowledge graph

An organization is a *structural* construct, not a semantic one. We are not building a knowledge graph of relationships, dependencies, or causal links between agents. We are building a tree that says who reports to whom. That's it.

## 10. Open Questions for Phase C/D

These will be ADRs when we get there:

- **Lifecycle**: when a team's leader dies, what happens? (Likely: team marked `degraded`, manual intervention required to designate new leader.)
- **Cost accounting**: which budget is charged for team work — leader's, members', team's? (Likely: team has its own budget, charged proportionally.)
- **Trust**: can a member refuse a leader's task? (Likely: yes, via permission system — leader can request, member's gate decides.)
- **External access**: can someone outside the team message a member directly? (Likely: yes, but only via the leader or org tree.)
- **Forking**: can a team be cloned with new members? (Likely: yes, via `team fork` analogous to `eonlet fork`.)
- **Dissolution**: how does a team end? (Likely: `team disband`, archives memory, members return to standalone.)

## 11. Why This Is Worth The Wait

We won't ship any of this in Phase A or Phase B. Why even write it down?

Because a project's long-term direction shapes its short-term decisions. The fact that we're aiming at "society of specialists" means:

- We design eonlet IDs to be stable references (so they can be team-membered later)
- We make A2A the inter-eonlet substrate (so teams sit on top, not parallel)
- We populate `specialty` and `capabilities` fields now (so future tools find them)
- We resist the temptation to make MVP eonlets generalists (so the specialist culture is native)
- We document this vision so contributors know where we're going

When v0.6 lands with team support, no one should be surprised. The runway will have been clear for months. The first teams will form naturally — from agents that were already specialists, in directories that were already addressable, communicating via a protocol that was already standardized.

---

> The interesting thing about a society of agents is that it can do things no single agent can. Not because the agents are smarter, but because the structure is wiser.
