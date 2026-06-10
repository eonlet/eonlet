# Memory Subsystem — Specification

| Field | Value |
|---|---|
| Status | Active |
| Spec version | 0.2.0 |
| Depends on | `SPEC.md`, `AGENT_CONFIG_SPEC.md`, `TOOL_SPEC.md`, `TRIGGER_SPEC.md`, `TASK_SPEC.md` |
| Implements | ADR-0003 (base), ADR-0005 (dual axis), ADR-0006 (compaction triggers), ADR-0009 (task scoping) |

## 0. Reader Guide

This spec is the **normative reference** for the memory subsystem as shipped.
The ADRs record the *decisions*; this spec records the *contract* the
implementation satisfies. When the two disagree, this spec wins (and the ADR
should be amended).

Spec version 0.2.0 is a full rewrite to the **dual-axis model** (ADR-0005):
the 0.1.0 document described notes/todos/`remember`/`forget` surfaces that no
longer exist.

Audience: implementers of `src/eonlet/memory/`, the related builtin tools,
the runtime injection point, and the CLI slash commands.

---

## 1. Vocabulary

| Term | Definition |
|---|---|
| **Working memory** | Raw conversation events not yet compacted; the runtime renders them verbatim into the LLM call. Sliding window. |
| **Episodic axis** | The conversation timeline: working memory → STM → LTM. Subject to compaction and forgetting — it *decays*, which is correct for a timeline. |
| **Short-term memory (STM)** | `short_term.md`: dated, compressed conversation summaries scoped to one eonlet. |
| **Long-term memory (LTM)** | `long_term.md`: dated episodic summaries promoted from STM. Holds **only** the `episodic` category. |
| **Knowledge axis** | `knowledge/`: durable facts/rules/preferences the agent curates deliberately via the `knowledge` tool. **Never auto-deleted.** |
| **Knowledge index** | `knowledge/index.md`: the agent-curated map of the knowledge tree. Injected whole into every call; file bodies are opened on demand. |
| **Recall** | Tool-driven FTS5 retrieval over the raw event log and the memory documents. |
| **Compaction** | LLM-driven summarization along working → STM → LTM. |
| **Forgetting** | Compaction restricted to LTM itself when LTM exceeds its budget. |
| **Chat scope** | Events with `task_id = None` — the user conversation plus cron turns (see §3.2). Episodic memory operates on this scope only. |
| **Task scope** | Events with `task_id` set — a task run's own turns (ADR-0009). Never promoted to STM/LTM. |

Tasks are **not** memory. They are event-sourced workflow state in
`src/eonlet/tasks/` (see `TASK_SPEC.md`); this spec covers only how the
`<tasks>` block is injected (§3.1) and how task scoping interacts with
compaction (§3.2, §4).

---

## 2. Storage Layout

Per eonlet, under `~/.eonlet/eonlets/<eonlet_id>/memory/`:

```
memory/
├── short_term.md       # episodic STM — dated sections
├── long_term.md        # episodic LTM — dated summaries only
├── knowledge/          # AXIS 2 — curated, hierarchical, never auto-deleted
│   ├── index.md        #   the agent-curated map; injected whole every call
│   └── …               #   one markdown file per topic (e.g. user.md, rules/…)
├── index.sqlite        # recall FTS5 index over event log + memory docs
└── watermark           # chat-scope compaction watermark (single integer)
```

Invariants:

- **I-S1 (Reconstructability).** Every file in `memory/` is recoverable from
  the event store. STM/LTM are *re-derivable*: deleting them resets the
  watermark path to 0 and the raw history replays into the window (content can
  be re-summarized, not byte-identically rebuilt). The **knowledge axis is
  reconstructable exactly**: every `kb_written` event carries the resulting
  full file body (§7), so the latest version of each file can be replayed from
  the log. The recall index rebuilds automatically (§2.4).
- **I-S2.** The worker is the only writer. CLI slash commands modify these
  files only by routing through the worker over IPC.
- **I-S3.** All writes use the atomic `write-temp-then-rename` pattern
  (`storage.atomic_write_text`). A half-written file MUST NOT be observable.
- **I-S4.** Writes are serialized per file by `anyio.Lock` — recall reads do
  not block LTM writes.

### 2.1 `short_term.md` format

A sequence of **sections**, each delimited by a level-2 header:

