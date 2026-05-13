# Trigger Specification

> Triggers are how eonlets *start doing things*. Without triggers, an eonlet is just a process waiting for a user to attach. With triggers, eonlets become autonomous: they wake on schedule, react to events, do work while you're asleep.

This document specifies the trigger system: how triggers are configured, what happens when one fires, how the agent recognizes triggered runs, and how the framework handles edge cases like missed fires, overlapping runs, and failures.

## 1. Conceptual Model

An eonlet has a **trigger queue** that the main loop reads. Items in the queue can come from:

- The **cron scheduler** (MVP): fires triggers on their cron schedule
- **User messages** (MVP, implicit): each user message becomes an "interactive trigger"
- **Webhooks** (v0.2): HTTP POST appends to the queue
- **File watchers** (v0.2): filesystem changes
- **Peer messages** (v0.4): other eonlets

The main loop processes one trigger at a time. It does not interleave — if a cron trigger fires while a user message is being handled, it waits in the queue.

```
   ┌─────────────────┐
   │ Cron scheduler  │ ── puts ──►┐
   └─────────────────┘            │
                                  ▼
   ┌─────────────────┐         ┌────────┐         ┌─────────────┐
   │ Runtime socket  │ ── puts ► Queue  │ ── reads► Main loop   │
   │  (user msgs)    │         └────────┘         │  (one LLM   │
   └─────────────────┘                            │   run / sec)│
                                                  └─────────────┘
```

## 2. The Three Trigger Kinds (MVP)

### 2.1 `interactive` — implicit

Every eonlet has an implicit interactive trigger. The user attaches via `eonlet attach <id>` or `eonlet send <id> "..."` and types a message. That message goes onto the trigger queue with `kind: "interactive"`.

The agent receives a normal user-role message, no special framing.

### 2.2 `cron` — declared

Declared in `agent.yaml`:

```yaml
triggers:
  - id: morning_digest
    kind: cron
    schedule: "0 8 * * *"
    timezone: Asia/Tokyo
    message: |
      Time for the morning digest. Fetch X tweets from people I follow
      since your last successful run, group by theme, and email me.
    grace_period: 1h
    enabled: true
```

Fields:

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | yes | Unique within the agent; used in events and CLI |
| `kind` | enum | yes | `cron` for this section |
| `schedule` | string | yes | 5-field cron expression or `@hourly`/`@daily`/`@weekly`/`@monthly` |
| `timezone` | string | yes | IANA timezone (e.g. `Asia/Tokyo`, `America/New_York`, `UTC`) |
| `message` | string | yes | Injected as the trigger message; what the agent sees |
| `grace_period` | duration | no | If missed by less than this, fire on startup. Default: `1h` |
| `enabled` | bool | no | If false, don't fire. Default: `true` |
| `max_concurrent_runs` | int | no | Default `1`. If a previous run is still active, new fires queue (up to `max_queued`) or drop |
| `max_queued` | int | no | Default `1`. Limits backlog if runs are slow |

### 2.3 Trigger event injection

When a `cron` trigger fires, the worker:

1. Records a `trigger_fired` event with the trigger's `id` and `fired_at`.
2. Looks up `last_success_at` from `trigger_state` table.
3. Injects a user-role message of this form into the conversation:

```
<trigger kind="cron" id="morning_digest" fired_at="2026-05-12T08:00:00+09:00">
  Previous successful run: 2026-05-11T08:01:23+09:00
  Time since last run: 23h 58m 37s
  
  Time for the morning digest. Fetch X tweets from people I follow
  since your last successful run, group by theme, and email me.
</trigger>
```

The agent's `system.md` is written to recognize this tag and execute accordingly. The XML-style tag is recognized as untrusted-by-default by Eonlet, but trigger payloads come from the user's own config so they're treated as trusted input.

The `message` field can use template variables (resolved at injection time):

| Variable | Substitution |
|---|---|
| `{{fired_at}}` | Full ISO 8601 timestamp with timezone (e.g. `2026-05-12T08:00:00+09:00`) |
| `{{fired_at_date}}` | Date portion only, in trigger's timezone (e.g. `2026-05-12`) |
| `{{fired_at_time}}` | Time portion only, HH:MM (e.g. `08:00`) |
| `{{last_success_at}}` | ISO 8601 or "never" |
| `{{since_last_run}}` | Human duration ("23h 58m") |
| `{{trigger_id}}` | The trigger's id |
| `{{eonlet_id}}` | The eonlet's id |

## 3. Lifecycle: From Cron to Completion

```
T-1  Worker scheduler: croniter.get_next() returns time T
T    Scheduler: anyio.sleep_until(T) wakes
     Scheduler: emit event(trigger_fired, trigger_id, fired_at=T)
     Scheduler: enqueue trigger message
     Scheduler: update trigger_state.last_fired_at = T

T+ε  Main loop: dequeues the message
     Main loop: enters a "triggered run" — sets ctx.trigger_context
     Main loop: agent processes the message
     ... (LLM calls, tool calls, etc.) ...
     
T+N  Main loop: agent emits assistant_message with stop_reason=end_turn
     Main loop: emit event(trigger_completed, trigger_id, success=true)
     Main loop: update trigger_state.last_success_at = now
     Main loop: returns to waiting on trigger queue
```

## 4. Edge Cases

### 4.1 Missed fires

If the worker was hibernated/dead when a cron fire should have occurred, behavior depends on `grace_period`:

- **Within grace period** (e.g., fire was 30 minutes ago, grace_period is 1h): fire once on startup with `fired_at` set to *now* (not the original time), and the message includes a note like "(catching up after downtime)".
- **Beyond grace period**: skip silently and log `trigger_skipped` event. The next scheduled fire is honored.

This prevents:
- Thundering herd of catch-up fires after long downtime
- But also prevents silently missing a fire that's just barely missed

### 4.2 Overlapping runs

By default, a trigger has `max_concurrent_runs: 1`. If a previous run is still active when the next fire comes:

- If queue has slots (`max_queued > 0`): enqueue the new fire
- If queue is full: emit `trigger_dropped` event, skip

This is critical for slow scheduled agents (e.g., portfolio analysis that takes longer than the trigger interval).

### 4.3 Trigger during interactive session

A user is `attached` and chatting. The cron schedule says it's time to fire. What happens?

**MVP behavior:** the trigger waits in the queue until the user's current turn ends. As soon as the agent's response to the user finishes, the trigger fires.

This avoids interrupting an interactive session but means scheduled work may be delayed by long chats. Acceptable for v0.1.

The attached session does see notifications: `trigger_fired` events stream through, so the user knows what's happening.

### 4.4 Failed runs

If the triggered run errors (uncaught exception, budget exceeded, tool failure that propagates up):

- Emit `trigger_failed` event
- Update `trigger_state.consecutive_failures += 1`
- Apply backoff (see below)
- The trigger continues firing on schedule

### 4.5 Backoff on repeated failure

If `consecutive_failures >= 3`:

- Skip the next `consecutive_failures - 2` fires (exponential-ish backoff)
- Each skip emits `trigger_skipped` with reason `"backoff_after_failures"`
- On the next successful run, reset `consecutive_failures = 0`

This prevents a broken trigger from spamming logs and burning budget. The user sees clear events and can `eonlet logs` to investigate.

## 5. Manual Trigger Firing

For testing, the user can manually fire a configured trigger:

```bash
eonlet fire <id> <trigger_id> [-m "override message"]
```

Or from inside an attached session:

```
/fire morning_digest
```

The trigger fires immediately, bypassing schedule. The injected message is the trigger's configured `message` (or the override if `-m` was used).

This is **the** workflow for developing scheduled agents — you don't wait until 8am to test the digest.

## 6. Trigger State Table

Persisted to `state.db`:

```sql
CREATE TABLE trigger_state (
    trigger_id            TEXT PRIMARY KEY,
    last_fired_at         INTEGER,
    last_success_at       INTEGER,
    last_failure_at       INTEGER,
    consecutive_failures  INTEGER DEFAULT 0,
    total_fires           INTEGER DEFAULT 0,
    total_successes       INTEGER DEFAULT 0
);
```

The agent can introspect this via the `trigger_state` builtin (or via the `state.get` runtime RPC). Users see it via `/triggers` in attached session.

## 7. How to Write a System Prompt for a Scheduled Agent

The agent's `system.md` should explicitly handle trigger events. Standard pattern:

```markdown
# Identity
You are the daily X digest agent...

# When a trigger fires
When you receive a `<trigger>` block, switch into autonomous mode:

1. Note the trigger ID and "last successful run" timestamp.
2. Execute the work described in the trigger message.
3. Emit no chit-chat — the user isn't here.
4. When done, write a one-line summary to `last_run.md`.
5. If the work fails partway, write what you completed to a partial file
   and explain what's left in `last_run.md`.

# When a user message comes in
If you receive a normal user message (no `<trigger>` block), this means
the user has attached and is asking you something interactively. Behave
conversationally.
```

This dual mode — autonomous when triggered, conversational when chatted with — is the core idiom for scheduled agents in Eonlet.

## 8. CLI Visibility

`eonlet ps` shows next trigger fire time:

```
ID                 PID    STATUS   NEXT TRIGGER
x-digest.morning   12347  running  morning_digest in 23h 12m
portfolio.main     12349  running  market_close in 6h 30m
```

`eonlet inspect <id>` includes full trigger state.

`/triggers` slash command (in attach mode) lists triggers with their last/next fire times and failure counts.

## 9. Limits

MVP per-eonlet limits:

- Max 10 triggers per agent
- Cron expression must fire no more than once per minute (no `* * * * *`)
- Trigger queue max size: 16 (further fires dropped if queue full)
- Per-trigger run time: defaults to `runtime.max_wall_clock_per_run`

These are not strict architecture limits — just sanity bounds for v0.1. Future versions may relax.

## 10. v0.2+ Trigger Kinds (Preview)

```yaml
# v0.2: webhook
triggers:
  - id: github_pr_event
    kind: webhook
    path: /pr-event              # eonlet exposes localhost:<port>/triggers/<id>/<path>
    method: POST
    secret_env: GITHUB_WEBHOOK_SECRET   # validates X-Hub-Signature
    message: |
      A PR event was received. The payload is in <payload>.

# v0.2: file watch
triggers:
  - id: new_research_paper
    kind: file_watch
    path: ~/Downloads
    pattern: "*.pdf"
    debounce: 5s
    message: |
      A new PDF appeared in Downloads. Path: {{path}}.
      Analyze it and add to my research notes.

# v0.4: peer message
triggers:
  - id: from_planner
    kind: peer_message
    from: planner.*
    message: |
      The planner agent sent you a task: <payload>
```

All future kinds follow the same lifecycle: enqueue, inject trigger message, run, emit completion event.

---

> Triggers are what make Eonlet a runtime, not a chat client. Get them right and the system feels alive.
