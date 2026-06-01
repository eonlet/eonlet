# ADR-0006: Compaction Triggers — Bounded Auto, User-Forced, and Agent-Proposed Semantic Compaction

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-05-30 |
| Accepted | 2026-05-30 |
| Deciders | Ziyu |
| Supersedes | – (extends the tier-1 trigger semantics of [ADR-0003](0003-memory-system.md) / [ADR-0005](0005-dual-axis-memory.md), MEMORY_SPEC §4.1) |
| Superseded by | – |

## Context

Episodic compaction (tier-1, working → STM) today has exactly **one** trigger
and **one** boundary policy:

- **Trigger** — the post-turn cascade in `worker/main.py` (`_maybe_run_tier1`)
  fires when the working-window token estimate crosses
  `episodic.working_memory_tokens`.
- **Boundary** — `compute_suggested_boundary` (`memory/tier1.py`) keeps a tail:
  `keep_recent_messages_min` messages and ~30% of the budget stay raw in the
  working window; everything older is summarized into STM.
- The manual `memory(action="compact")` path (`/compact`) reuses the **same**
  boundary policy — it just runs the same thing on demand.

After living with this, two gaps stand out:

1. **The only control is the ceiling.** Compaction happens because the buffer
   filled up, never because the conversation *moved on*. The natural moment to
   fold away old context is often a **topic switch** that occurs well before the
   token ceiling — but nothing detects it, so stale context rides along until
   the buffer is full.
2. **No clean slate.** A user who wants to start a fresh topic but keep the
   memory ("we're done with that — new subject, but remember what we did") has
   no lever: `/compact` still leaves a 30% tail. There is no way to empty the
   working window deliberately.

### Non-goal: turn-internal compaction

An earlier sketch considered compacting *within* a single turn to bound a long
tool-calling chain. **Rejected by design.** The chat layer is a thin interface
for short user↔agent exchanges. When a single turn needs heavy, sustained,
many-step work, that is the signal to decompose it into the **task-orchestration
layer** (a future ADR) — parallel/serial tasks that report back — not to grow
one turn unbounded. So the design bounds context **per conversation**, not
**per turn**.

### Reframing insight

**The trigger determines the boundary policy.** There is one *mechanism*
(tier-1 summarize-the-old-portion-into-STM); what differs across situations is
*who decides to run it* and *where the cut lands*. This ADR keeps the single
mechanism and gives it three triggers, each with its own boundary.

## Decision

Three triggers feed the one tier-1 mechanism:

| Trigger | Initiator | Boundary policy | Consent | Availability |
|---|---|---|---|---|
| **Bounded auto** (kept) | runtime, post-turn | keep ~30% tail (`compute_suggested_boundary`, unchanged) | none | interactive + cron |
| **User-forced full** (changed) | user, `/compact` | **everything** — working emptied, no tail | n/a (user *is* the initiator) | interactive |
| **Agent-proposed semantic** (new) | agent, `propose_compact` | agent-chosen topic-shift boundary | **required, blocking** | interactive only |

### 1. Bounded auto — unchanged (the upper bound)

`_maybe_run_tier1` continues to fire post-turn at
`tokens ≥ working_memory_tokens`, keeping the tail. This is the hard guarantee
that the working window never grows without bound, and it is the **only**
trigger that applies to non-interactive (cron / autonomous) runs, which have no
user to force or approve anything.

### 2. User-forced full compaction (the clean slate)

`/compact` (and the CLI/IPC path behind it) runs tier-1 with
`suggested_boundary = store.latest_id()` — **the entire working window is
summarized into STM and emptied**, leaving no tail. Tool-pair safety still
applies, but at end-of-turn there is no in-flight call/result to split.

Because emptying the window is semantically "this episode is over, start fresh
(but remembered)," a full compaction also marks a **session boundary**: it emits
`SESSION_ENDED` then `SESSION_STARTED` (both already exist in `EventKind`). The
next user message begins a new episode whose only carried-over context is the
injected memory preamble (STM + LTM + knowledge index) and `<tasks>`.

> Agent self-service is deliberately *not* offered here. The agent has **no**
> unconsented force-compact lever — its only path to compaction is
> `propose_compact` (below). "Agent-initiated compaction always requires
> consent" stays an invariant; the unconditional full compact is reachable only
> from the user's `/compact`.

### 3. Agent-proposed semantic compaction (new)

The agent already holds the full working context, so it — not a separate
classifier pass — is the cheapest judge of "has the conversation diverged from
the older context?" (Mechanism option **a**: agent self-judgment via a tool, no
extra per-turn LLM call.)

**Mechanism.** A new action `memory(action="propose_compact",
boundary_event_id, reason)` (final tool placement TBD — `memory` action vs. a
dedicated `propose_compact` tool). The agent calls it when it judges the live
exchange has moved on from older working context, naming the **topic-shift
boundary** it wants to cut at.

