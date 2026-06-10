# ADR-0009: Hierarchical Task Context Management — Asymmetric Context Flow over One Event Log

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-06-02 |
| Accepted | 2026-06-02 |
| Deciders | Ziyu |
| Supersedes | – (completes [ADR-0007](0007-task-scheduling.md) §4 *per-task context*; interacts with [ADR-0006](0006-compaction-triggers.md) episodic compaction; builds on [ADR-0008](0008-user-input-preemption.md) scheduling) |
| Superseded by | – |

## Context

Multi-task — and specifically **tree-structured** task — execution is where an
agent's context management succeeds or fails. ADR-0007 §4 promised that a task run
gets a working context **assembled on demand** (`goal` + parent-chain + the task's
`progress_summary` + relevant `knowledge`/`recall`), explicitly *not* a separate
long-lived conversation per task (its rejected Alternative C). The shipped code
delivered the *framing assembly* (`tasks/context.py:build_task_prompt`) but **not
the isolation, and not the right flow of context between tree levels**.

### The implementation gap

A task run is dispatched via `runtime.handle_user_message(task_prompt)`
(`worker/main.py:_run_task`), which records the framing as an ordinary
`USER_MESSAGE` into the **single global timeline** (`AgentState.messages`). The LLM
window is sliced by `_build_llm_messages` from that one list under a **single global
watermark** (`runtime/agent.py:452`; confirmed: tier-1 reads *all* events since the
watermark with no source filtering, `memory/tier1.py`; recall indexes *all* turns,
`memory/recall.py`). So while running task B the window is the global interleaving
of task A's turns + ad-hoc chat + cron turns + B's framing. Symptoms: context
cross-talk, per-beat cost on irrelevant history, and timeline bloat (every
preemption re-appends B's framing). And `_checkpoint_summary` is **purely
structural** — it grabs the last assistant message, not the task's key decisions.

### What the field has learned (two schools)

Industry practice on hierarchical agent context splits into two camps, and the
split is instructive:

- **Isolation school** — Claude Code subagents, Anthropic's multi-agent Research,
  Manus. A subagent runs in its **own context**; *"intermediate tool calls and
  results stay inside the subagent; only its final message returns to the parent."*
  Anthropic reports +90% on research evals from parallel isolated subagents — at
  ~15× the tokens. Optimized for **noisy, parallel, well-bounded** subtasks; keeps
  the parent clean.
- **Shared-trace school** — Cognition ("Don't Build Multi-Agents"). Two principles:
  (1) *"Share context, and share full agent traces, not just individual
  messages"* — **subtasks fail when they lack understanding of how the parent task
  was decomposed**; (2) *"actions carry implicit decisions, and conflicting
  decisions carry bad results."* Their conclusion: prefer a **single-threaded**
  agent, and for work beyond the context window introduce a **dedicated LLM that
  compresses the history of actions & conversation into key details, events, and
  decisions** — preserving *decision continuity*.

These look opposed but target different failure modes. The isolation school
prevents the parent from drowning in child noise (a flow *up* the tree). The
shared-trace school prevents a child from making decisions incoherent with the
parent's plan (a flow *down* the tree). Cognition's critique is specifically of
**parallel** subagents making **concurrent conflicting** decisions.

### Eonlet's position

Eonlet is a **single human-like worker**: at most one task runs at a time
(ADR-0007), serial execution (ADR-0008 §1). That is exactly the architecture
Cognition endorses, and it **structurally eliminates the concurrent-conflict
failure mode** the multi-agent debate centers on. So eonlet is free to take the
**best of both**: isolate noise on the way *up* (the parent doesn't see child
internals) while sharing the decomposition trace on the way *down* (a child
understands why it exists). The constraint to honor — same as ADR-0007 — is **one
event log, one `fold`, one `_record`** (Invariant #1): the solution must be a
*view over the log*, never a second mutable conversation store or a per-task
provider session.

## Decision

Adopt an **asymmetric context-flow model** for the task tree, implemented as
**scoped views over the single event log** plus an **LLM compression step** reused
from the memory compactor. Three rules govern how context moves between tree
levels; everything else is the substrate that realizes them.

### 1. The context-flow model (the conceptual core)

```
            chat scope  (the user conversation; episodic memory lives here)
                │  creates a root task → passes a compressed "why" downward
                ▼
        ┌──────────────── root task T (origin=user) ────────────────┐
        │  own working scope (T's turns) + framing from above        │
        │      │ decomposes → passes decision trace downward         │
        │      ▼                                                     │
        │   child C1 ──result──┐   child C2 ──result──┐  (siblings:  │
        │   (own scope)        │   (own scope)        │   no direct  │
        │                      ▼                      ▼   sharing)   │
        │            synthesis turn of T: own scope + child RESULTS  │
        └────────────────────────────────────────────────────────────┘
                │  T completes → result surfaces upward into chat
                ▼
            chat scope sees T's RESULT (not T's internals)
```

- **Down the tree (parent → child): share the *decision trace*, compressed.** A
  child's framing carries not just its `goal` but a compressed snapshot of the
  parent's relevant reasoning/decisions (Cognition Principle 1 — the antidote to
  "subtasks fail when they don't understand the decomposition"). The chat→root edge
  is the same edge: a root task created during a chat turn inherits a compressed
  snapshot of the chat that motivated it.
- **Up the tree (child → parent): only the `result` returns.** A child's internal
  turns (tool calls,探索, dead ends) **never** enter the parent's window; the
  parent's synthesis turn sees its own scope + the children's `result` fields
  (isolation school — information hiding upward). This is already the shape of
  `build_task_prompt`'s "Subtask results" section; ADR-0009 makes it *exclusive*
  (the parent literally cannot see child internals, because they are in a different
  scope).
- **Across siblings: no direct context sharing.** Siblings coordinate only through
  the parent (its decision trace flows to each; their results flow back). Serial
  execution means there is never a concurrent conflict to begin with; this rule
  keeps even the *sequential* sibling from inheriting the previous sibling's noise.

### 2. Substrate: one event log, scoped views (`Event.task_id`)

`Event` gains an optional structural field `task_id: str | None` (mirrors the
existing `trigger_id` slot — not a payload key, so the store/replay/`_record` treat
it uniformly). The single append point `AgentRuntime._record` **stamps it
automatically** from `self.current_task_id`: conversation events
(`USER_MESSAGE`/`ASSISTANT_MESSAGE`/`TOOL_RESULT`/`TOOL_ERROR`) produced during a
task-scoped run carry that task's id; those produced during chat/cron carry `None`
(the **chat scope**). `Message` (`state.py`) mirrors `task_id`; `reduce` copies it.
No call sites change — the runtime already owns `current_task_id` (ADR-0007 M2).

The **LLM window is the current scope**: `_build_llm_messages` filters
`state.messages` to `m.task_id == self.current_task_id` before its existing budget
walk. A task run sees its own scope; a chat turn sees the chat scope. Cross-talk is
gone by construction. Everything downstream (budget, `keep_recent_messages_min`,
tool-result boundary safety, turn timestamps) runs unchanged over the scoped slice.

### 3. Down-tree framing carries a compressed decision trace

`build_task_prompt` is extended: in addition to `goal` + parent-chain goals +
`progress_summary` + child results, a child's first run receives a **compressed
parent decision trace** — the key decisions/constraints the parent established that
the child must stay coherent with. This is produced by the **compaction LLM**
(reuse `memory/compactor.py`) over the parent's scope at the moment of
decomposition, *not* the raw parent turns (which would reintroduce cost and noise).
For the chat→root edge, the trace is compressed from the chat-scope turns that
preceded the task's creation.

Cost control (Manus "reversible first"): the trace is generated **once** at
decomposition and stored on the child (e.g. a `framing` field / a
`TASK_CHECKPOINTED`-like seed), not recomputed each beat. If a child needs more
than the trace gives, the **full parent scope is still in the event log** and
reachable via `recall` — the log is eonlet's "filesystem offload."

### 4. The checkpoint brief becomes an LLM compression (decision continuity)

Replace the structural `_checkpoint_summary` with a compaction-LLM call that
compresses the task's own scope into **"key details, events, and decisions"**
(Cognition's exact target), written as the `progress_summary` via
`TASK_CHECKPOINTED`. This is the resume brief that lets a preempted/suspended task
pick up coherently — and the same artifact serves as the basis for the down-tree
trace (§3) and the up-tree result (the agent's explicit `task(done, result=…)`).
Falls back to the structural grab if the compaction provider is unavailable (never
block a checkpoint).

### 5. Episodic memory is the **chat** scope; a task's residue is result + brief + log

Episodic memory (STM/LTM, the `<memory>` preamble — ADR-0006) is the *conversation
timeline* the agent lives with the user. A task run's internal turns are **ephemeral
working context for that task**, not shared episodic history. Therefore:

- **Tier-1/2/3 operate on the chat scope only** (the working-window estimate and
  tier-1 source exclude `task_id != None`). Task turns are never promoted to
  STM/LTM, so machine task-chatter never swamps the user-conversation memory.
- **A task's durable residue** = its `result` (surfaces into chat on completion,
  and *that* enters episodic memory naturally) + its `progress_summary` brief + the
  full, recall-indexed event log. Nothing is lost; it is simply not injected into
  the shared episodic stores.
- **Knowledge axis is unchanged and global**: the curated `knowledge/` index is
  injected into *every* scope (durable facts cross scope boundaries by design).

### 6. Reversible-before-irreversible compaction (Manus), bounding a long task's scope

Order of operations for managing any scope's growth:

1. **Keep full, recent turns** in the window (they guide the next decision).
2. **Offload, don't summarize, when possible**: full tool results already live in
   the event log (and on the filesystem for `bash`/`file_*`); the window can carry
   them and the budget walk drops the oldest — they remain retrievable via `recall`.
   This is reversible reduction.
3. **Summarize (irreversible) only when reversible reduction plateaus**: when a
   task's own scope exceeds `working_memory_tokens` at a beat boundary, run the
   task-scoped tier-1 (§4) to fold its older turns into the enriched brief and prune
   them from the live window. The chat scope keeps its existing tier-1→2→3 cascade.

## Consequences

### Positive

- **Right context at each tree level.** Children get the parent's decisions (no
  incoherent decomposition — Cognition's failure mode avoided); parents get clean
  child results (no noise — isolation school's benefit); siblings don't cross-
  contaminate. This is the crux the user identified as make-or-break.
- **Decision continuity across suspend/resume/preempt** via an LLM brief that
  actually carries decisions, not the last message.
- **Lower cost per beat**; episodic memory stays about the *user*, not the machine.
- **No Alternative C / no multi-agent fragility.** One log, one fold, one append
  point; serial execution sidesteps the concurrent-conflict trap entirely. The
  event log doubles as the reversible "offload" store (Manus), with `recall` as the
  retrieval path.
- **Fixes ADR-0008 review items #1 (cross-talk) and #2 (re-appended framing).**

### Negative

- **More compaction-LLM calls.** A down-tree trace per decomposition + an LLM brief
  per checkpoint add calls (and latency/cost) the structural version avoided.
  Mitigated: trace is computed once per child, not per beat; brief only on
  suspend/yield; both fall back to structural on provider failure.
- **`fold` still materializes all scopes in memory** (only the *window* is
  filtered). Same footprint as today for large logs; lazy/segmented fold is out of
  scope.
- **Trace quality is now load-bearing.** A child's coherence depends on the
  compression being good. Bounded by `recall` as the escape hatch, but a bad trace
  degrades a subtask more visibly than today's (no-trace) behavior — needs eval.
- **A child cannot "see" sibling work directly** by design; if genuinely
  interdependent subtasks are mis-modeled as siblings, the parent must mediate.
  This is the intended trade (it is *why* the parent exists) but it pushes
  decomposition quality onto the prompt.

### Neutral

- `recall` unchanged — whole log (every scope) stays searchable.
- The `<tasks>` sibling block still lists pending leaves forest-wide (backlog
  visibility); whether to narrow it during a focused run is left open (ADR-0008
  review #4).
- No new `EventKind`; one new structural `Event` field (`task_id`) and an optional
  child-framing seed (reuses `TASK_CHECKPOINTED` or a small `task_framing` payload).
- Per-task token budget (ADR-0007 M4) becomes scope-accurate for free.

## Phasing

- **M1 — Scoping substrate.** `Event.task_id` + store column; `_record` central
  stamping; `Message`/`reduce` mirror; `_build_llm_messages` scoped filter; tier-1
  trigger/source restricted to chat scope. Ships the isolation (fixes cross-talk).
  Down-tree framing still goal-only; checkpoint still structural. Lowest risk,
  immediately removes the pollution.
- **M2 — LLM checkpoint brief (§4).** Replace structural `_checkpoint_summary` with
  a compactor call; resume/preempt now carry decisions. Closes ADR-0007 M2's
  deferred enriched brief.
- **M3 — Down-tree decision trace (§3).** Compress the parent (or chat) scope into
  the child's framing at decomposition; store once on the child. The Cognition
  Principle-1 fix.
- **M4 — Task-scoped reversible→irreversible compaction (§6) + docs.** Task-scope
  tier-1 into the brief on overflow; TASK_SPEC §4.1 + MEMORY_SPEC + templates +
  CHANGELOG.

## Alternatives Considered

### A. Status quo — one global window for everything
Rejected; it is the problem (cross-talk, cost, bloat, structural-only brief).

### B. Full isolation school — child gets only its goal; only summary returns; no parent trace
Rejected as the *down* default. It is the Claude-Code/Anthropic/Manus model and is
right *upward* (we adopt it for child→parent), but Cognition shows that applying it
*downward* makes subtasks fail for lack of the decomposition rationale. We share the
(compressed) trace downward. (Pure isolation also targets *parallel* gains eonlet
doesn't pursue — it is serial.)

### C. Full shared-trace school — child inherits the parent's entire raw trace
Rejected as too costly/noisy (reintroduces the global-window problem one level down,
and Anthropic's data shows verbose traces balloon tokens). We share a **compressed**
trace and keep the raw parent scope in the recall-indexed log as the escape hatch.

### D. Alternative C (ADR-0007) — a separate conversation/provider session per task
Rejected again: second mutable store, suspend/resume becomes fresh compaction per
task, cost explodes with forest size. Scoped views over one log give the isolation
benefit without a second store (Invariant #1).

### E. Promote task turns into episodic STM/LTM like chat
Rejected (§5). Machine task-chatter would swamp user-conversation memory and distort
the `<memory>` preamble. A task's residue belongs in result + brief + the searchable
log.

### F. Structural (non-LLM) checkpoint and goal-only framing (today's behavior)
Rejected for the steady state (kept only as the fallback path). Cognition's core
recommendation is precisely a *dedicated compression LLM carrying decisions*; the
structural grab loses decision continuity, the thing that makes resume and
decomposition coherent.

## Resolved Decisions

1. **Asymmetric context flow**: compressed decision trace flows *down*; only
   `result` flows *up*; siblings share nothing directly (coordinate via parent).
2. **One event log, scoped views** (`Event.task_id`, stamped centrally by
   `_record`); the LLM window is the current scope. No per-task store (no Alt C).
3. **Episodic compaction = chat scope only**; task residue = result + LLM brief +
   recall-indexed log; knowledge stays global.
4. **Checkpoint brief and down-tree trace are LLM-compressed** ("key details,
   events, decisions"), reusing the memory compactor, with a structural fallback.
5. **Reversible-before-irreversible**: keep recent full turns, lean on the
   log+recall as the offload, summarize into the brief only on overflow.
6. **Serial single-worker is the enabling premise** — it removes the concurrent-
   conflict failure mode, letting eonlet combine both schools.

Open (deferred): lazy/segmented `fold` for very large logs; narrowing `<tasks>`
during a focused run (ADR-0008 #4); whether the down-tree trace should be refreshed
mid-task if the parent's plan changes after children spawn (likely no — children
re-read via recall, or the parent re-decomposes).

## References

- [ADR-0007](0007-task-scheduling.md) §4 — the per-task on-demand context this
  completes; Alternative C (rejected) it must not reopen; M2's deferred enriched
  brief that M2/M4 here close
- [ADR-0008](0008-user-input-preemption.md) — review items #1 (cross-talk) and #2
  (re-appended framing) this fixes; serial single-worker premise (§1)
- [ADR-0006](0006-compaction-triggers.md) — episodic compaction tiers, now scoped to
  the chat timeline; the LLMCompactor reused for briefs/traces
- Cognition, *Don't Build Multi-Agents* — Principles 1 & 2; single-thread +
  dedicated compression-LLM recommendation (down-tree trace, §1/§3): https://cognition.ai/blog/dont-build-multi-agents
- Anthropic, *How we built our multi-agent research system* — orchestrator-worker,
  subagent isolation, only-summary-returns, token cost (up-tree isolation, §1): https://www.anthropic.com/engineering/multi-agent-research-system
- *Context Engineering in Manus* — reversible compaction (full/compact), filesystem
  offload, summarize-when-plateaued (§6): https://rlancemartin.github.io/2025/10/15/manus/
- Claude Code subagents — per-subagent context, only final message to parent (§1
  up-tree): https://code.claude.com/docs/en/sub-agents
- `runtime/agent.py` — `_record` (central stamping), `_build_llm_messages` (scoped
  window), `_build_system_prompt`, `handle_user_message`
- `runtime/state.py` — `Message`/`reduce` (mirror `task_id`)
- `runtime/events.py`, `runtime/store.py` — new structural field + column
- `tasks/context.py` `build_task_prompt` — framing + down-tree trace (§3)
- `worker/main.py` — `_run_task`, `_checkpoint_summary` (→ LLM, §4), `_run_cascade`
  (chat-scope tier-1, §5)
- `memory/compactor.py` — reused for briefs (§4) and down-tree traces (§3)

## Update history

- 2026-06-02: Initial proposal scoped narrowly as "task-scoped windows over one
  log." Revised the same day after researching hierarchical-agent context practice
  (Cognition, Anthropic, Manus, Claude Code): broadened to the **asymmetric
  context-flow model** (compressed trace down / result up / siblings isolated),
  added the LLM checkpoint brief and down-tree decision trace (Cognition's
  compression-LLM), the chat↔task edge, and reversible-before-irreversible
  compaction (Manus). Status → Proposed, pending acceptance.
- 2026-06-02: Accepted; **M1 (scoping substrate) implemented**. `Event.task_id`
  structural field + additive store migration + index; `_record` central stamping
  (the conversation family); `Message`/`reduce` mirror; `_build_llm_messages` scopes
  the window (watermark applies to chat scope only); `chat_scope_only` filter
  applied to tier-1 source (`tier1.run_tier1`), the tier-1 trigger
  (`_maybe_run_tier1`), and the propose-compact guard. New tests: scope roundtrip +
  legacy-DB migration (`test_event_store`), `_record` stamping + scoped window +
  episodic exclusion (`test_agent_injection`). 616 tests; ruff + mypy clean. M2–M4
  (LLM brief, down-tree trace, task-scoped tier-1) remain.
- 2026-06-02: **M2 (LLM checkpoint brief) implemented.** New `tasks/brief.py`
  (`build_task_brief` + brief-specific prompt, plain-text output); `_checkpoint_summary`
  is now async — compresses the task's own-scope events via the compaction LLM, with
  a scope-aware structural fallback (`_structural_checkpoint_summary`) when memory is
  off / no provider / the call fails. Tests: `tests/unit/tasks/test_brief.py` (prompt
  assembly, provider call, scope-aware fallback); LLM path covered end-to-end by the
  yield/preempt worker integration tests. 619 tests; ruff + mypy clean. M3 (down-tree
  decision trace) and M4 (task-scoped tier-1) remain.
- 2026-06-02: **M3 (down-tree decision trace) implemented.** `Task.framing` field
  (reduced from `TASK_UPDATED(framing=…)` — no new EventKind); `tasks/brief.py`
  gains `build_decision_trace` + `TRACE_SYSTEM_PROMPT`; `build_task_prompt` injects
  a "Context from above" section; `worker.main._ensure_framing` computes the trace
  once on a task's first dispatch (parent scope for a subtask, chat scope for a
  user-origin root) and stores it — trigger-origin roots / trivial sources / no
  provider / failures stay goal-only. Tests: trace prompt + provider call
  (`test_brief`), framing reducer (`test_forest`), prompt injection (`test_context`),
  `_ensure_framing` store + trigger-root skip (`test_agent_injection`). 626 tests;
  ruff + mypy clean. **M4 (task-scoped reversible→irreversible tier-1) remains.**
- 2026-06-02: **M4 (task-scope compaction) implemented — ADR-0009 complete (M1–M4).**
  Per-task `Task.brief_watermark` (advanced by `TASK_CHECKPOINTED.boundary_event_id`,
  monotonic); cumulative brief (`build_task_brief(prior_brief=…)`); turn-boundary
  hook `AgentRuntime.on_turn_boundary` → `worker._maybe_compact_task` folds older
  own-scope turns (keeping a ~30% tail via `compute_suggested_boundary`) into the
  brief and advances the watermark; `_build_llm_messages` prunes own-scope turns
  ≤ watermark; `_build_system_prompt` injects the live brief as `<task_progress>`
  (rebuilt each turn ⇒ continuity without message re-injection). The
  suspend/yield/preempt checkpoint shares the cumulative-brief path and passes
  `boundary`; the structural fallback returns `boundary=None` (never prunes raw
  turns on a lossy summary). Tests: cumulative prompt + watermark reducer
  (`test_brief`/`test_forest`), fold+prune+system-prompt end-to-end
  (`test_agent_injection`). 628 tests; ruff + mypy clean.
