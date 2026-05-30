# Plan — Task Scheduling (Hierarchical, Priority-Ordered, Cooperatively-Preemptive)

Companion implementation plan for [ADR-0007](../adr/0007-task-scheduling.md).
Target version: **v0.2.0** (new ROADMAP entry — the task-orchestration layer
foreshadowed by ADR-0006).

## Why this plan exists

ADR-0007 promotes a task from a flat to-do record into a **durable, hierarchical,
resumable unit of work**, and adds an in-worker **TaskScheduler** that runs one
task at a time by priority with cooperative preemption. This is the largest
subsystem since memory; it touches `tasks/`, `runtime/`, `worker/`, `triggers/`,
`memory/` (compactor reuse), and the CLI. Three pieces carry real risk:

1. **A storage-model flip** — the task source of truth moves from
   `tasks/todos.jsonl` to the **event log** (tasks become a `fold`, like
   `AgentState`). Today the `task` tool *double-writes* (JSONL truth + a
   `task_added` audit event); the flip collapses that to one.
2. **A new execution loop** — the TaskScheduler drives task-scoped agent runs and
   must checkpoint/resume context, which has never happened before.
3. **Cooperative preemption** — pausing a task at a turn boundary to switch to a
   higher-priority one, with user consent.

Sequencing lands the storage flip first (additive, no scheduler), so the risky
execution and preemption infra build on a stable, well-tested projection.

## Guiding principles

1. **Storage flip before scheduler; scheduler before preemption.** M1 makes
   tasks an event-sourced projection with no behavior change to *how* tasks run
   (the agent still advances them via the tool). M2 adds the scheduler. M3 adds
   preemption. Each milestone is independently shippable and green.