**Blocking + consent-gated.** The call **suspends the agent's turn**. The
runtime surfaces `reason` and a boundary preview to the user over the
attach/IPC channel — reusing the **permission-gate presentation path**
(`PERMISSION_REQUESTED`-style) — and the agent does **no further work** until
the user answers:

- **Approve** → run tier-1 with the agent's `boundary_event_id`; the agent then
  resumes with compacted context.
- **Decline** → no compaction; the agent resumes and must not immediately
  re-propose (see cooldown).

Because the proposal blocks the turn, the working window **cannot keep growing**
while consent is pending — this is what removes the proposal-vs-ceiling race.
The proposed boundary is validated exactly like the compactor's: a known event
id, `≤ latest_id`, not splitting a tool_call/tool_result pair.

**Two configurable guards** keep proposals from being noisy:

- **Budget floor** — `propose_floor_tokens`: below this working size a proposal
  is disallowed (no point compacting a tiny context). This is the configurable
  "semantic lower bound."
- **Cooldown** — `propose_min_interval_seconds`: minimum **wall-clock** time
  since the *last* compaction (of any trigger). This is the dominant guard: it
  prevents the agent from proposing again right after a compaction just ran.

**Interactive only.** In cron/autonomous runs there is no user to approve, so
`propose_compact` is a no-op (refuses with a clear result); only bounded-auto
applies. If the ceiling is somehow already crossed when a proposal is raised,
that is harmless — approval brings tokens down immediately, and on decline the
normal post-turn bounded-auto still fires as the backstop.

### Consent is event-sourced

So that replay reconstructs the decision (state-is-derived invariant), the
proposal/decision are events, mirroring the permission triple:

- `MEM_COMPACT_PROPOSED` — `{boundary_event_id, reason, working_tokens}`
- `MEM_COMPACT_APPROVED` / `MEM_COMPACT_DECLINED`
- the compaction itself still emits `MEM_COMPACTED`.

The pending proposal is transient runtime state; only the decision is persisted.

### Per-turn timestamp injection (temporal awareness)

Independent of the three triggers but raised alongside them: each
`USER_MESSAGE` rendered into the working window is **prefixed at build time**
with its local datetime, e.g.:

```
[2026-05-30 14:23 +08:00] <user content>
```

This is **render-time only** — it is *not* written into the event payload
(events stay immutable; the timestamp already lives on `Event.ts`). It gives the
model temporal cognition of *when* episodes happened, matching STM/LTM (already
dated) and the new session boundaries. Default granularity is every user turn;
collapsing to "only when the date/hour changes" is an option if per-turn proves
noisy. Gated by `inject_turn_timestamps`.

### `agent.yaml` schema additions

```yaml
memory:
  episodic:
    working_memory_tokens: 10000        # bounded-auto ceiling (unchanged)
    keep_recent_messages_min: 4
    short_term_tokens: 4000
    long_term_tokens: 8000
    auto_compact: true
    propose_semantic: true              # enable agent-proposed compaction (interactive); default ON
    propose_floor_tokens: 5000          # don't propose below this working size
    propose_min_interval_seconds: 1800  # cooldown: wall-clock since last compaction
    propose_min_turns: 3                # cooldown: min turns since last compaction (secondary guard)
  inject_turn_timestamps: true          # prefix every user message with local datetime
```

Unknown keys keep the existing `extra="forbid"` rejection.

### Event-store changes

| Event | Fate |
|---|---|
| `MEM_COMPACTED` | kept (all three triggers emit it) |
| `SESSION_STARTED` / `SESSION_ENDED` | **reused** to mark user-forced full-compaction boundaries |
| `PERMISSION_*` | unchanged; their presentation path is reused for proposals |
| — | **new** `MEM_COMPACT_PROPOSED` / `MEM_COMPACT_APPROVED` / `MEM_COMPACT_DECLINED` |

`EventKind` grows from 36 → 39 variants.

## Consequences

### Positive

- Compaction can finally be driven by **meaning** (topic switch), not only by a
  full buffer — the stale-context-rides-along problem is addressed without
  guessing thresholds.
- The user gets a real **clean-slate** lever (`/compact` empties working) that
  doubles as an explicit episode/session boundary — a natural anchor the future
  task-orchestration layer and date-scoped `recall` can key off.
- "Agent-initiated compaction always needs consent" is a clean, auditable
  invariant; the event log faithfully records every proposal and its outcome.
- The blocking proposal **eliminates** the await-consent-vs-ceiling race by
  construction, rather than papering over it with priority rules.
- Timestamped turns give the model temporal awareness cheaply (render-time, no
  storage churn, no new immutability risk).

### Negative

- Touches `worker/main.py` (trigger dispatch), `memory/tier1.py` (full-boundary
  mode), the `memory` tool, the attach/IPC consent round-trip, `EventKind`
  (+3), `MemoryConfig` (+4 fields), MEMORY_SPEC §4, and the bundled templates.
- A blocking, user-gated tool call is a **new interaction shape** in the runtime
  (the agent loop must suspend mid-turn awaiting an out-of-band answer). The
  permission gate is the precedent but it gates *tool execution*, not *the agent
  pausing to ask a meta-question* — the plumbing differs.
