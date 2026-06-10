# ADR-0008: Scheduling Refinement — Root-Tree Scheduling Unit, Unified Preemption, and a Concurrent Control Plane

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-06-01 |
| Accepted | 2026-06-01 |
| Deciders | Ziyu |
| Supersedes | – (partially supersedes [ADR-0007](0007-task-scheduling.md) §3 *selection* and §6 *preemption*; the data model, lifecycle, bridge, and guards of ADR-0007 stand) |
| Superseded by | – |

## Context

ADR-0007 shipped (v0.0.10) a priority-ordered task forest with cooperative
preemption. Three things surfaced in design review that this ADR settles.

**1. The headline interrupt scenario is unreachable.** *"While the agent works on
task A, the user says do this other urgent thing, and the agent switches."* Today a
new interactive user message lands on the IPC queue while the worker is inside
`_run_task → handle_user_message`. `_main_loop` only checks the queue at **beat
boundaries** (`worker/main.py:284`), and `pause_check` looks **only** at the task
forest (`tasks/scheduler.py:preemptor`), never at the queue — so the message waits
until A hits a natural boundary. The user cannot interrupt.

**2. Preemption granularity was never pinned down.** ADR-0007's `preemptor`
compares the running **leaf's** priority against a contender and excludes only the
running node's **subtree** — so a sibling in the *same* tree could "preempt." That
contradicts the intended mental model: a task tree is a single unit of focused
work; you don't context-switch in the middle of your own decomposition.

**3. "Two threads" — can the agent talk while it works?** A natural wish: converse
with the agent (add tasks, ask status, get results) *without* stopping the running
task. The tension is that this system has **one LLM** — one "CPU core." Two
concurrent LLM generations against one shared conversation window
(`runtime.state.messages`, which both task runs and user turns mutate) would
corrupt context, and it breaks the "single human-like worker" invariant ADR-0007
is built on (and reopens ADR-0007's rejected Alternative C — a separate working
context per activity).

### The resolving insight: an OS run-queue with one core

ADR-0007 already calls the scheduler "the agent's analogue of an OS process
scheduler." Take that literally: a single-core OS runs **one thing at a time**;
its "concurrency" is *interleaving on the core* plus *cheap I/O off the core*. So:

- The **compute core** (the one LLM) runs exactly one thing at a time — a task beat
  *or* a user turn — selected from a priority **run queue**.
- A **control plane** (cheap, non-LLM I/O — like a shell + syscalls) can run
  *concurrently*: accept new work, reprioritize, cancel, report status, deliver
  results. None of it touches the LLM, so none of it stops the running task.

This holds the single-worker invariant (decision **甲** in review) while delivering
the "talk without interrupting" goal for everything that doesn't actually need the
core.

## Decision

### 1. Hold the single-worker invariant; split a concurrent control plane from the serial compute core

Keep "at most one LLM activity at any instant." Add a **concurrent control plane**:
a non-LLM path (already latent in the IPC handlers + the offline `eonlet tasks` CLI)
that, *while a task runs*, can:

- **create / insert** a task (append `TASK_CREATED`),
- **reprioritize / cancel / update** a task (append the lifecycle/update event),
- **query** the forest (pure read — render status, inspect a task),
- **deliver** a finished task's `result` to the user.

These never block the core and never interrupt the running task. The **compute
core** stays the existing serial `_main_loop`: each beat it runs the single
highest-priority piece of work. A user message that genuinely needs an LLM answer
is *compute-core work* (top priority, see §3) — it time-slices on the one core, it
does not run truly parallel to a task.

Rejected the alternative (**乙**): two concurrent LLM threads for chat-vs-execution.
It reopens ADR-0007 Alt C (per-activity working contexts), doubles token and
compaction cost, and abandons the one-worker manifesto framing. Concrete blocker:
task runs and user turns share one `runtime.state.messages` window today.

### 2. The scheduling unit is the root tree; no scheduling inside a tree

The run queue is a queue of **root trees**, ordered by **root priority** (ties by
creation order). The head is the currently-executing tree. **Within a tree there is
no scheduling and no preemption** — execution is strict depth-first: a parent
decomposes, its children run in order, then the parent gets its synthesis turn.

Consequences for priority semantics:

