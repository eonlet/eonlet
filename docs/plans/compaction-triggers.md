# Plan — Compaction Triggers (Bounded Auto + User-Forced + Agent-Proposed)

Companion implementation plan for [ADR-0006](../adr/0006-compaction-triggers.md).
Target version: **v0.0.9**.

## Why this plan exists

ADR-0006 keeps the single tier-1 *mechanism* and gives it three *triggers*, each
with its own boundary policy. Most of the change is additive, but two pieces
carry real risk: (1) a **new blocking IPC round-trip** (the worker pausing a
turn to ask the attached user a yes/no, which the runtime has never done), and
(2) a **behavior change** to `/compact` (it now empties the working window).
Sequencing isolates the risky infra so the additive parts land green first.

## Guiding principles

1. **Additive before destructive, infra before consumers.** Boundary modes and
   the clean-slate `/compact` (M1) land before the consent channel (M2), which
   lands before the agent-proposed trigger that depends on it (M3).
2. **One round-trip, two callers.** Per ADR-0006 OQ-resolution (A), the blocking
   `user_decision` channel is built once (M2) and serves both compaction
   proposals *and* the long-deferred interactive permission confirm
   (`permissions/gate.py` has auto-allowed destructive tools since v0.0.2 with a
   "will gate interactively" TODO). M2 closes that debt.
3. **The trigger determines the boundary; the mechanism is unchanged.** tier-1
   summarize-into-STM is not rewritten — only `compute_suggested_boundary` gains
   a "full" mode and the call sites choose a policy.
4. **Event log stays the source of truth.** New events
   (`MEM_COMPACT_PROPOSED` / `_APPROVED` / `_DECLINED`) are append-only; the
   pending proposal is transient runtime state, only the decision is persisted.
   `SESSION_STARTED` / `SESSION_ENDED` are *reused*, not duplicated.
5. **Events are immutable.** The per-message timestamp is a **render-time**
   prefix only; nothing is written back into `USER_MESSAGE` payloads
   (invariant 1 / `Event.ts` already carries the time).
6. **Docs land with the code.** MEMORY_SPEC §4 is normative; it ships in the
   same milestone as the behavior it describes (M4), not trailing.

## Milestone map

```
M1  Boundary modes + user-forced full /compact + session boundary + timestamps  (≈ 1 day)
M2  Generic user_decision consent round-trip (+ retrofit permission confirm)     (≈ 1.5 day)
M3  Agent-proposed semantic compaction (propose_compact, guards, yolo path)      (≈ 1 day)
M4  MEMORY_SPEC §4 rewrite + config/tool/CLI docs + templates + version bump     (≈ 1 day)
```

Four milestones, four PRs.

### Suggested PR shape

```
PR1 (M1) — feat(memory): boundary modes + clean-slate /compact + turn timestamps
  - tier1.py: compute_suggested_boundary gains a "full" mode (boundary = latest_id)
  - worker/main.py + tools/builtin/memory.py: /compact (user) → full compaction
  - full compaction empties working AND emits SESSION_ENDED then SESSION_STARTED
  - injection.py: prefix each USER_MESSAGE with "[YYYY-MM-DD HH:MM ±ZZ]" at render
  - config.py: add inject_turn_timestamps; add inert propose_* fields (wired in M3)
  - Tests: full-boundary cut, working emptied, session events, timestamp render
  - Bounded-auto path unchanged; no IPC touched

PR2 (M2) — feat(worker): blocking user_decision round-trip + interactive permission confirm
  - worker/ipc.py: PendingDecisions registry + decision.respond request handler
  - worker pushes a decision notification; agent task awaits an anyio.Event slot
  - envelope {id, kind, prompt, options, payload}; kind ∈ {"permission","compaction"}
  - cli attach: render a yes/no decision prompt; send decision.respond back
  - permissions/gate.py: ask-mode destructive tool now actually prompts (closes TODO)
  - no-session attached → auto-decline; multi-session → first responder wins
  - Tests: round-trip approve/decline, no-session decline, permission gating live

PR3 (M3) — feat(memory): agent-proposed semantic compaction
  - tools/builtin/memory.py: action "propose_compact"(boundary_event_id, reason)
  - blocks via the M2 channel (kind:"compaction"); approve → tier1 at agent boundary
  - guards: propose_floor_tokens + propose_min_interval_seconds + propose_min_turns
  - yolo → auto-approve, still emit PROPOSED + APPROVED(rule:"yolo")
  - interactive-only: no attached session → no-op refusal
  - events.py: MEM_COMPACT_PROPOSED / _APPROVED / _DECLINED (EventKind 36 → 39)
  - Tests: block+approve+decline, all guards, yolo audit path, cron no-op

PR4 (M4) — docs(memory): normative §4 rewrite + config/tool/CLI/templates + v0.0.9
  - MEMORY_SPEC.md §4: trigger × boundary matrix, consent flow, timestamps
  - AGENT_CONFIG_SPEC.md: memory.episodic.propose_*, inject_turn_timestamps
  - TOOL_SPEC.md: memory(action="propose_compact"); CLI_REFERENCE: /compact full
  - SECURITY.md: the user_decision consent channel + permission confirm now live
  - templates ×3 agent.yaml; CHANGELOG.md; CLAUDE.md version history → v0.0.9
```