```markdown
## [2026-05-22T14:00:00+08:00 – 2026-05-22T15:30:00+08:00] portfolio rebalancing
[topics: portfolio, rebalancing, AAPL]

Discussed Q1 portfolio drift; agent identified AAPL overweight and proposed
a 3% trim. User approved; trade scheduled via /trigger once.
```

Section grammar (regex shape, not BNF):

```
section   := header topic_line? blank_line body
header    := "## [" iso_ts " – " iso_ts "] " topic "\n"
topic_line:= "[topics: " comma_separated_keywords "]\n"
body      := one or more lines, terminated by EOF or next "## ["
```

Headers are machine-parseable; the body is free text intended for the LLM.
`topics:` is the keyword list the recall index uses.

### 2.2 `long_term.md` format

LTM holds **only the `episodic` category** (ADR-0005 M2): dated, roughly
chronological summaries promoted from STM.

```markdown
# Long-term memory

## episodic
- 2026-05-22: spent the morning on the portfolio rebalance flow; ended with AAPL trim approved [src:implicit, ts:2026-05-23]
```

The five semantic categories of the 0.1.0 spec (`user`/`feedback`/`project`/
`reference`/`fact`) live in the knowledge axis now. Tier-3 forgetting applies
uniformly to LTM bullets — there is **no** `src:explicit` exemption.

### 2.3 `knowledge/` format

A tree of small markdown files, one topic per file, plus the `index.md` map.
Each index entry is one line: a markdown link plus a relevance hook, e.g.

```markdown
- [Testing](rules/testing.md) — never mock the DB in tests
```

Written exclusively through the `knowledge` tool (§5.2) / `memory.knowledge.*`
IPC. The runtime **never** deletes, compacts, or rewrites knowledge files on
its own. Paths are validated (no traversal outside `knowledge/`).

### 2.4 `index.sqlite` schema

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
  content, role, kind,
  content='', tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS msg_meta (
  event_id   INTEGER PRIMARY KEY,
  ts         INTEGER NOT NULL,         -- microseconds since epoch
  role       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  fts_rowid  INTEGER NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  doc, section_id, content,
  content='', tokenize='unicode61 remove_diacritics 2'
);
```

- `msg_fts` indexes raw events (one row per text-bearing event) — **all
  scopes**, task turns included; recall is the escape hatch back into pruned
  task history (ADR-0009).
- `memory_fts` indexes sections of the memory documents.
- The index is **derived state**: if missing, corrupt, or lagging the event
  log, the worker catches up / rebuilds at startup without blocking the loop.

---

## 3. Context Injection Pipeline

When the runtime builds an LLM request, it produces:

```
[ system prompt = system.md + skills block + <memory> preamble + <tasks> block
                  (+ <task_context>/<task_progress> during a task run) ]
