# TASK_SPEC — Task Scheduling (normative)

Status: **Accepted** (ADR-0007, v0.0.10; the ROADMAP "task-orchestration"
feature tier, a.k.a. v0.2). This spec is authoritative for the task subsystem;
the code follows it.

Tasks are **workflow state** — the work an eonlet will *do* — as distinct from
memory (what it *knows*). An eonlet is a single human-like worker: it runs **at
most one task at a time**, chosen by priority, and may pause a task to attend to
a more urgent one. This document specifies the task model, the scheduler, the
lifecycle, preemption, the schedule bridge, and the anti-runaway guards.

See also: [ADR-0007](adr/0007-task-scheduling.md) (decision + rationale),
[AGENT_CONFIG_SPEC §tasks](AGENT_CONFIG_SPEC.md), [TOOL_SPEC](TOOL_SPEC.md) (the
`task` tool), [TRIGGER_SPEC](TRIGGER_SPEC.md) (the schedule substrate).

## 1. The forest is an event-sourced projection

There is **no `tasks/todos.jsonl` store**. The live task forest is a `fold` of
the task event family over the per-agent event log — exactly as `AgentState` is
a fold of the conversation events. The event log is the single source of truth
(Invariant #1). The runtime owns the projection as `AgentRuntime.task_forest`,
reduced on every append.

Task event family (`EventKind`):

| Event | Payload | Meaning |
|---|---|---|
| `TASK_CREATED` | `id, parent_id?, goal, content, priority, origin, due?, tags?, schedule?` | new node |
| `TASK_UPDATED` | `id, {content?/goal?/priority?/due?/tags?}` | edit mutable fields |
| `TASK_TRANSITIONED` | `id, from_state, to_state, reason?, result?` | lifecycle move |
| `TASK_CHECKPOINTED` | `id, progress_summary` | resume brief (written on pause/yield) |
| `TASK_DELETED` | `id` | remove node |

The reducer is **total and defensive**: a duplicate `id`, an event for a missing
node, or an illegal lifecycle transition is logged and skipped — never fatal to
replay.

## 2. Task model

A task is a node in a tree; independent roots form a forest. Fields:

- `id` — `task-<YYYY-MM-DD>-<4hex>`.
- `parent_id` — attaches it under another task (creation order = sibling order).
- `goal` — the durable objective (used to rebuild context on resume).
- `content` — human description.
- `priority` — integer; higher runs first. Default 0.
- `status` — the lifecycle state (§3).
- `progress_summary` — the resume brief, set on pause/yield.
- `result` — completion summary, surfaced to the user.
- `origin` — `user` / `agent` / `trigger`.
- `due`, `tags`, `schedule` — optional.

**Leaves vs internal nodes.** A leaf does concrete work. An internal node is an
*orchestration node*: it goes `blocked` while children run and, when all children
are terminal, gets one **synthesis** turn that folds the children's results into
its own result.

## 3. Lifecycle

```
pending ──▶ active ──▶ done            (agent calls task(done))
   │          │   └──▶ cancelled
   │          ├──▶ blocked   (decomposed: waiting on children)
   │          ├──▶ suspended (yielded / budget exhausted)
   │          └──▶ pending   (preempted: re-queued)
blocked ──▶ active           (synthesis, when children all terminal)
suspended ──▶ pending        (resumed: CLI `resume`)
```

`done` and `cancelled` are **terminal** (sticky — a transition off them is
dropped). `pending` is the only state the scheduler picks up as new work;
`suspended` is *not* auto-resumed (it waits for an explicit `resume` or, for a
preempted task, it is re-queued as `pending` instead of suspended).

## 4. Scheduler

The **TaskScheduler** is an in-worker component (not a daemon — ADR-0001),
distinct from the cron scheduler: the cron scheduler decides *when to wake*; the
TaskScheduler decides *which task this beat runs*.

**Runnable** (this beat) = either:

- a `pending` **leaf** — execute it, or
- a `blocked` node whose children are **all terminal** — execute it (synthesis).

**Selection** (`next_runnable`): the highest-priority root tree first; within a
tree, depth-first through highest-priority siblings to the first runnable node.
The active root→leaf path is the **spine**; exactly one spine runs at a time (the
worker's single-consumer loop guarantees serial execution).

**Interleave with triggers.** Each beat, a queued trigger (user/cron input) takes
precedence over autonomous task work. When idle, the loop blocks; a `task`
mutation pokes it (a `task_wake` sentinel) so out-of-band task creation is picked
up promptly.

### 4.1 Per-task run + outcomes

Running a task: transition `→ active`, assemble a prompt (`goal` + parent-chain
summaries + `progress_summary` + child results for synthesis; knowledge/recall
come from the memory preamble), run it task-scoped. The agent signals its outcome
through the ordinary `task` tool; the scheduler then **classifies** the result:

| Outcome | Detected by | Next move |
|---|---|---|
| **DONE** | task is terminal | move on |
| **DECOMPOSED** | task gained children | `→ blocked`; run children, then synthesize |
| **YIELDED** | active, no children, not terminal | checkpoint + `→ suspended` |
| **PREEMPTED** | paused mid-run (§5) | checkpoint + `→ pending` (re-queue) |

**Implicit current task.** During a task-scoped run the runtime sets a *current
task id*; `task(done)` / `task(add)` default to it, so the agent finishes or
decomposes without restating the id, and `task(add)` without a parent becomes a
subtask (the decomposition signal).

## 5. Preemption (cooperative)

Preemption is **cooperative**, at turn boundaries — there is no token-level
interrupt. At each turn boundary the scheduler checks for a **preemptor**: a
runnable task that is *strictly higher priority* than the running task and
*outside its subtree* (equal priority never preempts; a task's own subtask never
preempts it).

On a preemptor, consent is sought per `tasks.scheduling.preempt`:

| `preempt` | Behavior |
|---|---|
| `off` | never preempt |
| `ask` | block on the user-decision channel ("Pause A to run B?"); decline if headless |
| `auto_by_priority` | auto-approve, audited |

Under permission mode `yolo`, `ask` is treated as auto (the "don't stop to ask
me" contract). On approval the running task is checkpointed and **re-queued as
`pending`** (not suspended), so once the preemptor — now higher priority — and
anything above it finish, the task is re-selected and resumes from its brief. A
`preempt_cooldown` guards against thrash.

## 6. Schedule → task-template bridge

`schedule` and `task` are **orthogonal axes** — *when to wake* vs *what to do* —
bridged, not merged. A task may carry a `schedule` (cron + `timezone`); this
registers a recurring dynamic trigger carrying a **task template**. Each fire
**hatches a fresh task instance** (`origin="trigger"`, new id) rather than
re-running one task, so every occurrence has its own result and history. The
template persists across worker restarts. The low-level `schedule` tool is
unchanged.

## 7. Anti-runaway guards

Configured under `tasks.scheduling` (`0` disables a cap):

| Guard | Enforced at | Effect |
|---|---|---|
| `max_tree_depth` | task creation | reject a subtask beyond the depth cap |
| `max_fanout` | task creation | reject a subtask beyond children-per-node |
| `per_task_budget_tokens` | turn boundary | end a run that has spent its token allowance (→ yield) |
| `max_suspended` | yield | when the suspended backlog is full, cancel the no-progress task instead of growing it |

## 8. When to create a task (policy, not mechanism)

Trivial requests are answered inline; only "slightly complex" work becomes a
task. This is a **prompt-level** policy (system prompt + the `task` tool
description), not a code classifier — the model judges the threshold. The
architecture only guarantees that once a task exists it is scheduled and run.

## 9. CLI

- `eonlet tasks <id>` — render the forest as a tree (read-only, folded offline
  from `state.db`); `--status` filters.
- `eonlet tasks <id> suspend|resume|cancel <task_id>` — lifecycle ops over the
  running worker (`resume` re-queues a suspended task → `pending`).
- `eonlet tasks <id> prio <task_id> <n>` — reprioritize.

## 10. Out of scope

Multi-agent delegation (one eonlet schedules only *its own* work; teams are
v0.6+), token-level preemption, and parallel task execution are explicitly not
part of this spec.
