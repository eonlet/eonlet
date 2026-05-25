# ADR-0002: Dynamic Triggers — In-Conversation Schedule Management

| Field | Value |
|---|---|
| Status | Accepted (shipped in v0.0.2) |
| Date | 2026-05-18 |
| Deciders | Ziyu |
| Supersedes | – |
| Superseded by | – |

## Context

Today, an eonlet's cron triggers are declared statically in `agent.yaml` and
loaded once at worker startup (see `triggers/scheduler.py` and the `x-digest`
template). The user wants the agent to also create, list, enable/disable, and
remove triggers **from inside a conversation** — e.g. "remind me to review the
portfolio every weekday at 9am with prompt P".

Constraints we want to preserve:

1. **"Config file is the definition."** `agent.yaml` declares the eonlet's
   identity — what it ships with on day zero. Hot-mutating it from the loop
   would break that contract, mix machine-managed and human-managed state in
   one file, and create write-conflict risk against user edits.
2. **Static triggers are not user-removable.** Triggers declared in
   `agent.yaml` are part of the agent's identity (the way `x-digest`'s morning
   digest is part of *being* x-digest). They can be toggled enabled/disabled at
   runtime but cannot be deleted via the dynamic API.
3. **Dynamic triggers must be flexible.** They bind an arbitrary prompt (the
   message the agent receives when the trigger fires), not just one of the
   prompts already declared in `agent.yaml`.

## Decision

Introduce **dynamic triggers** as a separate, runtime-mutable layer on top of
the static triggers from `agent.yaml`.

### Storage

Persist dynamic triggers in a new JSON file per eonlet:

```
~/.eonlet/eonlets/<eonlet_id>/dynamic_triggers.json
```

Shape:

```json
{
  "version": 1,
  "triggers": [
    {
      "id": "dyn-2026-05-18-a1b2",
      "schedule": "0 9 * * 1-5",
      "timezone": "Asia/Shanghai",
      "message": "Review the portfolio and post the morning brief.",
      "enabled": true,
      "created_at": "2026-05-18T09:30:00+08:00",
      "created_by": "agent"
    }
  ]
}
```

Reasons for JSON-on-disk (vs. event log, vs. `agent.yaml` merge):

- **Atomic, mutable, human-inspectable.** The agent edits it; the user can
  `cat`/`vim` it; failure modes are obvious.
- **Survives restart** without replaying events. We *also* append `Event`s
  (`dyn_trigger_added/removed/toggled`) to the existing SQLite store so the
  event timeline stays complete, but the JSON file is the source of truth for
  "what's scheduled right now."
- **Doesn't touch `agent.yaml`.** Definition stays pure.

Concurrency: writes go through a per-eonlet `anyio.Lock` held by the runtime
and use the atomic write-temp-then-rename pattern. The worker is the only
writer (CLI commands route through IPC).

### Identity & namespace

- Dynamic IDs: `dyn-<YYYY-MM-DD>-<4-char-suffix>`. The `dyn-` prefix prevents
  collision with static IDs and lets the scheduler tell them apart at a glance.
- Static IDs MUST NOT start with `dyn-` (validated at config load).

### Scheduler integration

`CronScheduler` is extended to hold two lists of `_Scheduled` items: `static`
(today's behavior, fed by `definition.config.triggers`) and `dynamic` (fed by
the new `DynamicTriggerStore`). The firing loop is unchanged — it sees the
union. New methods:

- `add_dynamic(trig) -> None` — install at runtime, persist, emit event.
- `remove_dynamic(trigger_id) -> bool` — refuses static IDs.
- `set_enabled(trigger_id, enabled) -> bool` — works on both static and
  dynamic (static toggle is in-memory only and resets on worker restart;
  dynamic toggle is persisted).
- `clear_dynamic() -> int` — drops all dynamic triggers, returns count.

Static triggers can be disabled by name in-process, but their persisted
enabled/disabled state lives only in `agent.yaml`. Restarting the worker
re-reads `agent.yaml` and any runtime disable is lost — by design, since the
config file is canonical for static state.

### Agent-facing surface: the `schedule` tool

Single builtin tool with an `action` discriminator, mirroring patterns the LLM
already handles well (`bash` action-style, `notes_*` family). One tool with
five actions beats five tools with overlapping names.

