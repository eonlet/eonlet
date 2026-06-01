# ADR-0007: Task Scheduling — Hierarchical, Priority-Ordered, Cooperatively-Preemptive Work for a Single Agent

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-05-30 |
| Accepted | 2026-05-31 |
| Deciders | Ziyu |
| Supersedes | – (extends the task storage of [ADR-0005](0005-dual-axis-memory.md) §M3; realizes the "task-orchestration layer" foreshadowed by [ADR-0006](0006-compaction-triggers.md)) |
| Superseded by | – |

## Context

Today `task` (ADR-0005, `src/eonlet/tasks/`) is a **flat to-do list**: a
`tasks/todos.jsonl` of `pending / done / cancelled` records with `due` and
`tags`. It records *intent* but it does not *run* anything — the agent reads the
list, decides what to do inside one conversation turn, and the task object never
becomes the unit of execution. Separately, `schedule` (ADR-0002) manages cron +
one-shot triggers whose payload is a free-text message injected into the agent
when the trigger fires.

Four user scenarios motivate a richer model:

1. **Trivial** — "what's the weather today?" The agent calls a tool and answers
   inline. No task. *(Already works; stays working.)*
2. **Single complex** — "go do an X investigation for me." The agent should
   create a **task**, run it as a unit of work, and report a summary on
   completion — not try to cram an open-ended investigation into one chat turn.
3. **Recurring** — "summarize and email me every day at 8am." A schedule that,
   on each fire, runs a fresh instance of that work.
4. **Project decomposition** — "build me this project." The agent splits the
   goal into tasks, and a task may split further into sub-tasks: a **tree**, and
   across independent goals a **forest**, traversed depth-first.

### The mental model: an agent is a single human-like worker

The manifesto frames an eonlet as a person-like individual. Two consequences
fall out and constrain the whole design:

- **No parallelism.** A person does one thing at a time. The agent runs **at
  most one task at any instant**. (The worker's `_main_loop` already drains a
  single-consumer queue one `TriggerItem` at a time — serial execution is
  *already* the substrate; `worker/main.py:232`.)