[ recent_messages_window (raw events, oldest→newest, scope-filtered) ]
```

### 3.1 System prompt assembly

The system message is the concatenation, in order, of:

1. `definition.system_prompt` (from `system.md`)
2. Skills block
3. **Memory preamble** — built once per run, sub-blocks omitted when empty:

   ```
   <memory>
   <knowledge_index>
   {knowledge/index.md contents — the map; bodies never injected}
   </knowledge_index>

   <long_term>
   {long_term.md contents}
   </long_term>

   <short_term>
   {short_term.md contents}
   </short_term>
   </memory>
   ```

   **Scope rule (ADR-0009 §5):** during a **task-scoped run** only
   `<knowledge_index>` is injected. The knowledge axis is global; STM/LTM are
   the *chat* timeline — a task gets its context from its down-tree trace
   (`<task_context>`), its resume brief (`<task_progress>`), and its own scope
   window, not the whole conversation history.

4. **`<tasks>` block** — a sibling of `<memory>`, never nested inside it
   (tasks are workflow state, not memory). Lists pending **leaves**
   (actionable items, highest priority first) plus a `suspended` section —
   suspended tasks only resume by an explicit `resume`, so hiding them would
   make yielded work silently vanish.

5. During a task-scoped run: `<task_context>` (the down-tree decision trace)
   and `<task_progress>` (the cumulative resume brief). Both live **here**, not
   in the kickoff message, because the system prompt is rebuilt every turn —
   they stay present and current as the window prunes, with no stale copies
   accumulating as messages. See `TASK_SPEC.md` §4.

If the knowledge index exceeds `knowledge.index_max_tokens` it is still
injected (never silently truncated), but the `knowledge` tool appends a
visible warning to its write/move results so the agent — the only entity that
can prune its own index — gets the signal.

### 3.2 Recent-messages window

Selection algorithm:

1. Filter to the **current scope**: a task-scoped run sees only events with
   `task_id == current task`; a chat/cron turn sees only `task_id is None`.
2. Walk backwards from the newest eligible message, accumulating events while
   the rolling token estimate stays below `episodic.working_memory_tokens`
   (always keeping at least `keep_recent_messages_min`; hard cap 1000).
3. STOP at the **watermark**: for the chat scope, the compaction watermark
   (§4.2); for a task scope, that task's **brief watermark** (`TASK_SPEC` §4.2).
   The two are never mixed — applying the chat watermark to a task scope would
   silently hide task turns that no STM section represents.
4. Never split a `tool_call`/`tool_result` pair: a window must not start with
   an orphan tool result.

**Cron turns are chat scope — deliberately.** Scheduled/cron conversations
carry `task_id = None`, interleave into the chat window, and are compacted
into STM like any chat turn. Scheduled activity *is* part of the agent's
episodic timeline (a purely-cron agent would otherwise accumulate no episodic
memory at all). If cron↔chat cross-talk proves painful in practice, trigger
scoping can reuse the same `Event.task_id`-style mechanism — a deliberate
deferral, not an oversight.

When `memory.inject_turn_timestamps` is true, each user message is prefixed at
render time with its local datetime (e.g. `[2026-05-30 14:23 +08:00] …`).
Render-time only — event payloads stay immutable.

### 3.3 Trigger envelope interaction

The `<trigger>…</trigger>` envelope (TRIGGER_SPEC §2.3) is appended as a
user-role message at the end of the window and counts toward the budget like
any other event. The `<task_result>` envelope (TASK_SPEC §4.1) follows the
same convention: a completed root task's result is recorded as a chat-scope
user message, entering the window — and, via tier-1, episodic memory —
naturally.

### 3.4 Budget posture

The **window** is hard-bounded by `working_memory_tokens` per scope. The
**system prompt** is soft-bounded: STM/LTM are bounded post-hoc by tiers 2/3,
the knowledge index by the §3.1 feedback warning. There is currently **no**
single end-to-end clamp `tokens(system) + tokens(window) ≤ max_context −
reserved_output`; if a model-side overflow is ever observed in practice, add
the clamp at assembly time (drop oldest window events first, never truncate a
memory block mid-content).

---

## 4. Compaction Pipeline

Three tiers. All are LLM-driven (`memory.compaction_model`), run inline after
a run completes, and respect snapshot semantics.

### 4.0 Tier-1 trigger model (ADR-0006)

There is **one tier-1 mechanism** (summarize the older portion of the chat
working window into STM) with **three triggers**; the trigger decides the
boundary:

| Trigger | Initiator | Boundary | Consent | Availability |
|---|---|---|---|---|
| **Bounded auto** | runtime, post-run | keep ~30% tail (`compute_suggested_boundary`) | none | interactive + cron |
| **User-forced full** | user, `/compact` | **everything** — working emptied | n/a | interactive |
| **Agent-proposed semantic** | agent, `memory.propose_compact` | agent-chosen topic-shift point (tool-pair-snapped) | **required, blocking** | interactive only |

- **Bounded auto** fires whenever the **chat-scope** window exceeds
  `episodic.working_memory_tokens`. Task-scoped turns are excluded from both
  the token estimate and the compaction input (ADR-0009 §5) — a busy task
  never triggers conversation compaction.
- **User-forced full** (`/compact`) empties the working window into STM and
  marks an episode boundary by emitting `session_ended` + `session_started`.
- **Agent-proposed semantic** is the agent's *only* path to compaction — no
  unconsented agent force-compact. Gated by `propose_floor_tokens` plus the
  dual cooldown (`propose_min_interval_seconds`, `propose_min_turns`). On a
  passed guard the runtime emits `mem_compact_proposed`, **blocks** awaiting
  consent over the decision channel (§4.6), then `mem_compact_approved` +
  tier-1 at the boundary, or `mem_compact_declined`. `yolo` auto-approves
  (audited). Headless/no-session proposals are a no-op.

**Task-scope compaction** is a fourth use of the same shape but a different
target: when a running task's own-scope window exceeds the budget, its older
turns are folded into the task's cumulative **brief** (not STM) and the task's
brief watermark advances. Specified in `TASK_SPEC.md` §4.2; it shares the
boundary helper and the reversible-before-irreversible posture.

### 4.1 Tier-1 (working → STM)

**Input:** chat-scope events with `watermark < id ≤ snapshot_id`.

**Output:** zero or more STM sections appended to `short_term.md`, plus an
advance of the watermark to the chosen `boundary_event_id`.

**Boundary selection:**

1. The runtime computes `suggested_boundary` — keep at least
   `keep_recent_messages_min` messages and ~30% of the budget as the raw tail,
   snapped so a tool_call/tool_result pair is never split.
2. The compaction LLM receives the to-be-compacted region and the suggested
   boundary, and returns JSON:

   ```json
   {
     "sections": [
       {"ts_start": "<ISO>", "ts_end": "<ISO>", "topic": "short phrase",
        "topics": ["keyword"], "body": "..."}
     ],
     "boundary_event_id": <int>
   }
   ```

3. The returned boundary MUST satisfy `min_id ≤ boundary ≤ suggested` (the
   model may compress less than suggested, never more); violations fall back
   to `suggested_boundary`.
4. Unparseable/invalid output aborts tier-1 with no state change and an
   `ERROR` event.

**Event:** `mem_compacted` `{tier:1, snapshot_id, boundary_event_id,
sections_added, tokens_before, tokens_after, model}` — `model` is the real
compaction model id.

### 4.2 Compaction watermark

A monotonically non-decreasing event id in `memory/watermark`. Events with
`id ≤ watermark` in the **chat scope** are represented by STM/LTM, not raw
history. Advances **only** on successful tier-1. Missing/unparseable file ⇒
watermark 0 (replay everything raw — safe fallback). Task scopes use the
per-task brief watermark instead (TASK_SPEC §4.2); the two never interact.

### 4.3 Snapshot semantics

Tier-1 captures `store.latest_id()` as its upper bound under the per-eonlet
lock; events appended during the LLM call land in the *next* pass. The agent
loop is never blocked by compaction. (M-I4.)

### 4.4 Tier-2 (STM → LTM)

**Trigger:** `tokens(short_term.md) > episodic.short_term_tokens`, checked
after a tier-1 ran.

**Input:** the full STM. **Output:** new `episodic` LTM bullets (tagged
`[src:implicit, ts:<today>]`) and a reduced STM keeping only sections the
model flagged:

```json
{
  "ltm_additions": [{"section": "episodic", "content": "..."}],
  "stm_keep_section_headers": ["## [...] topic"]
}
```

Validation: `section` MUST be `episodic`; keep-headers must exactly match
existing STM headers (unknown ⇒ ignored, missing ⇒ section dropped).

**Event:** `mem_ltm_promoted` `{snapshot_id, additions, kept_section_count}`.

### 4.5 Tier-3 (LTM forgetting)

**Trigger:** `tokens(long_term.md) > episodic.long_term_tokens`.

**Input:** the full LTM. **Output:** a rewritten LTM within budget. Selection
is uniform recency/salience — no source-based exemption (durable facts belong
in the knowledge axis, which tier-3 never touches).

```json
{
  "kept_bullets": [{"section": "...", "content": "...", "src": "...", "ts": "...", "merged_from": ["..."]}],
  "dropped_bullets": [{"section": "...", "preview": "first 80 chars", "reason": "duplicate|stale|low-salience"}]
}
```

**Event:** `mem_ltm_forgotten` `{snapshot_id, kept_count, dropped_count,
dropped_digest}` — the digest keeps *what was forgotten* in the log even when
LTM no longer has it (M-I7).

### 4.6 Decision channel (consent round-trip, ADR-0006)

Agent-proposed compaction and the interactive permission confirm share one
generic blocking round-trip (`worker/decisions.py`): the worker pushes a
`decision/request` notification to attached sessions and blocks; the CLI
answers with `decision.respond`; first responder wins. No attached session (or
the last one detaching mid-wait) auto-declines — a headless worker never
hangs.

### 4.7 Auto-compact pause

Session-scoped `auto_compact_enabled` (init from
`memory.episodic.auto_compact`; `/compact off|on` flips it; not persisted).
When false, threshold-driven compaction is suppressed; explicit `/compact`
still runs.

---

## 5. Tool Surface

All in `src/eonlet/tools/builtin/`. The `task` tool is specified in
`TASK_SPEC.md`.

### 5.1 `recall` (read_only)

```python
class RecallArgs:
    mode: Literal["by_keyword", "by_date", "by_date_range", "around_event"]
    query: str | None = None
    date: str | None = None                  # YYYY-MM-DD
    date_range: tuple[str, str] | None = None
    around_event_id: int | None = None
    context_radius: int = 5
    limit: int = 20
    include: list[Literal["events", "knowledge", "tasks"]] = ["events"]
