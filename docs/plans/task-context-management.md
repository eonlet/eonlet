# Work Summary — Task Scheduling Refinement & Hierarchical Context Management

Date: **2026-06-02**. Author: Ziyu (with Claude).
Companion to [ADR-0008](../adr/0008-user-input-preemption.md) and
[ADR-0009](../adr/0009-task-scoped-context.md). Normative spec updates landed in
[TASK_SPEC §4–§5](../TASK_SPEC.md) and [MEMORY_SPEC §3.2/§4.1](../MEMORY_SPEC.md).

This document records *what was built today, why, and how* so a future
contributor can extend, tune, or refactor the task subsystem without re-deriving
the reasoning. It is a development log, not a normative spec — where this and the
ADRs/SPECs disagree, the ADRs/SPECs win.

---

## 0. TL;DR

Two ADRs were proposed, accepted, and implemented today, on top of the v0.0.10
task scheduler (ADR-0007):

- **ADR-0008 — Scheduling refinement.** The scheduling unit is the **root tree**
  (no scheduling *within* a tree; strict DFS in creation order). One **unified
  preemption rule** (the queue head changed) subsumes task-vs-task preemption and
  user-input interrupts. Consent splits by *who* changed the head. A **concurrent
  non-LLM control plane** lets the user create/inspect/reprioritize tasks without
  interrupting the running one.
- **ADR-0009 — Hierarchical task context management.** An **asymmetric
  context-flow model** over the single event log: a compressed **decision trace
  flows down** (parent→child, chat→root), only the **`result` flows up**
  (child→parent synthesis), and **siblings share nothing** directly. Implemented as
  *scoped views* (`Event.task_id`) + an **LLM compression** step reused for resume
  briefs, down-tree traces, and task-scope compaction.

Test count went 605 → **628**; ruff + mypy clean throughout. No new `EventKind`
(still 41). Two new structural fields (`Event.task_id`, plus task fields). One
additive SQLite migration.

The intellectual core of ADR-0009 came from researching how the field handles
hierarchical-agent context (Cognition "Don't Build Multi-Agents", Anthropic's
multi-agent Research, Manus context engineering, Claude Code subagents). The
synthesis: **Eonlet is serial single-worker**, which structurally avoids the
concurrent-conflict failure mode the multi-agent debate centers on — so it can
combine the *isolation* school (clean results up the tree) with the *shared-trace*
school (decisions down the tree).

---

## 1. Starting point (what existed before today)

ADR-0007 (v0.0.10) had shipped:

- Event-sourced task forest (`tasks/forest.py`): `Task`, `TaskForest`,
  `fold_tasks`/`reduce_task`. Task events: `TASK_CREATED/UPDATED/TRANSITIONED/
  CHECKPOINTED/DELETED`. Runtime owns `AgentRuntime.task_forest`.
- TaskScheduler (`tasks/scheduler.py`): `next_runnable`, `classify_post_run`,
  `preemptor`, `creation_guard_error`. Pure functions over the forest.
- Worker integration (`worker/main.py`): `_run_task`, `_make_pause_check`,
  `_checkpoint_summary` (structural), `_hatch_task`, the cron→task bridge.
- Per-task framing (`tasks/context.py`): `build_task_prompt` (goal + parent chain
  + progress_summary + child results).

Two gaps were identified in review and became today's work:

1. A new interactive user message could **not interrupt** a running task — it
   waited on the queue until the task hit a natural boundary. (→ ADR-0008)
2. A task run **shared the global conversation window** — `_run_task` dispatched
   via `handle_user_message`, recording the framing as a plain `USER_MESSAGE` into
   the single `AgentState.messages` timeline; the LLM window was sliced from that
   one list. So a running task saw interleaved chat + other tasks + cron turns
   (cross-talk, cost, timeline bloat). The checkpoint brief was purely structural
   (last assistant message). (→ ADR-0009)

---

## 2. ADR-0008 — Scheduling refinement

### 2.1 Decisions (and the alternatives rejected)