---

## M1 — Boundary modes + clean-slate `/compact` + timestamps (≈ 1 day)

### Scope

Purely additive plus one behavior change to `/compact`. No IPC.

- `memory/tier1.py` — `compute_suggested_boundary` (or a thin wrapper) gains a
  **full** mode that returns `store.latest_id()` as the boundary so the entire
  working window is summarized into STM with no tail. Tool-pair safety still
  applies (a no-op at end-of-turn, but keep it).
- `tools/builtin/memory.py` + the `/compact` path — the **user-forced** compact
  runs the full mode and **empties the working window**. The bounded-auto path
  (`worker/main.py:_maybe_run_tier1`) keeps the existing ~30%-tail boundary,
  unchanged.
- Full compaction marks an episode boundary: emit `SESSION_ENDED` then
  `SESSION_STARTED` (reuse existing `EventKind`). The next user message starts a
  new episode carrying only the injected memory preamble + `<tasks>`.
- `memory/injection.py` — prefix **every** `USER_MESSAGE` rendered into the
  working window with its local datetime `[YYYY-MM-DD HH:MM ±ZZ]` from
  `Event.ts`. Render-time only — payloads untouched. Gated by
  `inject_turn_timestamps`.
- `config.py` — add `inject_turn_timestamps: bool = True`. Add the
  `propose_semantic` / `propose_floor_tokens` / `propose_min_interval_seconds` /
  `propose_min_turns` fields now as **inert** config (validated, defaulted, not
  yet consumed); M3 wires them. Keep `extra="forbid"`.

### Done when

- A user-forced `/compact` empties the working window and a subsequent injection
  shows zero raw working events (only preamble); a `SESSION_ENDED`+
  `SESSION_STARTED` pair is recorded.
- Bounded-auto still keeps its tail (existing tier-1 tests unchanged).
- `test_injection.py` covers the timestamp prefix (and its off switch).
- New config fields validate; unknown keys still rejected.
- `mypy src` + `ruff check .` clean. Every pre-existing test passes.

---

## M2 — Generic `user_decision` consent round-trip (≈ 1.5 day)

### Scope

The risky infra, built once for two callers (principle 2).

- `worker/ipc.py` —
  - A `PendingDecisions` registry: `dict[decision_id, slot]` where each slot is
    an `anyio.Event` + a result cell.
  - The worker pushes a decision **notification** (existing `notify` broadcast)
    with envelope `{id, kind, prompt, options, payload}`,
    `kind ∈ {"permission", "compaction"}`.
  - A new JSON-RPC **request** `decision.respond(id, choice)` from an attached
    CLI fills the result cell and `set()`s the event; the paused agent task
    `await`s the slot and resumes.
  - **No session attached → auto-decline** immediately (don't block a headless
    worker). **Multiple sessions → first responder wins**; late responses to a
    resolved id are ignored.
- `cli/` (attach path) — render a decision prompt (`prompt` + `options`, default
  yes/no) and send `decision.respond` with the user's choice. One render path
  for both kinds.
- `permissions/gate.py` + `runtime/agent.py` — retrofit the deferred interactive
  confirm: in `ask` mode, a `destructive` tool with a session attached now
  raises a `kind:"permission"` decision instead of auto-allowing
  (`gate.py:104-110`). Deny → the existing `PERMISSION_DENIED` path; allow →
  `PERMISSION_GRANTED`. This closes the v0.0.2 TODO.

> If the permission retrofit balloons, split it into M2b — the compaction caller
> (M3) only needs the channel, not the permission wiring. Keep the channel
> generic so that split is cheap.

### Done when

- An integration test drives a full round-trip: worker raises a decision, a
  fake attached client answers `decision.respond`, the awaiting task resumes
  with the right outcome.
- No-session → auto-decline path tested; resolved-id late-response ignored.
- A destructive tool in `ask` mode with a session attached is now **actually**
  user-gated (new test; the old "auto-allow" assertion is replaced).
- `mypy src` + `ruff check .` clean; all prior tests pass.

---

## M3 — Agent-proposed semantic compaction (≈ 1 day)

### Scope

The new trigger, consuming the M2 channel.