```

Returns markdown; each hit carries its event id (`#1284`) for follow-up
`around_event` queries. Recall spans **all scopes** — it is the escape hatch
into compacted chat history and pruned task turns alike.

### 5.2 `knowledge` (destructive on mutating actions)

| action | extra args | annotation |
|---|---|---|
| `open` | `path` | read_only |
| `list` | – | read_only |
| `write` | `path, content, index_line?` | destructive |
| `edit` | `path, old_string, new_string` | destructive |
| `delete` | `path` | destructive |
| `move` | `path, new_path, index_line?` | destructive |

`write`/`edit`/`move` keep `index.md` in sync and emit `kb_*` events carrying
the resulting full body (§7). `write`/`move` results carry a visible warning
when the index exceeds `knowledge.index_max_tokens`.

### 5.3 `memory` (mixed)

| action | extra args | annotation |
|---|---|---|
| `show` | `store?: "stm"\|"ltm"\|"knowledge"\|"all"` | read_only |
| `compact` | – | destructive |
| `propose_compact` | `boundary_event_id, reason` | destructive (consented) |
| `compact_ltm` | – | destructive |
| `pause` / `resume` | – | destructive |

`compact` runs tier-1; `compact_ltm` runs tier-3; tier-2 triggers only as a
tier-1 follow-up. `propose_compact` is the §4.0 agent-proposed trigger.