2. **Tasks are a `fold`, exactly like `AgentState`.** The event log is the single
   source of truth (Invariant #1). A `TaskForest` reducer mirrors
   `runtime/state.py:reduce`. **No `forest.json` snapshot in M1** (resolved
   sub-decision): the log is short; pure replay is sufficient. A snapshot cache is
   a later optimization, gated behind its own consistency tests, only if cold-read
   replay measurably hurts.
3. **Clean event-family redefinition, no back-compat** (resolved sub-decision).
   The v0.0.8 `task_added/updated/deleted` trio is *replaced* by a lifecycle-aware
   family (`TASK_CREATED / UPDATED / TRANSITIONED / CHECKPOINTED / DELETED`).
   Pre-alpha needs no migration (ADR-0005 §M4 precedent).
4. **Reuse, don't rebuild.** Resume briefs reuse `memory/compactor.py`; preemption
   consent reuses `worker/decisions.py` `DecisionBroker`; recurrence reuses the
   `triggers/` engine. Net-new code is the data model + the scheduler.
5. **Bridge `task` and `schedule`, never merge.** The low-level `schedule` tool
   stays a trigger primitive; `task` gains a `schedule` attribute that registers a
   trigger bound to a **task template** which hatches a fresh instance per fire.
6. **No supervisor (ADR-0001).** The TaskScheduler is an in-worker component beside
   the cron scheduler and `_main_loop`; not a daemon, not a process manager.
7. **Docs land with the code.** A normative `TASK_SPEC.md` (or a TRIGGER/AGENT_CONFIG
   addition) ships in the milestone that introduces the behavior it describes — M4,
   not trailing.

## Milestone map

```
M1  Event-sourced task forest: model + reducer + tool rewrite + tree view   (≈ 2 day)
M2  TaskScheduler: runnable-leaf selection + task-scoped run + suspend/resume (≈ 2.5 day)
M3  Preemption (DecisionBroker consent) + schedule→task-template bridge       (≈ 2 day)
M4  Guards + CLI ops + TASK_SPEC + config/tool docs + templates + version     (≈ 1.5 day)
```

Four milestones, four PRs. M1 is the foundation everything else folds onto.

### Suggested PR shape

```
PR1 (M1) — feat(tasks): event-sourced hierarchical task forest
  - events.py: replace task_added/updated/deleted with the lifecycle family
  - tasks/forest.py: Task node (parent_id, priority, lifecycle, goal,
    progress_summary, result, origin) + fold_tasks() reducer (mirrors state.fold)
  - tasks/store.py: read path becomes "fold task events"; JSONL truth retired
  - runtime/agent.py: own a TaskForest projection; reduce task events on append
  - tools/protocol.py: ToolContext gains a read accessor for the forest
  - tools/builtin/task.py: mutate via record_event only (no JSONL double-write);
    add parent_id / priority / goal; list reads the projection
  - memory/injection.py: build_tasks_block folds the projection (pending leaves)
  - cli: `eonlet tasks` renders the forest as a tree
  - Tests: fold_tasks reducer, tool→event→projection round-trip, tree render

PR2 (M2) — feat(tasks): TaskScheduler + task-scoped runs + checkpoint/resume
PR3 (M3) — feat(tasks): cooperative preemption + recurring task templates
PR4 (M4) — docs(tasks): TASK_SPEC + guards + CLI ops + templates + v0.2.0
```

## M1 — Event-sourced task forest (≈ 2 day)

### Scope

The storage flip and the richer model. **No scheduler** — the agent still
advances tasks by calling the tool, exactly as today; only the *shape* and the
*source of truth* change.

- `runtime/events.py` — **replace** `TASK_ADDED/UPDATED/DELETED` with:
  - `TASK_CREATED` — `{id, parent_id?, goal, content, priority, origin, schedule?}`
  - `TASK_UPDATED` — `{id, content?, goal?, priority?, due?, tags?}`
  - `TASK_TRANSITIONED` — `{id, from_state, to_state, reason}`
  - `TASK_CHECKPOINTED` — `{id, progress_summary}`
  - `TASK_DELETED` — `{id}`
  - Update constructor helpers; `EventKind` count **39 → 41** (two genuinely new
    kinds; the old three are redefined). Keep the enum docstring/count comment in
    sync (CLAUDE.md tracks it).
- `tasks/forest.py` (new) — `Task` dataclass (the ADR §1 fields) + a `TaskForest`
  holding nodes by id with parent/child links, and a `fold_tasks(events) ->
  TaskForest` reducer that mirrors `runtime/state.py:fold`/`reduce`. Lifecycle is
  validated on `TASK_TRANSITIONED` (illegal transitions are dropped + logged, not
  crashed — defensive like `Task.from_json` today). DFS leaf iteration lives here.
- `tasks/store.py` — the read path becomes "fold task events from the store". The
  `tasks/todos.jsonl` **source of truth is retired**; no snapshot is written in
  M1. (The file/`todos_path` helper may linger unused for one milestone, removed
  in M4 cleanup, or deleted now — implementer's call.)
- `runtime/agent.py` — the runtime owns a `TaskForest` projection beside
  `AgentState`. It folds task events into it as they are appended (the existing
  `self.store.append` / reduce seam at `agent.py:487`/`105`), so the projection is
  always current without re-reading the whole log per tool call.
- `tools/protocol.py` — `ToolContext` gains a **read accessor** for the forest
  (e.g. `read_tasks: Callable[[], TaskForest] | None`), set by the runtime;
  `None` in standalone tests (the tool then folds from a provided store, mirroring
  the `tasks_dir` fallback pattern already in `task.py`).
- `tools/builtin/task.py` — rewrite to **mutate via `record_event` only** (drop
  the `TaskStore.add/...` JSONL double-write). New/extended args: `parent_id`,
  `priority`, `goal`. `list` reads the projection (tree-aware: indent children).
  Actions map onto events: `add → TASK_CREATED`; `update → TASK_UPDATED`;
  `done/cancel → TASK_TRANSITIONED`; `delete → TASK_DELETED`. (Lifecycle states
  beyond pending/done/cancelled exist in the model but are only *driven* by the
  scheduler in M2; the tool still exposes the manual subset.)
- `memory/injection.py` — `build_tasks_block` folds the projection for pending
  leaves instead of reading JSONL. Output shape unchanged (sibling `<tasks>`
  block, outside `<memory>`).
- `cli/` — `eonlet tasks` renders the forest as an indented tree (status icon +
  id + priority + goal), via rich. Read-only in M1.

### Done when

- A `task add` emits exactly one `TASK_CREATED` event and **no** JSONL write; the
  forest projection and `<tasks>` injection both reflect it by folding events.
- `fold_tasks` reconstructs an identical forest from the event log alone after a
  simulated worker restart (replay test).
- `eonlet tasks` shows a parent with nested children at correct depth.
- Illegal lifecycle transitions are dropped (logged) rather than crashing replay.
- `tests/unit/tasks/` updated: `test_store.py` retargeted to the fold path,
  `test_tools_task.py` asserts event-only mutation + projection reads, new
  `test_forest.py` covers the reducer + DFS iteration + replay.
- `mypy src` + `ruff check .` clean. Coverage ≥ 70. The `task_added/updated/
  deleted` symbols are fully removed (no dead constructors).

---

## M2 — TaskScheduler + task-scoped runs + checkpoint/resume (≈ 2.5 day)

### Scope

The execution core. Introduces "run the queue."

- `tasks/scheduler.py` (new) — `TaskScheduler`: given the `TaskForest`, select the
  highest-priority root tree, DFS to the leftmost **runnable leaf** (`pending`,
  not `blocked`, deps satisfied), and return it. Internal nodes orchestrate: a
  parent goes `active` while a child runs and, when all children are `done`, gets
  one synthesis turn then completes (`TASK_TRANSITIONED → done`). The active
  root→leaf path is the **spine**; exactly one spine active at a time.
- `worker/main.py` `_main_loop` — when the trigger queue is idle and
  `tasks.scheduling.enabled`, ask the scheduler for the next runnable task and
  drive a **task-scoped** `AgentRuntime` run. Strict serial execution is the
  existing single-consumer property; the scheduler just chooses *what* fills a
  beat. Emit `TASK_TRANSITIONED(pending→active)` before, and `→done`/`→blocked`
  after.
- **Per-task context assembly** (`tasks/context.py` or in the scheduler) — seed
  the run's working context from `goal` + parent-chain summaries + the task's
  `progress_summary` + relevant `knowledge`/`recall`. Not a separate long-lived
  conversation (ADR §4).
- **Checkpoint/resume** — on a clean stop (task not finished but yielding), run a
  tier-1-style compaction via `memory/compactor.py` to produce `progress_summary`,
  emit `TASK_CHECKPOINTED`, then `TASK_TRANSITIONED(active→suspended)`. Resume
  rebuilds context from that brief. (In M2, suspend is *voluntary* — e.g. a task
  blocks on a sub-task; involuntary preemption is M3.)
- `tasks/config.py` — add the `scheduling` sub-block (ADR §8) as **mostly inert**
  in M2: honor `enabled`; the guards (`max_*`, `preempt`, cooldown) are validated
  but enforced in M3/M4.

### Done when

- With `scheduling.enabled`, creating a leaf task causes the worker to run it to
  completion and emit a `TASK_TRANSITIONED → done` with a `result`, no manual tool
  step.
- A parent task with two child leaves runs both depth-first, then the parent
  synthesizes and completes.
- A task that voluntarily yields gets a `TASK_CHECKPOINTED` with a non-empty
  `progress_summary`; resuming continues from it (asserted via the injected
  context containing the brief, not the raw prior turns).
- Disabling `scheduling.enabled` restores M1 behavior (manual tool advance only).
- New `tests/unit/tasks/test_scheduler.py` (selection + DFS + parent synthesis)
  and a worker integration test for one full run. `mypy`/`ruff`/coverage green.

---

## M3 — Cooperative preemption + schedule→task-template bridge (≈ 2 day)

### Scope

Priority interruption (with consent) and recurrence.

- **Preemption** — at each turn boundary in the scheduler/`_main_loop`,
  re-evaluate the forest. If a **strictly higher-priority** runnable task exists
  outside the current spine and `tasks.scheduling.preempt != off`:
  - `preempt: ask` → block on `worker/decisions.py` `DecisionBroker`
    (`kind:"task_preempt"`, prompt *"Suspend A to start B?"*). Reuse the channel
    as-is; no new transport.
  - `preempt: auto_by_priority` (and `yolo`) → auto-approve with an audit
    `TASK_TRANSITIONED(reason:"preempt:auto")`.
  - On approval: `TASK_CHECKPOINTED(A)` → `TASK_TRANSITIONED(A active→suspended)`
    → `TASK_TRANSITIONED(B pending→active)`.
  - `preempt_cooldown` guard prevents thrash (mirror ADR-0006's cooldown shape).
- **schedule→task bridge** (`tools/builtin/task.py` + `triggers/`) — `task` gains
  a `schedule` attribute (cron / at). Setting it registers a dynamic trigger
  (reuse `triggers/dynamic_store.py` + the `schedule` substrate) bound to a
  **task template**. On fire, the trigger **hatches a fresh task instance**
  (`TASK_CREATED`, `origin:trigger`) rather than re-running one task — each
  occurrence is its own task with its own `result`/history.
- The low-level `schedule` tool is **unchanged** (kept as the trigger escape
  hatch, ADR fork #4).

### Done when

- With two pending tasks and `preempt: ask`, raising the second's priority above
  the running first triggers a `DecisionBroker` prompt; approving produces the
  checkpoint→suspend→activate event sequence; declining leaves the first running.
- `yolo` / `auto_by_priority` switches without prompting and leaves a
  `reason:"preempt:auto"` audit trail.
- A `task` with a daily `schedule` hatches a new `TASK_CREATED(origin:trigger)`
  on each fire; prior instances retain their `result`.
- `preempt_cooldown` blocks a second switch inside the window.
- New `test_preemption.py` + `test_task_templates.py`; reuse the existing
  `test_decisions.py` harness. Green gates.

---

## M4 — Guards, CLI ops, normative spec, templates, version (≈ 1.5 day)

### Scope

- **Guards enforced** — `max_tree_depth`, `max_fanout`, `max_suspended`,
  `per_task_budget_tokens`. `TASK_CREATED` that would breach depth/fanout is
  rejected at the tool with a clear error; budget caps a task-scoped run.
- **CLI ops** — `eonlet tasks suspend <id>` / `resume <id>` / `reprioritize <id>
  <n>` / `cancel <id>`, each emitting the right event through the worker.
- **`docs/TASK_SPEC.md`** (new, normative) — task lifecycle state machine, forest
  semantics, scheduler selection rule, preemption + consent matrix, the
  schedule-bridge/template model, guard defaults. Cross-link from `SPEC.md` and
  the ADR/plan index.
- **Doc updates** — `AGENT_CONFIG_SPEC.md` (the `tasks.scheduling` block),
  `TOOL_SPEC.md` (`task` tool new actions/attrs), `CLI_REFERENCE.md` (`eonlet
  tasks` subcommands), `TRIGGER_SPEC.md` (task-template hatching).
- **Templates** — `assistant` enables `scheduling` + `preempt: ask`; scheduled
  agents (`x-digest`, `portfolio`) set `preempt: off` (or `scheduling.enabled:
  false`); system prompts get the "trivial → answer inline; complex → create a
  task" guidance (ADR §7 is prompt-level).
- **Cleanup + version** — remove any lingering `todos.jsonl` helper; `EventKind`
  comment/count synced to 41; CHANGELOG + CLAUDE.md version-history entry;
  `pyproject.toml` → v0.2.0; ROADMAP gains the task-orchestration entry.

### Done when

- A forest exceeding `max_tree_depth`/`max_fanout` is refused at creation with a
  project error; `max_suspended` blocks new suspends; a task-scoped run respects
  its budget.
- `eonlet tasks suspend/resume/reprioritize/cancel` work end-to-end against a
  running worker.
- `TASK_SPEC.md` is normative and cross-linked; the three templates validate and
  carry the new config + prompt guidance.
- CHANGELOG/CLAUDE.md/ROADMAP/pyproject reflect v0.2.0. Full suite + `mypy` +
  `ruff` green; coverage ≥ 70.

---

## Cross-cutting checklist (all milestones)

- `from __future__ import annotations`; anyio (never asyncio); structlog/rich; no
  bare `except Exception`; project errors only.
- No event row is ever mutated. Tasks are a `fold` of append-only events; the
  forest projection and any future snapshot are derived and rebuildable.
- The `TaskForest` reducer is total and defensive: unknown/illegal transitions
  are logged and skipped, never fatal to replay (mirror `Task.from_json` today).
- `EventKind` count + the CLAUDE.md "39/41 variants" references stay in sync.
- Branch coverage stays ≥ 70 (CI gate); the scheduler, reducer, preemption, and
  guards each carry their own tests.

## Risks & mitigations

- **Storage flip breaks the existing task surface.** Mitigate: M1 is self-
  contained and ships before any scheduler; the tool's external behavior
  (add/list/done/cancel/delete) is preserved while only the source of truth
  moves; `test_tools_task.py` is retargeted, not deleted.
- **Resume fidelity is bounded by the compactor.** A `progress_summary` is lossy.
  Mitigate: `recall` over the task's event range is available as a fallback; pre-
  alpha tolerance accepted.
- **Self-decomposition fork-bomb.** An agent that recursively splits can explode
  the forest. Mitigate: `max_tree_depth`/`max_fanout`/`max_suspended` land in M4;
  until then, dogfood with `scheduling.enabled: false` on autonomous agents.
- **Preemption thrash.** Mitigate: `preempt_cooldown` + strictly-higher-priority
  requirement; consent prompt in `ask` mode is itself a brake.
- **Blocking a task-scoped run for consent in a headless worker.** Mitigate: the
  `DecisionBroker` already auto-declines with no attached session (ADR-0006), so a
  detached worker never hangs — it just doesn't preempt.
- **Replay cost as the log grows.** Mitigate: the projection is folded once and
  updated incrementally on append (not re-folded per tool call); a `forest.json`
  snapshot is the escape hatch if cold-read replay ever bites (deferred, not M1).

## References

- [ADR-0007](../adr/0007-task-scheduling.md) — the decision this plan sequences
- [ADR-0006](../adr/0006-compaction-triggers.md) / [plans/compaction-triggers.md](compaction-triggers.md) — `DecisionBroker` consent pattern + plan format this follows
- [ADR-0005](../adr/0005-dual-axis-memory.md) — task storage (`tasks/todos.jsonl`) this supersedes; memory axes the resume brief draws on
- [ADR-0001](../adr/0001-no-supervisor-mvp.md) — the scheduler is in-worker, not a daemon
- `runtime/state.py` — `fold`/`reduce`, the projection pattern `TaskForest` mirrors
- `runtime/agent.py:487` / `:105` — the append/reduce seam the forest projection hooks
- `tools/builtin/task.py` — the tool rewritten from JSONL double-write to event-only
- `memory/compactor.py` — reused to produce per-task resume briefs
- `worker/decisions.py` — `DecisionBroker`, reused for preemption consent
- `triggers/dynamic_store.py` / `tools/builtin/schedule.py` — the trigger substrate the task-template bridge sits on

## Update history

- 2026-05-30: Initial plan drafted from ADR-0007 (Proposed). Two M1 sub-decisions
  baked in: no `forest.json` snapshot in M1 (pure replay), and a clean event-
  family redefinition with no back-compat to the v0.0.8 trio.
