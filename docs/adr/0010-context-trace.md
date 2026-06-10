# ADR-0010: Context Trace — Lineage-Aware Recording of Every LLM Request

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-06-10 |
| Accepted | 2026-06-10 |
| Deciders | Ziyu |
| Supersedes | – (observability sibling of SPEC §8.1 token-delta rule; reads the context produced under [ADR-0006](0006-compaction-triggers.md) compaction and [ADR-0009](0009-task-scoped-context.md) scoped windows) |
| Superseded by | – |

## Context

Debugging a stateful agent means answering one question over and over: **"what
exactly did the model see when it produced this?"** Today that is unanswerable
without reconstruction. The event log stores conversation *events*, but the
request actually sent to the provider is a derived artifact — `_build_llm_messages`
slices a token-budgeted window under a compaction watermark, scopes it per task
(ADR-0009), stamps timestamps, and coalesces user turns; `_build_system_prompt`
splices in the memory preamble, `<tasks>` block, and per-task framing/progress.
Replaying events tells you what *happened*; it does not tell you what the model
was *shown* — especially across a compaction, where the window visibly rewrites.

Tools like `claude-trace` solve this for Claude Code by recording every HTTP
request/response pair. A naive port (full request snapshot per call) is wasteful
and, worse, structureless: an agent turn loop re-sends an almost-identical,
strictly-growing context every step, and the interesting moments — compaction,
window slides, `/compact`, task-scope switches — are invisible deltas between
two multi-thousand-line snapshots.

The structural insight: a sequence of LLM requests forms **lines**. Within a
run, each request's message list is a *prefix-extension* of the previous one
(same context, new turns appended). When the context is rewritten — episodic
compaction moved the watermark, the working window slid, a task-scoped run
swapped scopes — the prefix property breaks, and a **new line begins, descending
from the old one**. Recording deltas along a line and full snapshots only at
line roots makes the trace both cheap and *legible*: the fork points ARE the
compactions.

## Decision

A new `src/eonlet/trace/` package records every LLM request at the single choke
point all requests pass through (`AgentRuntime._stream_one_turn`), into a
per-agent append-only JSONL file:

```
~/.eonlet/eonlets/<id>/trace/context.jsonl
```

### 1. Not events

Trace records are **observability data, not state** — the same ruling as
SPEC §8.1 token deltas. They never enter the SQLite event store (full contexts
would bloat it with material wholly derivable in real time, and replay must
never depend on them). Tracing is best-effort: a trace failure logs and never
breaks the agent loop. Deleting `trace/` is always safe.

### 2. Lineage model

The tracer keeps the fingerprint sequence of the last recorded context — one
16-hex SHA-256 fingerprint per provider-neutral message. On each request:

- **Prefix-extension** (`old fingerprints == new[:len(old)]`) → **delta
  record** on the current line: only the appended suffix messages are stored.
- **Anything else** (shorter, or diverging at any index) → **root record** of a
  fresh line: full message snapshot, plus a `parent: {line, seq}` pointer at
  the record where the old line left off. This catches every rewrite cause —
  tier-1 compaction, `/compact`, working-window slide, task-scope switch,
  worker restart with a different window.

The **system prompt is versioned within a line, not part of lineage**: it is
rebuilt every turn by design (`<task_progress>` mutates mid-run), so treating
it as prefix-breaking would fork a line per turn. Each record carries
`system_hash`; the full text is stored only when the hash differs from the
previous record on the same line.

Each request is followed by a lightweight **`response` record** carrying the
assistant reply. Along a line, turn N's reply does reappear in record N+1's
delta — but the *final* reply of a run never would, so without response
records the most interesting message of every run is invisible. Response
records carry **no lineage state**: they never enter the prefix check (so a
reply can never cause a fork), and readers dedupe the reply out of the
following delta by its `hash`. Request recording still happens *before* the
provider call, so the context survives even a crash mid-call; the response is
appended when the stream completes.

### 3. Record schema (one JSON object per line)