---

## 6. Slash Commands

Inside `eonlet attach`; each routes through worker IPC.

| Command | Effect |
|---|---|
| `/compact` | User-forced full compaction (clean slate + episode boundary) |
| `/compact off` / `/compact on` | Toggle auto-compaction (session-scoped) |
| `/memory show [stm\|ltm\|knowledge\|all]` | Render store contents |
| `/knowledge list` / `open <path>` / `write <path> <text>` / `rm <path>` | Knowledge axis ops |
| `/task …` | Task ops (see TASK_SPEC / CLI_REFERENCE) |

---

## 7. Events

| Kind | Trigger | Payload shape |
|---|---|---|
| `mem_compacted` | tier-1 success | `{tier:1, snapshot_id, boundary_event_id, sections_added, tokens_before, tokens_after, model}` |
| `mem_ltm_promoted` | tier-2 success | `{snapshot_id, additions, kept_section_count, model}` |
| `mem_ltm_forgotten` | tier-3 success | `{snapshot_id, kept_count, dropped_count, dropped_digest, cause:"tier3", model?}` |
| `mem_recall_invoked` | `recall` entry | `{mode, query?, date?, hits}` |
| `mem_paused` / `mem_resumed` | `/compact off` / `on` | `{}` |
| `mem_compact_proposed` | proposal guards passed | `{boundary_event_id, reason, working_tokens}` |
| `mem_compact_approved` / `_declined` | consent outcome | `{boundary_event_id, rule}` |
| `kb_written` | knowledge write/edit | `{path, size, action:"write"\|"edit", content}` — **carries the resulting full body** (knowledge must be reconstructable from the log; I-S1) |
| `kb_deleted` | knowledge delete | `{path}` |
| `kb_moved` | knowledge move | `{src, dst}` |
| `session_started` / `session_ended` | `/compact` episode boundary | `{reason:"compact"}` |

STM/LTM bodies are NOT carried in events (counts, ids, digests only — they are
re-derivable); knowledge bodies ARE (they are not re-derivable from anything
else). Task events are specified in `TASK_SPEC.md`.

