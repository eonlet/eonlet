# Design Review Fixes — Problem & Solution List

Date: **2026-06-10**. Author: Ziyu (with Claude).

Findings from a full design review (context organization, memory system,
scheduling) of the post-ADR-0009 codebase. The project is pre-alpha, so these
land as direct fixes without new ADRs; where a fix touches an ADR's stated
model, the ADR's intent is honored (this list closes implementation gaps, it
does not change decisions).

Status legend: `[ ]` open · `[x]` done · `[~]` documented-as-deliberate.

**All fifteen items landed 2026-06-10** (commits: Round A–E on main).

---

## A. Severe — design promises broken in implementation

### P1 `[x]` Task results never flow back into the chat scope

**Problem.** ADR-0009 promises "T completes → result surfaces upward into
chat … and that enters episodic memory naturally". In code, `TASK_TRANSITIONED`
is a bookkeeping event (never enters `state.messages`), the `<tasks>` block
injects pending leaves only, and tier-1 ignores it. The only "surfacing" is a
human-facing CLI line. The model in chat scope never learns a completed root
task's result, and episodic memory never records any task work.

**Fix.** When a **root** task reaches a terminal state inside a scheduler-driven
run (`_run_task`), record a chat-scope `<task_result>` envelope as a
`USER_MESSAGE` (task_id None, no agent run triggered). It enters the chat
window naturally on the next turn and is folded into STM by tier-1. Chat-turn
`task(done)` calls need no envelope (already visible in chat scope).

### P2 `[x]` Crash leaves `active` tasks wedged forever

**Problem.** `_run_task` records `→active` then runs. A crash/SIGKILL mid-run
replays the task as `active`; `next_runnable` only picks pending leaves /
synthesis-ready parents, so the task is never scheduled again and is invisible
in `<tasks>`. No startup reconciliation exists.

**Fix.** On worker startup, transition every `active` task back to `pending`
(`reason="crash_recovery"`). Serial execution guarantees at most one task can
legitimately be active, and the worker is just starting — none is.

### P3 `[x]` Suspended tasks are a black hole

**Problem.** YIELDED (incl. budget-exhausted, and "model forgot `task(done)`")
→ `suspended`; the scheduler never picks suspended; the `<tasks>` block hides
them; the agent has no `resume` action (CLI-only); `max_suspended` silently
cancels. Tasks vanish without anyone being told.

**Fix.** (a) Inject suspended tasks into the `<tasks>` block under a
"suspended (resume to continue)" section; (b) add a `resume` action to the
`task` tool (suspended→pending) so the agent can act on it when the user asks.

---

## B. Memory system — invariants & consistency

### P4 `[x]` Knowledge axis is not event-sourced (I-S1 is false)

**Problem.** `knowledge/` is the "never auto-deleted" curated asset, yet it is
a single on-disk copy: `kb_written` carries only path+size, so the event log —
nominally the single source of truth (Invariant #1) — cannot reconstruct it.
`rm -rf memory/` permanently destroys knowledge.

**Fix.** Carry the full file content in `kb_written` payloads (knowledge files
are small markdown by design). The log then preserves every version; a rebuild
tool can come later. Document the amended invariant.

### P5 `[x]` Chat STM/LTM is injected into task-scoped runs

**Problem.** `handle_user_message` builds the `<memory>` preamble
unconditionally, so every task beat carries the *chat* episodic timeline —
undercutting ADR-0009's "a child gets a compressed decision trace, not the
parent's history" and paying STM/LTM tokens on every task turn. ADR-0009 only
makes the *knowledge* axis global.

**Fix.** During a task-scoped run, inject the knowledge index only; skip
`<long_term>`/`<short_term>` (episodic = chat scope, per the ADR). `recall`
remains the escape hatch.

### P6 `[x]` Knowledge-index overflow has no feedback loop

**Problem.** The index is injected every call; exceeding `index_max_tokens`
only logs a warning the agent never sees. The agent is the only entity that can
prune its index, and it gets no signal.

**Fix.** After `knowledge` write/edit/move, if the index exceeds the budget,
append a visible warning to the ToolResult so the model can curate.

### P7 `[~]` Cron conversations interleave into the chat scope

**Problem.** ADR-0009 scoped task turns, but cron-triggered turns are
`task_id=None` — full cron conversations land mid-chat-window and in STM, the
same cross-talk the ADR fixed for tasks.

**Decision (documented, not coded).** Deliberate for now: scheduled activity
*is* part of the agent's episodic timeline (a purely-cron agent like x-digest
would otherwise have no episodic memory at all). Trigger-scoping can reuse the
same mechanism later if real cross-talk shows up in dogfooding. Noted in
MEMORY_SPEC §3.2.