- **Only a root's `priority` schedules.** It is the whole tree's weight.
- **A subtask's `priority` is dropped from scheduling entirely.** Subtasks run in
  **creation order** (the order the agent decomposed them — its intended sequence).
  `_ordered_children` stops sorting by `-priority`; the `priority` argument to
  `task(add)` is honored **only** when creating a root and is ignored for subtasks.
  (The `Task.priority` field remains on the dataclass; it just has no effect off a
  root.)
- A high-priority subtask can **never** jump out of its tree or preempt another
  tree — it is in-tree work, full stop.

Within-tree progression needs **no preemption machinery**: when the running node
goes done / decomposed / yielded, the next beat's `next_runnable` simply picks the
tree's next DFS node, because that tree is still the head.

### 3. One unified preemption rule: the queue head changed

At each turn boundary, recompute the head (`next_runnable` over the live forest). A
switch happens **iff a *different* root tree's root-priority strictly exceeds the
running tree's root-priority.** On a switch: checkpoint the active node
(`TASK_CHECKPOINTED`), re-queue it as `pending` (ADR-0007's re-queue refinement —
the tree resumes by priority later), and run the new head.

This **one rule subsumes** both prior cases:

- *task-vs-task* (ADR-0007 §6) → "another root tree outranks me."
- *user interrupt* (this ADR's original motivation) → the user's control-plane
  action creates/raises a root tree that becomes the new, higher head.

`preemptor` is rewritten accordingly: add `forest.root_of(task_id)`; compare
**root** priorities; exclude the **entire current root tree** (not just the current
subtree). No separate `user_input_pending` flag is needed — the control plane
appends the task event, the runtime reduces it into `task_forest` (`agent.py:520`),
and the turn-boundary head recompute sees it directly.

A user turn that requires an LLM response is treated as **top-priority compute-core
work** (a transient `origin="user"` root); it preempts via this same rule, runs,
completes, and the prior tree resumes. Everything the core does is thus one queue.

### 4. Consent is split by *who changed the head*

Preemption eligibility and consent depend on the contender root tree's `origin`:

| Contender root origin | Preempts? | Consent / cooldown |
|---|---|---|
| `user` | yes | **none** — the user is the initiator; asking "may I attend to what you just said?" is absurd, and a cooldown would only delay the user's own interrupt |
| `agent` | yes | ADR-0007 §6 unchanged: `ask` prompts via `DecisionBroker`, `auto_by_priority`/`yolo` auto-approve with audit; `preempt_cooldown` applies |
| `trigger` (hatched) | **no** | autonomous/scheduled work is background; it waits for a natural task boundary and never interrupts foreground work |

Note: under §2 (scheduling only over roots; the agent decomposes *in-tree*) the
agent rarely spawns a rival **root** tree by its own judgment, so the `agent`
row is largely **dormant** — kept for completeness and forward-compatibility. In
practice preemption is user-initiated and consent-free.

### 5. `origin="user"` on user-originated root tasks

The `task` tool hardcodes `origin="agent"` today (`tools/builtin/task.py:190`).
Refine: a task created during a **user-originated turn** records `origin="user"`; a
subtask created during a **task-scoped run** stays `origin="agent"`; hatched
instances stay `origin="trigger"`. `current_task_id is None` is insufficient (a
cron-triggered root also has none), so the worker threads the turn origin
explicitly via a new `ToolContext.turn_origin` (`"user" | "agent" | "trigger"`),
which the `task` tool maps to `origin` for root creations. Semantic label only — no
structural rule that user tasks *must* be roots (mark, don't enforce); it is also
exactly the signal §4 reads to gate consent.

## Consequences

### Positive

- **The interrupt scenario works**, and via *one* rule (head changed) rather than a
  bespoke flag — task-vs-task and user-interrupt collapse together.
- **"Talk without stopping work" holds** for all control-plane interaction
  (add/insert/reprioritize/cancel/query/deliver) — the bulk of what a user does to
  a busy agent — with no second LLM.
- **Cleaner priority model.** One scheduling weight per *tree*; subtasks are pure
  in-order decomposition. No confusing "which node's priority wins."
- **On-brand.** A single-core run queue + a non-LLM control shell is the literal
  OS-scheduler analogy "the systemd for agents" promises.
- **Minimal new machinery.** `root_of` + a `preemptor` rewrite + `turn_origin` + a
  concurrent control-plane dispatch. No new event kinds; `DecisionBroker` reused
  (and mostly dormant) for the `agent` branch only.

### Negative

- **A trivial interrupt still costs one checkpoint round-trip** (pause → handle →
  resume). Cheap, bounded by human cadence.
- **Substantive chat is not truly parallel to work** — it time-slices on the core.
  This is the deliberate cost of holding 甲; users who expected "answers me while it
  keeps working" get interleaving, not parallelism.
- **Resume fidelity is bounded by the structural checkpoint** (inherited from
  ADR-0007 M2; LLM-enriched briefs still deferred).
- **Concurrency correctness burden.** The control plane appends events while the
  core runs; appends are serialized through the event store, but the `task_forest`
  projection update and the core's turn-boundary read must be ordered so a switch
  decision sees a consistent forest. Needs care in the worker (single reducer owner).

### Neutral

- ADR-0007's data model, lifecycle, schedule→task bridge, and guards are unchanged.
  Only §3 *selection* (now root-tree-unit) and §6 *preemption* (now the unified
  head rule + origin-split consent) are revised.
- `agent.yaml` is unchanged: `tasks.scheduling.preempt`/`preempt_cooldown` still
  govern the **`agent`** consent branch; the `user` branch is unconditional and the
  `trigger` branch never preempts, so no new config keys.
- `ToolContext` gains `turn_origin`; `Task.priority` loses meaning off a root but
  the field stays.

## Implementation sketch

Single milestone:

1. **`tasks/forest.py`**: add `root_of(task_id) -> Task | None` (walk `parent_id`,
   cycle-guarded like `depth`). Change `_ordered_children` to sort by
   `(created_at, id)` only — drop `-priority` (§2).
2. **`tasks/scheduler.py`**: rewrite `preemptor` to compare `root_of(current)` vs
   `root_of(contender)` priorities and exclude the whole current root tree; return
   the contender only when its root strictly outranks *and* its root `origin` is
   preemption-eligible (`user`/`agent`, not `trigger`) (§3, §4).
3. **`worker/main.py`**: `_make_pause_check` calls the new `preemptor`; the
   user-origin branch skips `_approve_preempt` + cooldown, the agent-origin branch
   keeps them (§4). `_run_task` re-queues the paused node as `pending`
   `reason:"preempted:<origin>:<root_id>"`. The separate `user_input_pending` flag
   from the earlier draft is **not** introduced (§3).
4. **Control plane (`worker/ipc.py`)**: route pure task-control ops
   (create/insert/reprioritize/cancel/update + status read + result delivery)
   through a **non-LLM** handler that appends events / reads the forest without
   entering `handle_user_message`; only substantive user turns enter the compute
   core (§1).
5. **`tools/protocol.py` + worker**: add `ToolContext.turn_origin`; the worker sets
   it per dispatched turn. **`tools/builtin/task.py`**: map `turn_origin` →
   `origin` for root creation; honor `priority` only for roots (§5, §2).
6. **Prompting**: nudge the agent to set a higher root `priority` for urgent user
   requests so the new root actually leads the queue.
7. **Docs**: rewrite TASK_SPEC §3–§4 (root-tree unit, unified preemption,
   origin-split consent, control plane); CLAUDE.md / CHANGELOG note the refinement.
8. **Tests**: a user-created higher-priority root preempts at the turn boundary; an
   in-tree higher-priority subtask does **not** preempt; a `trigger` root never
   preempts; an `agent` root preempts only with consent/cooldown; subtasks run in
   creation order; user-origin roots record `origin="user"`; control-plane ops
   mutate the forest without interrupting a running task.

## Alternatives Considered

### A. Status quo — user waits for the next natural task boundary
Rejected; the interrupt gap is the whole motivation.

### B. Convert the user message to a task first, then use ADR-0007's node `preemptor`
Subsumed, not rejected: the control plane *does* create a task — but the preemption
rule is recast to root-tree granularity (§3), not the original node-level compare.

### C. 乙 — two concurrent LLM threads (chat ∥ execution)
Rejected. Reopens ADR-0007 Alt C, doubles cost, breaks one-worker, and the shared
`state.messages` window makes it incorrect without per-activity contexts.

### D. Keep node-level priority / in-tree preemption
Rejected (§2). A tree is one unit of focused work; switching mid-decomposition is
neither needed (the beat loop advances DFS for free) nor desirable.

### E. Subtask priority orders DFS siblings
Rejected (decision ①). Subtasks run in **creation order**; priority is a root-only
scheduling weight. Simpler and matches "depth-first in the order I planned."

### F. One consent policy for all preemption
Rejected (decision ②). Consent is split by initiator: user-initiated switches are
unconditional; agent-judgment switches keep ADR-0007 §6 consent; triggers never
preempt.

### G. Let cron / hatched triggers preempt
Rejected (§4). Autonomous stimuli are background work; only the user interrupts
foreground work.

### H. A `user_input_pending` flag peeked by `pause_check` (the earlier draft's §3)
Superseded by §3 here: since the control plane turns user input into a task event
that reduces into the shared `task_forest`, the unified head recompute already sees
it — no peek/flag needed.

## Resolved Decisions

1. **甲** — hold the single-worker invariant: concurrent non-LLM control plane +
   single serial LLM compute core. (Rejected 乙.)
2. **Root tree is the scheduling unit; no scheduling within a tree** (strict DFS).
3. **Subtask priority dropped** — subtasks run in creation order; `priority`
   schedules only at the root (decision ①).
4. **Unified preemption**: switch iff a different root tree's root-priority strictly
   exceeds the running tree's, evaluated at the turn boundary.
5. **Consent split by initiator** (decision ②): `user` unconditional, `agent`
   keeps ADR-0007 §6 consent + cooldown, `trigger` never preempts.
6. **`origin="user"`** on user-originated root tasks via `ToolContext.turn_origin`.

Open (deferred, not blocking): LLM-enriched checkpoint briefs (inherits ADR-0007's
deferral); whether substantive chat should be reified as an explicit transient root
task or kept as a queued top-priority user turn (implementation detail of §3).

## References

- [ADR-0007](0007-task-scheduling.md) §3 (selection), §6 (preemption) — revised
  here; §1/§4/§5/§7 (data model, resume, bridge, when-to-spawn) unchanged
- [ADR-0006](0006-compaction-triggers.md) — `DecisionBroker`, reused only for the
  `agent` consent branch (§4)
- [ADR-0001](0001-no-supervisor-mvp.md) — all scheduling in the one worker; the
  control plane is in-process I/O, not a daemon
- `worker/main.py` — `_main_loop`, `_make_pause_check`, `_run_task`, `_try_receive`
- `worker/ipc.py` — the control-plane dispatch (§1, §4 step)
- `tasks/scheduler.py` — `next_runnable`, `preemptor` (rewritten), `_ordered_children`
- `tasks/forest.py` — `roots()`, `depth()`, new `root_of()`
- `tools/builtin/task.py` — `origin` hardcode (`:190`), root-only `priority`
- `tools/protocol.py` — `ToolContext`, gaining `turn_origin`
- `docs/TASK_SPEC.md` §3–§4 — to be rewritten for this model

## Update history

- 2026-06-01: First draft scoped narrowly as a §6 extension ("user-input
  preemption"). Superseded same day after review by this broader refinement:
  hold single-worker (甲) with a concurrent control plane; root tree as the
  scheduling unit with no in-tree scheduling; subtask priority dropped (①);
  one unified head-based preemption rule; consent split by initiator (②).
  Status → Proposed, pending acceptance.
- 2026-06-01: Accepted and implemented. `TaskForest.root_of`; `preemptor`
  rewritten (root-priority compare, exclude the whole current tree, skip
  trigger-origin roots); `_ordered_children` → creation order; subtask `priority`
  forced to 0 + root `origin` from `ToolContext.turn_origin` in the `task` tool
  and the `task.add` IPC; `_make_pause_check` user-input interrupt
  (`AgentRuntime.pending_interactive`) + consent split; TASK_SPEC §4–§5 and the
  CHANGELOG updated. The concurrent control plane was already present from
  ADR-0007 M4 (`task.*` IPC + `eonlet tasks` CLI) and is now documented as such.
  No new `EventKind` (the preempt reason string carries the contender origin).
  605 → 611 tests; ruff + mypy clean. Status → Accepted.