---

## 8. `agent.yaml` Schema

```yaml
memory:
  enabled: true
  compaction_model: "claude-haiku-4-5@anthropic"
  inject_turn_timestamps: true       # render-time [date time] prefix on user turns

  episodic:
    working_memory_tokens: 10000
    keep_recent_messages_min: 4
    short_term_tokens: 4000
    long_term_tokens: 8000
    auto_compact: true
    propose_semantic: true           # allow agent-proposed compaction (ADR-0006)
    propose_floor_tokens: 5000
    propose_min_interval_seconds: 1800
    propose_min_turns: 3

  knowledge:
    inject_index: true
    index_max_tokens: 2000           # over-budget ⇒ visible tool warning
    warn_file_tokens: 4000
```

- All sub-fields have defaults; the whole `memory:` block can be omitted.
- The legacy v0.0.x fields (`recent_messages_in_context`, `notes_files`) are
  rejected at load time with a `ConfigError`. No migration tool exists
  (pre-alpha rewrites `agent.yaml`).
- `tasks:` is a separate **top-level** block (`AGENT_CONFIG_SPEC.md` /
  `TASK_SPEC.md`), not part of `memory:`.

---

## 9. Disabled Mode

`memory.enabled = false`:

1. No memory preamble; no `<knowledge_index>`.
2. No compaction (any tier, any trigger); watermark ignored (treated as 0).
3. The window is still bounded by the `episodic` token budgets (the budget
   walk needs no memory files).
4. Memory files are not created; pre-existing files are untouched.
5. `memory` tool actions and `propose_compact` return
   `is_error=True` with a clear message; task checkpoint briefs fall back to
   the structural summary.

---

## 10. Lifecycle Hooks

1. **Worker startup** — verify/rebuild `index.sqlite`, catch up any events
   appended after the highest indexed id.
2. **Event append** — `AgentRuntime._record` indexes every text-bearing event
   into the recall index synchronously.
3. **Post-run** — the compaction cascade runs inline after every run:
   tier-1 (threshold) → tier-2 (if tier-1 ran) → tier-3 (threshold).

---

## 11. Invariants & Test Guidance

- **M-I1** (Reconstructability) See I-S1. Knowledge: replaying `kb_written`/
  `kb_deleted`/`kb_moved` reproduces the latest tree exactly.
- **M-I2** (Watermark monotonicity) Both the chat watermark and every task's
  brief watermark never decrease, including across restarts.
- **M-I3** (Boundary safety) The window never includes a `tool_result` whose
  `tool_call` is outside the window; compaction boundaries never split a pair.
- **M-I4** (Snapshot isolation) Events appended during a compaction run land
  in the next run's input, never the current run's output.
- **M-I5** (Scope isolation) Task-scoped turns never enter STM/LTM, never
  count toward the chat tier-1 threshold, and never appear in a chat window
  (and vice versa). ADR-0009.
- **M-I6** (Knowledge preservation) No automatic process ever deletes or
  rewrites a knowledge file. Only the `knowledge` tool / IPC may.
- **M-I7** (Forget auditability) After tier-3, dropped content's digest is
  recoverable from the event log.
- **M-I8** (Disabled-mode neutrality) With `memory.enabled = false`, no memory
  file is created and no memory event is emitted.

Test layout: see `tests/unit/memory/` (per-store, per-tier, injection,
recall, knowledge, agent-injection end-to-end) — the structure in the repo is
authoritative.

---

## 12. Versioning

This spec is `0.2.0` (dual-axis rewrite; supersedes `0.1.0` wholesale).
Backwards-incompatible changes bump the major version; field additions bump
the minor. `index.sqlite` carries its own `PRAGMA user_version`; missing or
older versions trigger a rebuild, not a migration.

---

## 13. References

- ADR-0003 — original memory decision (compaction tiers, recall)
- ADR-0005 — dual-axis re-architecture (knowledge axis, tasks out of memory)
- ADR-0006 — compaction trigger matrix + consent channel + timestamps
- ADR-0009 — task-scoped context; episodic = chat scope
- `TASK_SPEC.md` — task forest, briefs, task-scope compaction
- `src/eonlet/memory/` — implementation
- `src/eonlet/tools/builtin/{recall,knowledge,memory}.py`