- `tools/builtin/memory.py` — add action
  `propose_compact(boundary_event_id, reason)`:
  - Validate `boundary_event_id`: a known event id, `≤ latest_id`, not splitting
    a tool_call/tool_result pair (reuse tier-1's pair-safety check).
  - **Guards** (all must pass, else the action returns a "not now" result
    without raising a decision): working tokens ≥ `propose_floor_tokens`; AND
    wall-clock since last compaction ≥ `propose_min_interval_seconds`; AND turns
    since last compaction ≥ `propose_min_turns`.
  - **Interactive-only**: no attached session → no-op refusal (cron/autonomous
    never proposes).
  - Emit `MEM_COMPACT_PROPOSED`, then raise a `kind:"compaction"` decision via
    the M2 channel and **block the turn**.
  - **Approve** → emit `MEM_COMPACT_APPROVED`, run tier-1 at the agent's
    boundary, agent resumes with compacted context. **Decline** → emit
    `MEM_COMPACT_DECLINED`, agent resumes (cooldown now prevents an immediate
    re-propose).
  - **`yolo` mode** → skip the round-trip: emit `MEM_COMPACT_PROPOSED` then
    auto-`MEM_COMPACT_APPROVED` (`rule:"yolo"`) and compact. Guards still apply.
- `runtime/events.py` — add `MEM_COMPACT_PROPOSED` / `MEM_COMPACT_APPROVED` /
  `MEM_COMPACT_DECLINED` (`EventKind` 36 → 39). Track "last compaction time/turn"
  from the event stream for the cooldown guards.
- `config.py` — the `propose_*` fields added inert in M1 are now consumed.

### Done when

- Block→approve runs tier-1 at the agent boundary; block→decline leaves memory
  untouched and resumes.
- Each guard independently blocks a proposal (floor, interval, turns).
- `yolo` auto-approves and records `PROPOSED`+`APPROVED(rule:"yolo")`; guards
  still hold.
- Headless (no session) `propose_compact` is a no-op refusal.
- `EventKind` count updated; replay reconstructs proposal outcomes.
- `mypy src` + `ruff check .` clean; all prior tests pass.

---

## M4 — Normative spec + docs + templates + version (≈ 1 day)

### Scope

- `MEMORY_SPEC.md` §4 — rewrite for the **trigger × boundary** matrix: bounded
  auto (tail), user-forced (full + session boundary), agent-proposed (agent
  boundary, consent-gated, interactive-only, yolo auto-approve). Document the
  consent flow and per-message timestamps.
- `AGENT_CONFIG_SPEC.md` — `memory.episodic.propose_semantic` /
  `propose_floor_tokens` / `propose_min_interval_seconds` / `propose_min_turns`
  and `memory.inject_turn_timestamps`.
- `TOOL_SPEC.md` — `memory(action="propose_compact")`; note the blocking/consent
  semantics. `CLI_REFERENCE.md` — `/compact` now full + session boundary.
- `SECURITY.md` — the generic `user_decision` channel; note the permission
  interactive confirm is now live (no longer auto-allow).
- Templates ×3 (`assistant`, `x-digest`, `portfolio`) — `agent.yaml` gains the
  new `memory.episodic.propose_*` / `inject_turn_timestamps` fields where they
  illustrate the feature.
- `CHANGELOG.md`; `CLAUDE.md` version history → **v0.0.9**; update the
  "`EventKind` settles at N variants" note (36 → 39).

### Done when

- §4 reflects the shipped behavior exactly; no doc trails code.
- Templates load clean against the updated config schema.
- `pytest` green; `mypy src` + `ruff check .` clean.

---

## Cross-cutting checklist (all milestones)

- `from __future__ import annotations`; anyio (never asyncio); structlog/rich;
  no bare `except Exception`; atomic writes for any memory file.
- No event row is ever mutated; proposals/decisions are append-only events.
- Branch coverage stays ≥ 70 (CI gate); new infra (consent round-trip, guards)
  carries its own tests.

## Risks & mitigations

- **Blocking the agent turn mid-flight is new plumbing.** Mitigate: M2 ships the
  round-trip standalone with its own integration test before any consumer; the
  no-session auto-decline keeps a headless worker from hanging.
- **Permission retrofit scope creep.** Mitigate: the channel is generic; the
  permission wiring can split to M2b without blocking M3 (which needs only the
  channel).
- **`/compact` emptying working surprises a user mid-task.** Mitigate: it is
  user-initiated and explicitly the "clean slate" lever; the session boundary
  event makes the reset auditable in `tail`/`replay`.
- **Agent over-proposing.** Mitigate: floor + dual cooldown guards; prompt
  tuning is the remaining lever, acceptable pre-alpha.

## References

- [ADR-0006](../adr/0006-compaction-triggers.md) — the decision this plan sequences
- [ADR-0005](../adr/0005-dual-axis-memory.md) / [plans/dual-axis-memory.md](dual-axis-memory.md) — the episodic axis + plan format this follows
- `memory/tier1.py` — `compute_suggested_boundary` (gains full mode)
- `worker/ipc.py` — notification channel the round-trip extends
- `permissions/gate.py` — the deferred interactive confirm M2 finally builds

## Update history

- 2026-05-30: Initial plan, paired with ADR-0006 acceptance.
- 2026-05-30: M1–M4 implemented (v0.0.9). Three triggers, blocking decision
  channel (+ interactive permission confirm), agent-proposed semantic
  compaction, per-turn timestamps; docs/specs/templates updated. 552 tests,
  ruff + mypy clean.