Actions:

| action | purpose | mutates persisted state |
|---|---|---|
| `list` | return all triggers (static + dynamic) with status | no |
| `add` | register a new dynamic trigger | yes |
| `remove` | drop a dynamic trigger by id (errors on static id) | yes |
| `set_enabled` | enable/disable a trigger by id | dynamic: yes; static: no |
| `clear` | remove all dynamic triggers | yes |

Schema (Pydantic discriminated union by `action`, see skeleton).

### Permissions

The `schedule` tool is marked `destructive=True` for mutating actions, so the
`ask` permission mode prompts before each mutation; `list` is `read_only=True`.
In `yolo` mode the agent can self-schedule freely — same as for `notes_append`.

### Reserved-prompt safety

The `message` field accepts arbitrary text, but the runtime refuses any string
that **starts with** `<trigger ` — that prefix is reserved for the envelope
`build_trigger_message` constructs (TRIGGER_SPEC §2.3). Refusing it prevents
the agent from forging fake "this is a system trigger" framing on itself.

### Rate / quota

Cap dynamic triggers per eonlet at **64** (matches the spirit of the existing
16-slot trigger queue without being so tight that an agent runs out doing
normal reminder work). Returning an explicit `is_error` `ToolResult` when full
is friendlier to the LLM than silent drop.

## Consequences

### Positive

- Conversational ergonomics: "remind me every Monday at 9" Just Works.
- Static triggers (the agent's identity) remain protected.
- `agent.yaml` is not hot-written; no merge conflicts with user edits.
- Existing `triggers.list` IPC and `eonlet trigger` CLI commands surface both
  kinds for free once the union is wired in `serializable()`.

### Negative

- State now lives in two places (`agent.yaml` + `dynamic_triggers.json`). The
  CLI must learn both when answering "what's scheduled?".
- `eonlet export`/`import` (v0.0.3) must include the JSON file in the bundle.
- Restoring an eonlet on a different machine carries its dynamic triggers
  along — generally desirable, but worth documenting.

### Neutral

- Backoff and queue-full semantics are identical to static triggers; the
  scheduler doesn't care which list a `_Scheduled` came from.

## Alternatives Considered

### A. Hot-write `agent.yaml`

Rejected: breaks "config-as-definition", creates write conflicts with user
edits, requires a YAML round-trip that loses comments, and conflates
machine-managed with human-managed config.

### B. Store dynamic triggers only in the event log, rebuild on startup

Rejected: the event log is append-only and intentionally not optimized for
"what is the current set of dynamic triggers?" queries. A separate state file
that the event log *also* records mutations against is the standard
event-sourcing snapshot pattern and matches how `trigger_state` already works.

### C. One tool per action (`schedule_add`, `schedule_remove`, …)

Rejected: more tool names in the LLM context for no expressive gain. The
`schedule` umbrella with a discriminated `action` keeps the tool catalog tight
and lets us extend with new actions (`pause_until`, `snooze`) without churning
the registered tool list.

## References

- `docs/TRIGGER_SPEC.md` — trigger envelope, grace period, run-rate floor
- `docs/TOOL_SPEC.md` — tool protocol that `schedule` implements
- `src/eonlet/triggers/scheduler.py` — extension points
- `src/eonlet/tools/builtin/schedule.py` — skeleton

## Update history

- 2026-05-18: Initial proposal (recurring dynamic triggers).
- 2026-05-19: Add one-shot timers. `OnceTrigger` lives alongside `CronTrigger`
  in the same JSON store under an `once: [...]` section. `_Scheduled` carries
  a `once: bool` flag; after firing, the scheduler removes the entry from both
  in-memory list and persistent store. `catch_up_missed` fires past-due
  one-shots within `grace_period` (consuming them) and skips/removes those
  beyond grace. New tool action `add_once` (with `fire_at` ISO datetime or
  `in` relative duration), IPC method `triggers.add_once`, and slash commands
  `/trigger once <ISO+tz> <tz> <msg…>` and `/trigger in <duration> <tz> <msg…>`.
  IDs share the `dyn-` namespace across both kinds; the cap (64) covers their
  sum. One-shots are never static — the use case doesn't exist in `agent.yaml`.
