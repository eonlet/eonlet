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

**The root tree is the scheduling unit** (ADR-0008 §2). The run queue is a queue
of **root trees** ordered by **root priority** (ties by creation order); the head
is the currently-executing tree. **There is no scheduling within a tree** —
execution is strict depth-first, and a subtask's `priority` has **no scheduling
effect** (it schedules only at the root; `task(add)` forces a subtask's priority
to 0). Subtasks run in **creation order** — the order the agent decomposed them.

**Runnable** (this beat) = either:

- a `pending` **leaf** — execute it, or
- a `blocked` node whose children are **all terminal** — execute it (synthesis).

**Selection** (`next_runnable`): the highest-priority root tree first; within a
tree, depth-first **in creation order** to the first runnable node. The active
root→leaf path is the **spine**; exactly one spine runs at a time (the worker's
single-consumer loop guarantees serial execution).

**Interleave with triggers.** Each beat, a queued trigger (user/cron input) takes
precedence over autonomous task work. When idle, the loop blocks; a `task`
mutation pokes it (a `task_wake` sentinel) so out-of-band task creation is picked
up promptly.

**Concurrent control plane** (ADR-0008 §1). The single LLM is one serial "compute
core" — at most one task beat *or* user turn at any instant (the one-worker
invariant). Alongside it a **non-LLM control plane** (the `task.*` IPC methods +
the `eonlet tasks` CLI) runs concurrently: create/insert, reprioritize, cancel,
query, and deliver results **without interrupting the running task**. Every
mutation flows through the single `runtime._record` reducer (anyio cooperative
scheduling — no true parallelism), so the forest projection stays consistent.
*Talking to the agent does not stop task execution* for control-plane
interaction; only work that needs the LLM time-slices on the core.

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

### 4.2 Scoped working context (ADR-0009)

A task run does **not** see the global conversation timeline. Each conversation
event (`USER_MESSAGE`/`ASSISTANT_MESSAGE`/`TOOL_CALL`/`TOOL_RESULT`/`TOOL_ERROR`)
is tagged with the **task scope** it was produced in (`Event.task_id`, stamped
centrally by `runtime._record` from the *current task id*; chat/cron turns are the
**chat scope**, `task_id = None`). The LLM window is filtered to the **current
scope**: a run of task `T` sees only `T`'s own turns plus the assembled framing
(§4.1) and the `<memory>`/`<tasks>` preambles; a chat turn sees only chat-scope
turns. This realizes the **asymmetric context-flow model**:

- **Down the tree** (parent → child, and chat → root): the child's framing carries
  a compressed *decision trace* (`Task.framing`), not just the `goal` — a child must
  understand how it was decomposed. Computed once on the child's first dispatch by
  compressing the parent's (or, for a user-origin root, the chat's) scope via the
  compaction LLM, stored on the task, and injected as a "Context from above"
  section. Trigger-origin roots and trivial sources stay goal-only; any failure is
  non-fatal (goal-only). (ADR-0009 M3.)
- **Up the tree** (child → parent): only the child's `result` returns; its internal
  turns never enter the parent's synthesis window.
- **Across siblings**: no direct sharing — siblings coordinate only through the
  parent. (Serial execution means there is never a concurrent conflict anyway.)

**Checkpoint brief is LLM-compressed (M2).** On suspend/yield/preempt the
`progress_summary` is produced by compressing the task's own-scope events into key
details, events, and **decisions** via the compaction LLM (decision continuity —
the basis for a coherent resume and, in M3, the down-tree trace). It falls back to
a structural summary (the task's last assistant turn) when memory is disabled, no
compaction provider resolves, or the call fails — a checkpoint is never blocked.

**Episodic memory is the chat scope only.** Tier-1/2/3 (STM/LTM, the `<memory>`
preamble) compact the conversation timeline; a task's turns are never promoted to
STM/LTM. A task's durable residue is its `result` (which surfaces into chat on
completion, and thereby into episodic memory), its `progress_summary` checkpoint
brief, and the full **recall-indexed** event log. The curated `knowledge/` axis is
global and injected into every scope. The chat compaction watermark applies to the
chat scope only; each task carries its **own brief watermark** (M4 below).

**Task-scope compaction (M4, reversible→irreversible).** While a task runs, recent
turns stay raw in its window; when the un-folded own-scope window exceeds
`working_memory_tokens`, a turn-boundary task-scope tier-1 folds the older portion
(keeping a recent tail) into the **cumulative** brief and advances the task's brief
watermark, pruning those turns from the window (they remain in the log for recall).
The brief is injected into the system prompt (`<task_progress>`), rebuilt each
turn, so continuity holds as the window prunes — no message re-injection. The same
cumulative-brief machinery serves the suspend/yield/preempt checkpoint (folding the
whole un-folded tail, boundary = latest). The structural fallback never advances the
watermark, so a lossy fallback can never prune raw turns.

## 5. Preemption (cooperative, unified on the queue head)

Preemption is **cooperative**, at turn boundaries — there is no token-level
interrupt. There is **one rule** (ADR-0008 §3): at each turn boundary the
scheduler rechecks the queue head; a switch happens **iff a *different* root tree
has root-priority strictly higher** than the running tree's. Equal priority never
preempts; a node inside the running task's own tree is never a preemptor (no
in-tree scheduling). This single rule subsumes both task-vs-task preemption and
the *user interrupt* (the user's new high-priority root tree becomes the head).

On approval the running task is checkpointed and **re-queued as `pending`** (not
suspended), so once the preemptor — now higher priority — and anything above it
finish, the task is re-selected and resumes from its brief.

### 5.1 Consent splits by *who changed the head* (ADR-0008 §4)

Eligibility and consent depend on the contender root tree's `origin`:

| Contender root `origin` | Preempts? | Consent / cooldown |
|---|---|---|
| `user` | yes | **none** — the user is the initiator; asking permission to attend to their own request is absurd, and a cooldown would only delay it |
| `agent` | yes | governed by `tasks.scheduling.preempt`: `off` never; `ask` blocks on the user-decision channel ("Pause A to run B?", declines if headless); `auto_by_priority` auto-approves, audited. `yolo` treats `ask` as auto. `preempt_cooldown` guards against thrash |
| `trigger` (hatched) | **no** | scheduled/autonomous work is background; it waits for a natural task boundary and never interrupts foreground work |

Under "scheduling only over roots" (§4) the agent decomposes *in-tree* and rarely
spawns a rival **root**, so the `agent` row is largely dormant — in practice
preemption is user-initiated and consent-free.

### 5.2 User-input interrupt

A queued **interactive user message** (the IPC `message.send` path) preempts the
running task unconditionally and without cooldown (it is the `user` row above,
even before a task exists): the worker marks the interrupt, the running task
yields at its next turn boundary and is re-queued as `pending`, and the message is
handled as a normal top-priority user turn — which may itself create a new
higher-priority root (the headline "user gives a new urgent task" scenario). For
the new root to actually run before the paused task resumes, it must carry a
**higher priority** (a prompt-level nudge, per §8).

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