---

## C. Scheduling & runtime efficiency

### P8 `[x]` Every user message costs an LLM checkpoint of the running task

**Problem.** Any interactive message — even "thanks" — pauses the running task
and triggers a full LLM brief compression before re-queue, then re-dispatch.
Chatty users thrash the task with paid LLM calls.

**Fix.** For *user-input* pauses, use the cheap structural checkpoint directly
(no LLM call, `boundary=None` so no pruning — the raw scoped window survives
intact for the resume). The LLM brief stays for yield/suspend/cross-tree
preemption, where the task may sit for a long time.

### P9 `[x]` 1 Hz idle polling

**Problem.** With scheduling enabled the worker wakes every second forever.
All paths that create runnable work already push a queue item or `task_wake`
sentinel (IPC mutations, hatches) or happen inside the loop itself — the poll
is a belt-and-braces only.

**Fix.** Raise `SCHED_POLL_S` to 30 s (safety net), keep the wake sentinel as
the primary signal.

### P10 `[x]` Cancelling/deleting a running task doesn't stop the run

**Problem.** `pause_check` returns False when the current task is deleted and
doesn't look at terminal states — the run burns tokens to its natural end.

**Fix.** First check in `pause_check`: current task gone or terminal → end the
run immediately; `_run_task` skips checkpoint/transition for that case.

### P11 `[x]` Per-task budget re-reads the whole log every turn

**Problem.** The budget check does `store.read(since=start_id)` and sums at
*every* turn boundary — O(suffix) per turn, on top of the known full-read
patterns in checkpoint/compaction (plan §5.3).

**Fix.** Accumulate incrementally inside the `pause_check` closure (read only
the delta since the last check).

---

## D. Context organization — smaller

### P12 `[x]` `progress_summary`/`framing` double-injected and go stale

**Problem.** On resume, the brief appears both in the framing `USER_MESSAGE`
("Progress so far") and in the system prompt (`<task_progress>`); repeated
pauses accumulate stale framing messages in the scope window that can
contradict the live brief.

**Fix.** Per the plan's refactor guidance: move `framing` ("Context from
above") and progress out of the kickoff message into the system prompt
(rebuilt every turn, hence never stale); keep the kickoff message minimal
(instructions + goal + synthesis child results).

### P13 `[x]` `task(done)` accepts an empty result

**Problem.** The `result` is the *only* payload of the upward flow; an empty
one leaves the parent synthesis (and P1's chat envelope) with "(done)".

**Fix.** When a task-scoped run marks *its own* task done, require a non-empty
`result` (error prompts the model to supply one). Chat-scope `done` (user
ticking items off) stays optional.

### P14 `[x]` `mem_compacted.model` records a class name

**Problem.** Audit field carries `type(compactor).__name__` ("LLMCompactor"),
not the model id.

**Fix.** `LLMCompactor` exposes the provider's model id; tier-1 records it.

---

## E. Documentation

### P15 `[x]` MEMORY_SPEC is partially false

**Problem.** The spec carries a long superseding notice; §5–§11 describe
removed tools (`note`/`remember`/`forget`/`todo`), the old config shape, and a
removed migration tool. "Read the spec first" currently misleads.

**Fix.** Rewrite MEMORY_SPEC to the dual-axis model (ADR-0005) + trigger
matrix (ADR-0006) + scoping (ADR-0009); drop the superseded sections.

---

## Execution order

Grouped so each lands independently green:

1. **Round A — scheduler/worker correctness:** P2, P10, P8, P11
2. **Round B — upward flow & visibility:** P1, P13, P3
3. **Round C — injection & framing:** P5, P12
4. **Round D — knowledge & audit:** P4, P6, P14
5. **Round E — efficiency & docs:** P9, P7 (doc note), P15