- **Priority with preemption by interruption.** While task A is unfinished, the
  user (or the agent's own judgment) may introduce a higher-priority task B. A
  person stops, jots down where they were, switches to B, and later resumes A
  from their notes. We want exactly that: **suspend → checkpoint → switch →
  resume**.

### Two framings to get right before deciding

**(a) `task` and `schedule` are orthogonal axes — bridge, don't merge.**
`schedule` answers *when does the agent wake* (time/event); `task` answers *what
work is outstanding*. Scenario 3 is not "a task that ticks" — it is "a trigger
that hatches a task." Collapsing the two into one implementation would conflate
a stimulus with a unit of work. The right relationship makes **task the primary
noun and a schedule an optional attribute of a task**, with the existing trigger
engine kept underneath as the substrate.

**(b) "Preemption" in an LLM is cooperative, not true.** You cannot interrupt a
model mid-token. The only real switch points are **turn boundaries / between
tool calls**. So "suspend A for B" means: at a turn boundary the scheduler
notices a higher-priority runnable task, compacts A's working context into a
**resume brief**, and switches. The design commits to cooperative checkpoint
switching and never pretends otherwise.

### Relationship to existing invariants

- **No supervisor (ADR-0001).** The task scheduler lives **inside the single
  worker process**, alongside the cron scheduler and `_main_loop`. It is not a
  new daemon and does not manage other processes. ADR-0001 holds.
- **Events are append-only (Invariant #1).** Tasks become an **event-sourced
  projection**, like `AgentState` — not a second mutable source of truth.
- **Don't reintroduce retired tools (Invariant #7).** This evolves the existing
  `task` tool; it does not bring back `todo`/`note`/`remember`/`forget`.

## Decision

Promote a task from a *to-do record* to a **durable, hierarchical, resumable
unit of work** ("job"), and add a **TaskScheduler** that runs at most one task
at a time by priority with cooperative preemption. Keep `schedule`/triggers as
the separate "when to wake" layer and **bridge** them: a task may carry a
schedule that, on fire, hatches a fresh task instance.

This is the agent's analogue of an **OS process scheduler** — directly on-brand
for "the systemd for agents."

### 1. Task data model (record → job)

A task gains the fields needed for a forest, priority, and resume:

| Field | Purpose |
|---|---|
| `id` | unchanged (`mint_task_id`) |
| `parent_id` + child order | tree / forest structure; DFS traversal |
| `goal` | the durable objective, used to rebuild context on resume |
| `content` | human-readable description (existing) |
| `priority` | integer; scheduler ordering. Set by the user, else the agent's judgment |
| `lifecycle` | `pending → active → suspended / blocked → done / cancelled` (replaces the flat `status`) |
| `progress_summary` | **the resume brief** — written on suspend by the compactor; the handle that lets the agent pick the task back up |
| `result` | completion summary, surfaced to the user (scenario 2) |
| `origin` | `user` / `agent` / `trigger` — audit + scenario routing |
| `schedule` | optional cron/at spec; when present the task is a **template** (see §4) |
| `due`, `tags` | unchanged |

**Internal nodes vs leaves.** A leaf does concrete work. A parent is an
*orchestration node*: it goes `active` while a child runs, and when all children
are `done` it gets one synthesis turn and then completes. Only leaves are
directly executed; internal nodes synthesize.

### 2. Storage: event-sourced projection (resolved fork #1)

The task forest is **rebuilt by replaying task events**, exactly as `AgentState`
is. The event log (per-agent SQLite) is the single source of truth. This
**supersedes ADR-0005's choice** of `tasks/todos.jsonl` as the store; the JSONL
becomes at most a derived, rebuildable snapshot cache (`tasks/forest.json`) for
fast cold reads — never authoritative.

Task event family (replaces the v0.0.8 `task_added/updated/deleted` trio with a
lifecycle-aware set):

- `TASK_CREATED` — `{id, parent_id?, goal, content, priority, origin, schedule?}`
- `TASK_UPDATED` — mutable fields `{content?, goal?, priority?, due?, tags?}`
- `TASK_TRANSITIONED` — `{id, from_state, to_state, reason}` (one event kind
  covers activate / suspend / resume / block / complete / cancel uniformly)
- `TASK_CHECKPOINTED` — `{id, progress_summary}` (written on suspend)
- `TASK_DELETED` — `{id}`

Net `EventKind`: **39 → 41** (the three old task variants are redefined, not
added to; `TASK_TRANSITIONED` and `TASK_CHECKPOINTED` are the two genuinely new
kinds). Pre-alpha needs no migration (ADR-0005 §M4 precedent).

### 3. The TaskScheduler (new core), distinct from the cron scheduler

Two schedulers, two jobs:

- **Cron scheduler** (existing, `triggers/scheduler.py`) — decides **when to
  wake**.
- **TaskScheduler** (new) — once awake, decides **which task this beat runs**.

Selection: among the forest roots, pick the highest-priority tree; within it,
DFS to the leftmost **runnable leaf** (`pending`, not `blocked`, dependencies
satisfied) and drive a task-scoped agent run. The root→active-leaf path is the
**current spine**; exactly one spine is active at a time (strictly serial).

### 4. Per-task context and resume (resolved decision; reuse, don't rebuild)

Each task run gets a working context **assembled on demand** — *not* a separate
long-lived LLM conversation per task (too costly; a compaction nightmare):

- **On run/resume**, seed the working context from: `goal` + the parent-chain
  summaries + the task's own `progress_summary` + relevant `knowledge`/`recall`.
- **On suspend**, run a tier-1-style compaction (reuse `memory/compactor.py`) to
  fold the working window into `progress_summary`, then emit
  `TASK_CHECKPOINTED`. "Switch task" therefore reduces to "read that task's
  resume brief" — exactly the human-with-notes model.

### 5. schedule ↔ task bridge; recurring = template hatches instances (fork #2 + #4)

- The low-level `schedule` tool is **kept** as the trigger primitive (fork #4) —
  pure trigger operations remain useful and unchanged.
- The `task` tool gains an optional `schedule` attribute. Under the hood it
  registers a trigger bound to a **task template**.
- A recurring task is a **template that hatches a fresh task instance on each
  fire** (fork #2) — not one task that resets. Each day's run is its own task
  with its own `result` and history, keeping the forest clean and auditable.

### 6. Cooperative preemption + consent (resolved fork #3)

At each turn boundary in `_main_loop`, the scheduler re-evaluates. If a
**strictly higher-priority** runnable task exists outside the current spine, it
proposes a switch:

- **Interactive (`ask`)** — prompt the user via the existing **`DecisionBroker`**
  (ADR-0006's blocking worker↔CLI consent channel): *"Suspend A to start B?"*
- **`yolo`** — auto-approve **with an audit trail** (`TASK_TRANSITIONED`
  `reason:"preempt:yolo"`), mirroring ADR-0006's `yolo` proposal handling.

On approval: `TASK_CHECKPOINTED(A)` → `TASK_TRANSITIONED(A: active→suspended)` →
`TASK_TRANSITIONED(B: pending→active)`. No new consent transport is built — the
`DecisionBroker` is reused as-is.

### 7. When to spawn a task is *prompting*, not code

The scenario-1-vs-2 distinction ("answer inline" vs "create a task") is ~90% a
**system-prompt + tool-description** matter. The model judges the threshold; the
architecture only guarantees that *once a task exists it gets scheduled and
run*. No code-level "trivial vs complex" classifier.

### 8. `agent.yaml` schema additions

The existing top-level `tasks` block (`inject_pending`,
`archive_done_after_days`) gains a scheduling sub-block:

```yaml
tasks:
  inject_pending: true            # existing
  archive_done_after_days: 30     # existing
  scheduling:
    enabled: true                 # turn the TaskScheduler on
    preempt: ask                  # off | ask | auto_by_priority
    max_tree_depth: 5             # guard: runaway self-decomposition
    max_fanout: 12                # guard: children per node
    max_suspended: 8              # guard: backlog of half-done work
    per_task_budget_tokens: 0     # 0 = inherit agent budget
    preempt_cooldown: 5m          # anti-thrash (mirrors ADR-0006 cooldown)
```

Scheduled (cron/autonomous) agents may set `preempt: off` or
`scheduling.enabled: false`; interactive agents default `preempt: ask`.

## Consequences

### Positive

- **Open-ended work stops fighting the chat layer.** Investigations and projects
  run as scheduled jobs that report back, resolving exactly the pressure
  ADR-0006 named when it rejected turn-internal compaction.
- **On-brand.** A per-agent process scheduler is the literal realization of "the
  systemd for agents," reusing the serial `_main_loop` substrate already present.
- **Auditable by construction.** Event-sourced tasks + `TASK_TRANSITIONED`/
  `TASK_CHECKPOINTED` make every activation, suspend, preemption, and completion
  visible in `tail`/`replay`.
- **Maximal reuse.** Resume briefs reuse the memory compactor; consent reuses the
  `DecisionBroker`; recurrence reuses the trigger engine. Little net-new
  machinery beyond the scheduler and the data model.

### Negative

- **The largest subsystem since memory.** Touches `tasks/`, `runtime/`,
  `worker/`, `triggers/`, `memory/` (compactor reuse), and the CLI. Must be
  phased (below) or it becomes a death march.
- **Resume fidelity is bounded by the compactor.** A `progress_summary` is lossy;
  a resumed task may have lost nuance. Mitigated by `recall` over the task's
  event range, but not eliminated.
- **New runaway surface.** An agent that self-decomposes can fork-bomb its own
  forest. The `max_tree_depth` / `max_fanout` / `max_suspended` / per-task budget
  guards exist precisely for this and must land with Phase 4, not after.

### Neutral

- `tasks/todos.jsonl` as a source of truth is retired in favor of the event log;
  any on-disk task file becomes a rebuildable cache.
- Single-agent only. Multi-agent delegation (teams) stays a v0.6+ concern; this
  ADR is explicitly *one* worker scheduling *its own* work.

## Phasing

- **M1 — Data model.** Tree + priority + lifecycle, event-sourced projection;
  `task` tool extended (`parent_id`, `priority`, `goal`); `eonlet tasks` tree
  view. **No scheduler yet** — the agent still advances tasks manually via the
  tool. Low-risk, immediately useful, validates the forest shape.
- **M2 — Scheduler.** TaskScheduler selects the next runnable leaf; single-task
  serial execution; suspend/resume via the compactor-produced `progress_summary`.
  The "run the queue" loop.
- **M3 — Preemption + bridge.** Priority preemption with `DecisionBroker`
  consent; `schedule`→task-template bridge; recurring instances.
- **M4 — Guards + observability.** Depth/fanout/suspended caps, per-task budget,
  preempt cooldown; `eonlet tasks suspend/resume/reprioritize`; spec + template
  updates.

## Alternatives Considered

### A. Merge `task` and `schedule` into one primitive
Rejected. They are orthogonal axes (when-to-wake vs what-to-do); merging
conflates a stimulus with a unit of work. We bridge instead — task is the noun,
schedule an attribute, trigger engine the substrate (§5).

### B. Keep `tasks/todos.jsonl` as the source of truth, bolt a tree on top
Rejected. A tree over flat JSONL is already awkward (parent pointers, ordering,
lifecycle history) and would create a second mutable source of truth beside the
event log, violating Invariant #1. Event-sourced projection (§2) is cleaner and
free of migration cost in pre-alpha.

### C. One full LLM conversation per task
Rejected. Cost explodes with forest size and every suspend/resume becomes a
fresh compaction problem. Assemble context on demand from `goal` + parent chain +
`progress_summary` + recall instead (§4).

### D. True preemption / interrupting a run mid-step
Impossible at the token level and undesirable mid-tool-call. Cooperative
checkpoint switching at turn boundaries is the only coherent semantics (§6).

### E. Recurring task = one task that resets its lifecycle
Rejected in favor of template-hatches-instances (§5). Resetting loses per-run
history and `result`; hatching keeps a clean audit of each occurrence.

### F. A separate scheduler daemon
Rejected — violates ADR-0001. The TaskScheduler is an in-worker component beside
the cron scheduler, not a process manager.

## Resolved Decisions

All four design forks were settled during the proposal discussion:

1. **Task source of truth → event-sourced projection** (not a separate
   JSONL/table). Honors Invariant #1, free history/audit; replay cost accepted.
2. **Recurring tasks → template hatches a fresh instance per fire** (not a single
   resetting task). Clean forest, per-run archive.
3. **Preemption → interactive `ask` prompts via `DecisionBroker`; `yolo`
   auto-approves with an audit trail.** Aligns with the existing permission
   modes; no new consent transport.
4. **Keep the low-level `schedule` tool** as the trigger escape hatch; `task`
   layers the scheduling attribute on top of it.

No open questions remain for the M1 data-model milestone. Scheduler guard
defaults (§8 values) are provisional and may be tuned during M4 dogfooding.

## References

- [ADR-0001](0001-no-supervisor-mvp.md) — no supervisor; this scheduler lives inside the one worker
- [ADR-0002](0002-dynamic-triggers.md) — the trigger engine this bridges to
- [ADR-0005](0005-dual-axis-memory.md) §M3 — task storage this supersedes; memory axes the resume brief draws on
- [ADR-0006](0006-compaction-triggers.md) — foreshadows this "task-orchestration layer"; source of the `DecisionBroker` consent pattern and the session-boundary anchor
- `worker/main.py` `_main_loop` — the serial execution substrate the scheduler drives
- `memory/compactor.py` — reused to produce per-task resume briefs
- `worker/decisions.py` `DecisionBroker` — reused for preemption consent
- TOOL_SPEC / AGENT_CONFIG_SPEC / CLI_REFERENCE / TRIGGER_SPEC — to gain the task-scheduling surface (M4)

## Update history

- 2026-05-30: Initial proposal. Four design forks resolved during the discussion
  (event-sourced projection; template-hatches-instances recurrence; `ask`-prompt
  / `yolo`-audit preemption; keep low-level `schedule`). Status → Proposed,
  pending acceptance.
- 2026-05-31: Implemented M1–M4 (see [`docs/plans/task-scheduling.md`](../plans/task-scheduling.md)
  and [`docs/TASK_SPEC.md`](../TASK_SPEC.md)). Two design refinements during
  build: (a) preemption re-queues the paused task as **pending** (not suspended)
  so it resumes naturally once the preemptor outranks-then-finishes; (b) the M2
  checkpoint resume brief is **structural** (LLM enrichment via the compactor
  deferred). A prompt task-pickup poke (`task_wake`) was added so out-of-band
  task creation isn't delayed by the idle poll. Status → Accepted. Shipped in
  v0.0.10 (the ROADMAP v0.2 task-orchestration tier).