- Proposal quality depends on prompt tuning; the agent may over- or under-
  propose. Acceptable pre-alpha, bounded by the floor + cooldown guards.

### Neutral

- Non-interactive behavior is unchanged (bounded-auto only), so cron agents are
  unaffected.
- `memory.enabled: false` still disables all compaction and all three triggers.

## Alternatives Considered

### A. Turn-internal compaction to bound long tool chains
Rejected (see Non-goal): heavy multi-step work belongs in the task layer, not in
growing one turn. Bounding per-conversation, not per-turn.

### B. A separate per-turn relatedness classifier instead of agent self-judgment
Rejected: costs an extra LLM call every turn and duplicates judgment the agent
can already make from context it already holds. Option **a** (tool-based self-
judgment) is cheaper and keeps the decision legible in the event log.

### C. Let the agent force-compact without consent
Rejected: silent, agent-driven context loss is exactly the surprise the consent
gate exists to prevent. The agent proposes; the user disposes. Unconditional
full compaction stays a user-only lever.

### D. Non-blocking proposal (fire-and-continue)
Rejected: if the agent keeps working while the proposal is outstanding, the
working window keeps growing and the boundary the user approved may already be
stale, reintroducing the race. Blocking is the simpler, correct model.

### E. Store the per-turn timestamp in the event payload
Rejected: events are append-only and immutable; `Event.ts` already carries the
time. Prefixing is a pure render-time concern.

## Resolved Decisions

Resolved during the design discussion (2026-05-30):

1. **Tool placement → action on `memory`.** `memory(action="propose_compact",
   boundary_event_id, reason)`, not a new tool — consistent with ADR-0005's
   surface-shrinking.
2. **`propose_semantic` default → ON.** Discoverable out of the box; the floor +
   cooldown guards keep it from being noisy.
3. **Cooldown → wall-clock AND turns.** Both guards apply:
   `propose_min_interval_seconds` (wall-clock, the dominant guard) and
   `propose_min_turns` (secondary, for very rapid exchanges). A proposal is
   allowed only when *both* have elapsed since the last compaction.
4. **Timestamp granularity → every user message.** Per-turn `[YYYY-MM-DD HH:MM
   ±ZZ]` prefix at render time; no collapse-on-change.
5. **Consent transport → (A) one generic `user_decision` round-trip.** The
   blocking "ask the user" channel must be built either way (the permission
   gate's interactive confirm was deferred in v0.0.2 and never built —
   `permissions/gate.py` auto-allows destructive tools when a session is
   attached; IPC today has only one-way server→client notifications). We build
   **one** generic, minimal round-trip — envelope `{id, kind, prompt, options,
   payload}`, a worker-side `PendingDecisions` registry, and a single
   `decision.respond` IPC request — and route **both** compaction proposals
   (`kind:"compaction"`) and the long-deferred interactive permission confirm
   (`kind:"permission"`) through it. The envelope stays minimal (no speculative
   fields); the two cases differ only in `payload`, while the user-facing shape
   (one prompt + yes/no) is identical.
6. **`yolo` auto-approves proposals.** Under `yolo`, a proposal is **not**
   blocked: it is auto-approved without a user round-trip, but still recorded as
   `MEM_COMPACT_PROPOSED` followed by `MEM_COMPACT_APPROVED` (`rule:"yolo"`) for
   audit. The "must wait for user confirmation" contract holds for `ask` mode;
   `yolo` keeps its "don't stop to ask me" promise. The floor + cooldown guards
   still apply, so `yolo` cannot cause compaction thrash, and the event trail
   keeps every auto-approved compaction visible in `tail`/`replay`.

No open questions remain.

## References

- [ADR-0003](0003-memory-system.md) — original tier-1 mechanism + watermark
- [ADR-0005](0005-dual-axis-memory.md) — dual-axis model; episodic axis this trigger work sits on
- [MEMORY_SPEC.md](../MEMORY_SPEC.md) §4 — Compaction Pipeline (to gain the trigger/boundary matrix)
- `memory/tier1.py` — `compute_suggested_boundary` (gains a full-boundary mode)
- `worker/main.py` — `_maybe_run_tier1` (gains trigger dispatch)
- `permissions/gate.py` — the consent/IPC precedent the proposal flow mirrors
- Future task-orchestration ADR — where heavy multi-step work goes instead of turn-internal compaction; shares the session-boundary anchor

## Update history

- 2026-05-30: Initial proposal.
- 2026-05-30: All open questions resolved during design discussion (memory
  action; `propose_semantic` default ON; dual wall-clock + turn cooldown;
  per-message timestamps; consent transport → one generic `user_decision`
  round-trip shared with the deferred permission confirm; `yolo` auto-approves
  with an audit trail). Status → Accepted. Companion implementation plan at
  [plans/compaction-triggers.md](../plans/compaction-triggers.md).