| Field | Type | Meaning |
|---|---|---|
| `seq` | int | Global per-agent monotonic call counter (1-based) |
| `ts` | str | ISO-8601 UTC timestamp of the request |
| `line` | str | Line id — `ln-<YYYY-MM-DD>-<4hex>` (ADR-0002 id shape) |
| `parent` | `{line, seq}` \| null | Fork origin; null on the very first line |
| `kind` | `"root"` \| `"delta"` \| `"response"` | Full snapshot vs appended suffix vs reply (see below) |
| `model` | str | Provider model string |
| `task_id` | str \| null | Scope of the run (ADR-0009), for filtering |
| `n_messages` | int | Total messages in the full context of this request |
| `system_hash` | str | Fingerprint of the system prompt |
| `system` | str \| null | Full system prompt iff changed on this line |
| `tools_hash` | str | Fingerprint of the tool-spec list (bodies not stored) |
| `messages` | list | Serialized `LLMMessage`s — all (root) or suffix (delta) |
| `hashes` | list[str] | Fingerprints of exactly the messages in this record |

`response` records are smaller:
`{seq, ts, line, kind: "response", for_seq, message, hash}` — `for_seq` points
at the request answered, `message` is the serialized assistant reply, and
`hash` is its fingerprint (equal to the same message's hash in the following
delta, which is how readers dedupe). They share the global `seq` counter but
never enter the context fold.

A line's full context at `seq` N = concatenation of its root record's messages
plus every delta on the line up to N. Restart restore folds exactly this from
the existing file (anchoring on the last *request* record); corrupt trailing
lines (crash mid-append) are skipped with a warning and the next record simply
roots a new line.

### 4. Configuration & surfaces

`agent.yaml` gains a top-level block (AGENT_CONFIG_SPEC):

```yaml
trace:
  enabled: true        # default false — opt-in, it writes unboundedly
```

CLI gains an offline reader (no worker required, like `replay`):

```
eonlet trace <id>              # lineage tree: lines, forks, call counts, spans
eonlet trace <id> --line <ln>  # fold one line and print its latest full context
eonlet trace <id> --json       # raw JSONL passthrough
eonlet trace <id> --html PATH  # self-contained HTML viewer (embedded data +
                               # vanilla JS/CSS; no deps, no server)
```

The HTML export is the human-scale reader (`trace/html.py`): reading
multi-thousand-token contexts, comparing across forks, and scanning
system-prompt changes outgrow the terminal quickly. Per line it renders
system-prompt versions in their own section (one collapsible entry per
change), the conversation with each call's reply inline (deduped against the
next delta by hash), and tool results nested under the tool call they answer.
Embedding is breakout-safe (`</` → `<\/` in the JSON payload; all record data
rendered via ``textContent``, never as HTML).

## Alternatives considered

- **Record full request per call (claude-trace style).** Simple, but O(context²)
  disk growth per run and no structure: compactions invisible. Rejected.
- **Store traces in the event store.** Violates the derived-data boundary;
  bloats replay; tempts state to depend on observability. Rejected (same logic
  that keeps token deltas out, SPEC §8.1).
- **Hook at the provider layer (`LLMProvider` wrapper).** Catches retries/
  fallbacks too, but each provider serializes differently, and the runtime
  choke point already sees the provider-neutral request — which is the level
  the lineage semantics live at. Rejected for v1; a provider-level wire tap can
  layer on later without schema change.
- **Content-addressed message store (dedupe across lines).** Roots duplicate
  the shared prefix of their parent line. Accepted cost: forks are rare
  (per-compaction, not per-turn), and self-contained roots keep the reader
  trivial. Revisit only if dogfooding shows real disk pressure.

## Consequences

- "What did the model see" becomes a file you can read; compactions and window
  slides become visible fork points with before/after on either side.
- Disk: deltas are ~the size of new turns (cheap); each fork costs one full
  snapshot. No rotation in v1 — `trace/` is deletable at any time; a size cap
  can be added to the config block without breaking the format.
- One new write per LLM call on the hot path — a single buffered append,
  consistent with the synchronous SQLite append already in `_record`.
- No new `EventKind`, no event-store migration, no behavior change when
  disabled (the default).