1. **甲, not 乙** — hold "one worker / at most one LLM activity at a time."
   Add a *concurrent non-LLM control plane*, not a second LLM thread. Rejected 乙
   (chat ∥ execution as two LLM threads) because it reopens ADR-0007's rejected
   Alternative C (separate per-activity contexts), doubles cost, and the shared
   `state.messages` window makes it incorrect without per-activity contexts.
2. **Root tree is the scheduling unit; no scheduling within a tree.** Priority
   schedules only at the **root**; a subtask's priority has *no* scheduling effect
   and is forced to `0` at creation. Subtasks run in **creation order** (DFS).
3. **Unified preemption rule:** at a turn boundary, switch iff a *different* root
   tree's **root-priority** strictly exceeds the running tree's. Subsumes
   task-vs-task and user-interrupt.
4. **Consent splits by initiator** (contender root `origin`): `user` preempts with
   **no consent / no cooldown**; `agent` keeps ADR-0007 §6 `preempt`/cooldown/
   `DecisionBroker`; `trigger` (scheduled) **never** preempts foreground work.
5. **User input is a preemption signal.** A queued interactive message makes the
   running task yield at its next turn boundary and re-queue as `pending`; the
   message is then a normal top-priority turn (which may create a new root).
6. **`origin="user"` on user-originated roots** via a new `ToolContext.turn_origin`
   threaded by the worker per turn.

### 2.2 Implementation map

| Concern | Location | Note |
|---|---|---|
| Root accessor | `tasks/forest.py: TaskForest.root_of()` | walk to root, cycle-guarded |
| Subtask DFS order | `tasks/scheduler.py: _ordered_children` | now creation order (was `-priority`) |
| Unified preemptor | `tasks/scheduler.py: preemptor` | compares **root** priorities, excludes whole current tree, skips `trigger` roots |
| User-input signal | `runtime/agent.py: AgentRuntime.pending_interactive` | incremented by IPC `message.send`, decremented by `_main_loop` |
| Turn origin | `tools/protocol.py: ToolContext.turn_origin` + `AgentRuntime.turn_origin` | worker stamps per turn |
| Pause hook | `worker/main.py: _make_pause_check` | 3 checks in order: user-input (unconditional) → per-task budget → cross-tree preemptor (consent split) |
| Root-only priority + origin | `tools/builtin/task.py` + `worker/main.py` `task.add` IPC | subtask → priority 0, origin "agent"; root → `turn_origin` |
| Control plane | `worker/main.py: _make_handler` `task.*` methods | already present from ADR-0007 M4; documented as the non-LLM path |

### 2.3 Subtleties worth remembering

- The control plane was **already latent** (the `task.*` IPC + `eonlet tasks`
  CLI mutate the forest via `runtime._record`). ADR-0008 §1 is largely
  *confirmation + documentation* — no new IPC machinery. It is safe because anyio
  is cooperatively scheduled and every mutation funnels through the single
  `_record` reducer; there is no true parallelism to race.
- The `agent`-origin preemption branch is **largely dormant**: under "schedule
  only over roots; agent decomposes in-tree," the agent rarely spawns a rival
  *root*. Kept for completeness/forward-compat.
- The earlier draft's dedicated `user_input_pending` *peek* idea was kept as
  `pending_interactive` (a counter), because anyio receive streams can't peek and
  `receive_nowait` would consume the message irrecoverably.

---

## 3. ADR-0009 — Hierarchical task context management

### 3.1 The model (the part to internalize)

```
            chat scope  (the user conversation; episodic memory lives here)
                │  creates a root task → compressed "why" flows DOWN
                ▼
        root task T ── decomposes → decision trace flows DOWN to each child
        │
        ├── child C1 ──result──┐     ├── child C2 ──result──┐   (siblings:
        │   (own scope)        │     │   (own scope)        │    no direct
        │                      ▼     ▼                      ▼    sharing)
        │            synthesis turn of T: T's own scope + child RESULTS only
        │  T completes → result flows UP into chat
        ▼
            chat scope sees T's RESULT, never T's internals
```

- **Down** (parent→child, chat→root): a **compressed decision trace**, not just
  the goal. (Cognition: "subtasks fail when they don't understand how the parent
  decomposed.")
- **Up** (child→parent): only the `result`. Child internals never enter the
  parent's window. (Isolation school: information hiding upward.)
- **Across siblings**: nothing direct; coordinate via the parent. (Serial
  execution already prevents concurrent conflict.)

**Why Eonlet can do this**: it is a serial single worker (ADR-0007/0008), the
architecture Cognition endorses, which removes the concurrent-conflict failure
mode — so it freely combines isolation (up) with shared-trace (down).

### 3.2 Substrate: one event log, scoped views

- **`Event.task_id: str | None`** (mirrors the `trigger_id` slot). Stamped
  **centrally** in `runtime/agent.py: AgentRuntime._record` from
  `current_task_id`, only for the conversation family (`USER_MESSAGE`,
  `ASSISTANT_MESSAGE`, `TOOL_CALL`, `TOOL_RESULT`, `TOOL_ERROR`). Chat/cron =
  `None`. Task/memory/bookkeeping events stay scope-neutral.
- **`Message.task_id`** mirrors it (`runtime/state.py: reduce`).
- **Scoped window** (`_build_llm_messages`): a task run sees only `task_id == T`;
  a chat turn sees only `task_id is None`. The **chat compaction watermark applies
  to the chat scope only** (task turns are never in STM, so the watermark must not
  hide them); a task scope uses its **own brief watermark** (M4).
- **Episodic memory = chat scope** (`memory/injection.py: chat_scope_only`,
  applied in `tier1.run_tier1`, the tier-1 trigger, and the propose-compact guard).
  Task turns are never promoted to STM/LTM. The curated `knowledge/` axis stays
  **global** (injected into every scope). A task's residue = `result` + brief +
  the recall-indexed log (recall indexes everything, unchanged).

### 3.3 LLM compression (reused three ways) — `tasks/brief.py`

A single module, plain-text output (not the JSON-section schema of
`memory/compactor.py`, which targets STM). All callers fall back gracefully.

| Use | Function | Prompt | When |
|---|---|---|---|
| Resume brief | `build_task_brief(prior_brief=…)` | `BRIEF_SYSTEM_PROMPT` | suspend/yield/preempt; mid-run overflow |
| Down-tree trace | `build_decision_trace` | `TRACE_SYSTEM_PROMPT` | child's first dispatch |

The brief is **cumulative**: `prior_brief + new events → updated brief`, so a long
task's older content survives in the rolling brief.

### 3.4 Milestones (all four shipped today)

- **M1 — scoping substrate.** `Event.task_id` (+ additive store column + index +
  migration), central `_record` stamping, `Message`/`reduce` mirror, scoped
  `_build_llm_messages`, chat-scope-only tier-1. *Fixes cross-talk.*
- **M2 — LLM checkpoint brief.** `_checkpoint_summary` async, compresses the task's
  own scope into key details/events/**decisions**; scope-aware structural fallback.
- **M3 — down-tree decision trace.** `Task.framing` (reduced from
  `TASK_UPDATED(framing=…)` — no new EventKind); `build_task_prompt` injects a
  "Context from above" section; `worker._ensure_framing` computes once on first
  dispatch (parent scope for a subtask, chat scope for a user-origin root).
- **M4 — task-scope compaction (reversible→irreversible).** Per-task
  `Task.brief_watermark` (advanced by `TASK_CHECKPOINTED.boundary_event_id`,
  monotonic); turn-boundary hook `AgentRuntime.on_turn_boundary` →
  `worker._maybe_compact_task` folds older own-scope turns (keeps a ~30% tail) into
  the cumulative brief and prunes them; the brief is injected into the **system
  prompt** as `<task_progress>` (rebuilt each turn ⇒ continuity without
  re-injecting messages).

### 3.5 Implementation map

| Concern | Location |
|---|---|
| Scope field | `runtime/events.py: Event.task_id`; `runtime/store.py` (column/index/migration) |
| Central stamping | `runtime/agent.py: _record` (+ `_SCOPED_KINDS`) |
| Message mirror | `runtime/state.py: Message.task_id`, `reduce` |
| Scoped window | `runtime/agent.py: _build_llm_messages` |
| Live brief in sys prompt | `runtime/agent.py: _build_system_prompt` (`<task_progress>`) |
| Turn-boundary hook | `runtime/agent.py: on_turn_boundary` field + call in `_run_until_end` |
| Chat-scope filter | `memory/injection.py: chat_scope_only`; `memory/tier1.py`; `worker/main.py: _maybe_run_tier1`; `tools/builtin/memory.py` (propose guard) |
| Brief/trace LLM | `tasks/brief.py` |
| Framing field | `tasks/forest.py: Task.framing` (reduce from `TASK_UPDATED`) |
| Down-tree compute | `worker/main.py: _ensure_framing` |
| Brief watermark | `tasks/forest.py: Task.brief_watermark` (reduce from `TASK_CHECKPOINTED.boundary_event_id`) |
| Cumulative brief + boundary | `worker/main.py: _checkpoint_summary` (returns `(brief, boundary)`) |
| Task-scope compaction | `worker/main.py: _maybe_compact_task` |
| Prompt injection | `tasks/context.py: build_task_prompt` ("Context from above") |

### 3.6 Subtle invariants (do not break these in a refactor)

1. **The structural checkpoint fallback returns `boundary=None`.** A lossy
   fallback must never advance the brief watermark — otherwise raw turns get
   pruned with nothing faithful representing them. Only an LLM-built brief may
   advance the watermark.
2. **The brief lives in the system prompt during a task run, not as a re-injected
   message.** This is *why* M4 has no message-ordering hack: `_build_system_prompt`
   is rebuilt every turn, so as the window prunes, the brief stays present. If you
   move framing into the system prompt entirely (see §5), keep this property.
3. **The chat watermark is chat-scope only; each task has its own brief
   watermark.** Mixing them (e.g. one global watermark) silently drops task turns
   from the window. M1's `_build_llm_messages` deliberately does *not* apply the
   chat watermark to a task scope.
4. **Only the conversation family is scope-stamped.** Task/memory events are
   scope-neutral (`task_id None`) by design — they belong to the forest/memory
   projections, not a window.
5. **Knowledge is global; episodic is chat-scope.** Don't promote task turns into
   STM/LTM (it would swamp the user-conversation memory).

---

## 4. Test coverage added today

| Area | File | What it locks |
|---|---|---|
| Root-tree preemption | `tests/unit/tasks/test_scheduler.py` | root-priority compare, trigger-skip, in-tree non-preemption, creation-order DFS |
| `root_of` | `tests/unit/tasks/test_forest.py` | walk-to-root |
| Subtask priority/origin | `tests/unit/tasks/test_tools_task.py` | subtask priority forced 0, origin agent; root origin from turn_origin |
| `task_id` persistence + migration | `tests/unit/test_event_store.py` | roundtrip + legacy-DB `ALTER TABLE` |
| Scope stamping + window | `tests/unit/memory/test_agent_injection.py` | `_record` stamping, scoped window, episodic exclusion, `_ensure_framing`, task-scope compaction fold+prune+`<task_progress>` |
| Brief/trace | `tests/unit/tasks/test_brief.py` | prompt assembly (cumulative + trace), provider call, scope-aware structural fallback |
| Framing/watermark reducers | `tests/unit/tasks/test_forest.py` | `framing` from `TASK_UPDATED`, `brief_watermark` monotonic from checkpoint boundary |
| Prompt injection | `tests/unit/tasks/test_context.py` | "Context from above" present/absent |
| Preempt reason | `tests/integration/test_worker_inprocess.py` | `preempted:user:<id>` |

**Coverage note for the fake providers:** the in-process worker integration tests
use `FakeProvider` variants. `fake-task-tree`'s `complete` (used by the trace/brief
calls) returns tool-call content with empty text, so in that test the trace ends up
empty and is harmlessly skipped — the *controlled* M3/M4 behavior is asserted via
the real `AgentRuntime` + `_Recorder` fixture in `test_agent_injection.py` (its
`complete` returns `"ok"`). If you add provider-dependent assertions to the
integration tests, account for this.

---

## 5. Known limitations & open items (for future work / refactor)

Carried in the ADRs as "Open / deferred"; collected here for visibility.

1. **`fold` materializes all scopes in memory.** Only the *window* is filtered;
   `AgentState.messages` still holds every conversation message. For very large
   logs this is the same footprint as before. A lazy/segmented fold (or scope-
   indexed read) is the natural optimization — gate it behind its own tests.
2. **Down-tree trace bound for a chat→root edge** uses the *tail* of the chat
   scope (`_MAX_BRIEF_EVENTS=120`), not "chat turns strictly before task creation."
   Fine in practice (the task runs soon after creation, and the chat is frozen
   during the task run), but if chat continues heavily between creation and first
   dispatch, the trace tail could include post-creation chatter. Bounding by the
   task's creation event id would be exact (needs storing that id on the task).
3. **`_ensure_framing` / `_checkpoint_summary` / `_maybe_compact_task` each do a
   full `store.read()`** then filter by `task_id` in Python. Fine at current scale
   (checkpoints/compactions are infrequent), but the `events_task_idx` index now
   exists — a `read(task_id=…)` store method would make these O(scope) not O(log).
4. **Trace/brief quality is load-bearing and unevaluated.** A bad compression
   degrades a subtask more visibly than the old no-trace behavior. `recall` is the
   escape hatch, but there is no eval harness for brief/trace fidelity yet.
5. **The `agent`-origin preemption branch is dormant** (see §2.3). If a future
   design lets the agent spawn rival root trees by judgment, exercise + test it.
6. **`<tasks>` block still lists all pending leaves forest-wide during a focused
   run** (ADR-0008 review #4, left open). Narrowing it to the current spine during
   a task run is a possible refinement.
7. **`eonlet tasks <id> prio <subtask> <n>`** still stores a priority on a subtask
   that has no scheduling effect (ADR-0008 §2). Consider a CLI hint or rejection.
8. **Mid-run task compaction re-summarizes from the brief each time** (cumulative).
   For a pathologically long single task this is repeated LLM cost; acceptable
   given how rare runaway single-leaf tasks are (most work decomposes).

### Refactor guidance

- If you ever move the task **framing into the system prompt** entirely (instead
  of the kickoff `USER_MESSAGE`), you can drop the "framing is a scoped message"
  subtlety — but preserve invariant §3.6.2 (brief always present, rebuilt per
  turn) and make sure turn-1 still has a triggering user message for the loop.
- If you introduce a **snapshot/cache** for the forest or a segmented fold (item
  1), keep the event log authoritative (Invariant #1) and add consistency tests
  that replay-vs-cache match.
- The three compression callers (`brief`, `trace`, mid-run compaction) share
  `tasks/brief.py`. If a fourth emerges, resist inlining a fourth prompt in the
  worker — keep prompts in `brief.py`.

---

## 6. Cross-references

- ADRs: [0007](../adr/0007-task-scheduling.md) (base), [0008](../adr/0008-user-input-preemption.md),
  [0009](../adr/0009-task-scoped-context.md).
- Specs: [TASK_SPEC](../TASK_SPEC.md) §3 (scheduling unit), §4 (run + scoped
  context + compaction), §5 (preemption + consent + control plane);
  [MEMORY_SPEC](../MEMORY_SPEC.md) §3.2 (scoped window), §4.1 (chat-scope tier-1).
- Research that shaped ADR-0009: Cognition *Don't Build Multi-Agents*; Anthropic
  *multi-agent research system*; *Context Engineering in Manus*; Claude Code
  subagents. (URLs in ADR-0009 References.)
- CHANGELOG: the `[Unreleased]` section's "scheduling refinement (ADR-0008)" and
  "task-scoped context, M1–M4 (ADR-0009)" entries.

---

## 7. Final state

- **628 tests**, ruff + ruff-format + mypy(strict) all green.
- **41 `EventKind` variants** (unchanged — both ADRs reused existing events).
- New structural fields: `Event.task_id`; `Task.framing`, `Task.brief_watermark`;
  `ToolContext.turn_origin`; `AgentRuntime.{pending_interactive, turn_origin,
  on_turn_boundary}`.
- One additive SQLite migration (`events.task_id` column + index), backward
  compatible (old rows = chat scope).
- Working tree is **uncommitted** as of this writing — suggested split: one commit
  for ADR-0008, one for ADR-0009 (or per milestone M1–M4), each independently green.
