# Memory Subsystem — Specification

| Field | Value |
|---|---|
| Status | Draft (normative) |
| Spec version | 0.1.0 |
| Depends on | `SPEC.md`, `AGENT_CONFIG_SPEC.md`, `TOOL_SPEC.md`, `TRIGGER_SPEC.md`, `adr/0003-memory-system.md` |
| Implements | ADR-0003 |

## 0. Reader Guide

This spec is the **normative reference** for the memory subsystem introduced
by ADR-0003. The ADR records the *decision*; this spec records the *contract*
the implementation must satisfy. When the two disagree, the spec wins (and
the ADR should be amended).

Audience: implementers of `src/eonlet/memory/`, the related builtin tools,
the runtime injection point, and the CLI slash commands.

---

## 1. Vocabulary

| Term | Definition |
|---|---|
| **Working memory** | Raw conversation events not yet compacted; the runtime renders them verbatim into the LLM call. Sliding window. |
| **Short-term memory (STM)** | A markdown document of compressed conversation summaries scoped to one eonlet. |
| **Long-term memory (LTM)** | A markdown document of durable knowledge across conversations; subject to self-compaction ("forgetting"). |
| **Notes** | User-curated explicit knowledge. The runtime never deletes notes on its own. |
| **TODOs** | Action items with structured state. |
| **Recall** | Tool-driven retrieval over the raw event log and the memory documents. |
| **Compaction** | Background LLM-driven summarization that promotes content along the working → STM → LTM path. |
| **Forgetting** | Compaction restricted to LTM itself when LTM exceeds its budget. |

Working memory is not a file on disk. STM, LTM, notes, and TODOs are.

---

## 2. Storage Layout

Per eonlet:

```
~/.eonlet/eonlets/<eonlet_id>/memory/
├── short_term.md       # STM
├── long_term.md        # LTM
├── notes.md            # Notes
├── todos.jsonl         # TODOs
└── index.sqlite        # Recall FTS5 index over event log + memory docs
```

Invariants:

- **I-S1.** Every file in `memory/` is **derived state**. Deleting the
  directory MUST cause the runtime to rebuild it from the event store on
  next idle without data loss. The event store is the source of truth.
- **I-S2.** The worker is the only writer. CLI slash commands modify these
  files only by routing through the worker over IPC.
- **I-S3.** All writes use the atomic `write-temp-then-rename` pattern. A
  half-written file MUST NOT be observable.
- **I-S4.** Writes are serialized by a per-eonlet `anyio.Lock` held across
  the temp-write-and-rename of each file. There is one lock per file (four
  locks total), not one global lock — recall does not block writes to LTM.

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

Headers are machine-parseable by the runtime; the body is free text intended
for the LLM. `topic` is a human-readable short phrase; `topics:` is the
keyword list the recall index uses.

### 2.2 `long_term.md` format

Top-level structure:

```markdown
# Long-term memory

## user
- preferred concise responses; dislikes hedging language [src:feedback, ts:2026-04-12]
- works in finance, focused on portfolio automation [src:user, ts:2026-03-30]

## feedback
- never mock the database in tests [src:feedback, ts:2026-02-18]

## project
- legal compliance is the real driver of the auth rewrite [src:project, ts:2026-05-01]

## reference
- pipeline bugs tracked in Linear "INGEST" [src:reference, ts:2026-04-22]

## fact
- ...

## episodic
- 2026-05-22: spent the morning on the portfolio rebalance flow; ended with AAPL trim approved
```

The six categories (`user` / `feedback` / `project` / `reference` / `fact` /
`episodic`) are fixed. `episodic` is special: it holds compressed summaries
promoted from STM, dated and roughly chronological. The other five hold
durable atomic facts written either by promotion (auto) or by `remember`
(explicit).

Each bullet ends with a trailer `[src:<source>, ts:<ISO date>]` where
`source ∈ {user, feedback, project, reference, fact, implicit, explicit}`:

- `implicit` — produced by STM→LTM promotion
- `explicit` — produced by `remember()` or by user `/remember`
- the rest — category-matched source from the explicit writer

The trailer drives LTM compaction priorities (see §5.3).

### 2.3 `notes.md` format

Free-form markdown. No required structure. The runtime preserves whatever
the user writes; it appends new entries from `note add` at the bottom under
an `## YYYY-MM-DD HH:MM` header.

### 2.4 `todos.jsonl` format

One JSON object per line, UTF-8, no trailing comma:

```json
{"id":"todo-2026-05-22-a1b2","content":"...","status":"pending","created_at":"2026-05-22T14:03:11+08:00","due":null,"done_at":null,"tags":["..."]}
```

Schema:

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | `todo-YYYY-MM-DD-<4hex>` |
| `content` | string | yes | freeform |
| `status` | `"pending" \| "done" \| "cancelled"` | yes | |
| `created_at` | ISO-8601 string | yes | |
| `due` | ISO-8601 string \| null | no | |
| `done_at` | ISO-8601 string \| null | no | set when status transitions to `done` |
| `tags` | array of strings | no | |

Done/cancelled items remain in-file for audit. `archive_done_after_days`
(from `agent.yaml`) optionally moves them out (see §10).

### 2.5 `index.sqlite` schema

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
  content, role, kind,
  content='', tokenize='unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS msg_meta (
  event_id   INTEGER PRIMARY KEY,
  ts         INTEGER NOT NULL,         -- microseconds since epoch
  role       TEXT NOT NULL,            -- 'user' | 'assistant' | 'tool' | 'system'
  kind       TEXT NOT NULL,            -- event kind string
  fts_rowid  INTEGER NOT NULL          -- rowid in msg_fts
);

CREATE INDEX IF NOT EXISTS msg_meta_ts ON msg_meta(ts);
CREATE INDEX IF NOT EXISTS msg_meta_kind ON msg_meta(kind);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
  doc, section_id, content,
  content='', tokenize='unicode61 remove_diacritics 2'
);
```

- `msg_fts` indexes raw events from the event store (one row per event with
  text-bearing payload).
- `memory_fts` indexes sections of the memory documents (`doc` ∈
  `{'stm','ltm','notes'}`, `section_id` is the section header for STM/LTM
  or the heading anchor for notes).
- The recall index is **derived state**, rebuildable from the event store
  and memory docs. If `index.sqlite` is missing, corrupt, or stale, the
  runtime MUST rebuild it on startup (background task) without blocking the
  agent loop.

---

## 3. Context Injection Pipeline

When the runtime builds an LLM request, it produces:

```
[ system prompt (definition.system_prompt + memory preamble) ]
[ recent_messages_window (raw events, oldest→newest) ]
[ current trigger envelope or user message ]
```

### 3.1 System prompt assembly

The system message sent to the LLM is the concatenation, in order, of:

1. `definition.system_prompt` (from `system.md`)
2. Skills block (existing behavior, unchanged)
3. **Memory preamble** (new), each block omitted if its store is empty or
   `inject: false`:

   ```
   <memory>
   <long_term>
   {long_term.md contents, trimmed to long_term_tokens}
   </long_term>

   <notes>
   {notes.md contents, trimmed to notes.max_tokens with "(truncated)" marker}
   </notes>

   <todos>
   {pending todos as a bullet list, one per line: "- [id] content (due: ...)"}
   </todos>

   <short_term>
   {short_term.md contents}
   </short_term>
   </memory>
   ```

The outer `<memory>` element MUST appear iff at least one inner block does.
Empty stores produce no element at all.

### 3.2 Recent-messages window

After the memory preamble, the runtime appends raw events as normal LLM
messages (user / assistant / tool roles).

Selection algorithm:

1. Start from the most recent event.
2. Walk backwards accumulating events while the rolling token estimate stays
   below `working_memory_tokens` and the message count stays below an
   internal hard cap (1000, to bound DB reads).
3. STOP at the **compaction watermark** (see §4.2): events older than the
   watermark MUST NOT appear in the window — they are represented by STM.
4. The window MUST NOT split a `tool_call` / `tool_result` pair. If the
   walk-back would land inside a pair, extend it backwards to the
   originating `assistant_message`.

The resulting list is reversed to chronological order and emitted as the
message history.

### 3.3 Trigger envelope interaction

`TRIGGER_SPEC.md §2.3` defines the `<trigger>...</trigger>` envelope. The
envelope is **not** part of the memory preamble — it is appended as a
user-role message at the end of the recent window. The envelope's tokens
count toward `working_memory_tokens` like any other event.

### 3.4 Budget contract

The runtime MUST ensure that, after assembly:

```
tokens(system_prompt) + tokens(recent_window) ≤ runtime.max_context_tokens − reserved_output
```

where `reserved_output = max_tokens_per_response + safety_margin` and
`safety_margin = 1024`. If the budget cannot be satisfied even after STM
is fully populated, the runtime:

1. Logs a `BUDGET_WARNING` event.
2. Drops oldest events from the recent window until budget is satisfied.
3. If still over budget, drops the `<notes>` block (notes are dropped before
   LTM because LTM is already maximally compressed).

The runtime MUST NOT silently truncate STM or LTM mid-content — they are
either fully included or fully omitted.

---

## 4. Compaction Pipeline

Three tiers. All three are LLM-driven, run in the background, and respect
snapshot semantics.

### 4.1 Tier-1 (working → STM)

**Trigger:** when the token estimate of the recent-messages window (as it
would be assembled in §3.2) exceeds `conversation.working_memory_tokens`,
the runtime schedules a tier-1 pass.

**Input:** all events with `id > compaction_watermark` and `id ≤ snapshot_id`.

**Output:** zero or more new STM sections appended to `short_term.md`, plus
an advance of `compaction_watermark` to a chosen `boundary_event_id`.

**Boundary selection:**

1. The runtime computes `suggested_boundary` — the most recent event such
   that the events newer than it total at least
   `max(keep_recent_messages_min, 0.3 × working_memory_tokens)` tokens, and
   the boundary lies between an assistant turn and the next user turn (or
   between any two non-paired events).
2. The compaction LLM is called with: the full to-be-compacted region, the
   suggested boundary event_id, and a JSON-output instruction returning:

   ```json
   {
     "sections": [
       {
         "ts_start": "<ISO>",
         "ts_end": "<ISO>",
         "topic": "short phrase",
         "topics": ["keyword", "..."],
         "body": "..."
       }
     ],
     "boundary_event_id": <int>
   }
   ```

3. The boundary returned MUST satisfy
   `event_log_min_id ≤ boundary_event_id ≤ suggested_boundary`. (The model
   may compress less than suggested, never more.) If the model violates
   this, the runtime falls back to `suggested_boundary` and uses the
   sections as-is.
4. If the model output fails JSON parsing or schema validation, the
   runtime aborts tier-1 with no state change and logs an `ERROR` event.
   The next tier-1 attempt happens on the next threshold crossing.

**Event:** `mem_compacted` with payload
`{tier: 1, snapshot_id, boundary_event_id, sections_added, tokens_before, tokens_after, model}`.

### 4.2 Compaction watermark

A monotonically non-decreasing event ID stored in `~/.eonlet/eonlets/<id>/memory/watermark`
(a tiny text file containing a single integer).

- Events with `id ≤ watermark` are represented by STM/LTM, not by raw
  history.
- The watermark advances **only** on successful tier-1 compaction.
- On worker startup, the runtime reads the watermark; if the file is
  missing or unparseable, watermark = 0 (replay everything as raw — safe
  fallback).

### 4.3 Snapshot semantics

Tier-1 pseudocode:

```python
async def run_tier1():
    async with stm_lock:
        snapshot_id = store.latest_id()                # capture
        events = store.read_range(watermark, snapshot_id)
        if estimate_tokens(events) < working_memory_tokens:
            return                                     # raced; nothing to do
        result = await compaction_model.summarize(events, suggested_boundary)
        if not valid(result):
            emit(ERROR); return
        append_sections(short_term_md, result.sections)
        watermark = result.boundary_event_id
        emit(mem_compacted, ...)
    # check if tier-2 is now warranted
    if estimate_tokens(short_term_md) >= short_term_tokens:
        schedule_tier2()
```

New events arriving during the LLM call are appended normally to the event
store; they will be eligible for the *next* tier-1 pass. The agent loop is
never blocked by compaction.

### 4.4 Tier-2 (STM → LTM)

**Trigger:** when `tokens(short_term.md) > conversation.short_term_tokens`.

**Input:** the full STM, sectioned per §2.1.

**Output:** new LTM bullets (under existing category headers; create the
header if absent) and a reduced STM keeping only the sections the model
flagged as `stm_keep`.

**LLM response schema:**

```json
{
  "ltm_additions": [
    {"section": "user|feedback|project|reference|fact|episodic", "content": "..."}
  ],
  "stm_keep_section_headers": ["## [...] topic", "..."]
}
```

Validation:

- Each `ltm_additions[].section` MUST be one of the six categories.
- Each header in `stm_keep_section_headers` MUST be an exact match for an
  existing STM section header. Unknown headers are ignored; missing
  matches result in the section being dropped.
- All bullets added by tier-2 are tagged `[src:implicit, ts:<today>]`.

**Event:** `mem_ltm_promoted` with payload
`{snapshot_id, additions, kept_section_count}`.

### 4.5 Tier-3 (LTM → LTM, "forgetting")

**Trigger:** when `tokens(long_term.md) > conversation.long_term_tokens`.

**Input:** the full LTM document.

**Output:** a rewritten LTM that fits within budget. Items tagged
`src:explicit` (or any of the explicit categories `user/feedback/project/
reference`) are merge candidates only; items tagged `src:implicit` (from
tier-2) may be dropped entirely.

**LLM response schema:**

```json
{
  "kept_bullets": [
    {"section": "...", "content": "...", "src": "...", "ts": "...", "merged_from": ["..."]}
  ],
  "dropped_bullets": [
    {"section": "...", "preview": "first 80 chars", "reason": "duplicate|stale|low-salience"}
  ]
}
```

`merged_from` allows two implicit observations to combine into one
consolidated explicit-style bullet.

The runtime rewrites `long_term.md` in full from `kept_bullets`, preserving
category ordering.

**Event:** `mem_ltm_forgotten` with payload
`{snapshot_id, kept_count, dropped_count, dropped_digest: [...]}`.

`dropped_digest` carries the `preview + reason` for each drop so the event
log retains *what was forgotten* even when LTM no longer does.

### 4.6 Auto-compact pause

The worker exposes a session-scoped boolean `auto_compact_enabled`,
initialized from `agent.yaml`'s `memory.conversation.auto_compact`. When
false, threshold-driven compaction is suppressed; explicit calls to
`memory.compact` / `/compact` still run.

This flag is **not** persisted. Restart re-reads config defaults.

---

## 5. Tool Surface

All tools live in `src/eonlet/tools/builtin/`. Each follows `TOOL_SPEC.md`
conventions. Tools are listed by canonical name; `ToolAnnotations` shows
the permission posture.

### 5.1 `recall` (read_only)

```python
class RecallArgs:
    mode: Literal["by_keyword", "by_date", "by_date_range", "around_event"]
    query: str | None = None              # by_keyword
    date: str | None = None               # by_date — YYYY-MM-DD
    date_range: tuple[str, str] | None = None
    around_event_id: int | None = None
    context_radius: int = 5
    limit: int = 20
    include: list[Literal["events", "notes", "todos", "memory"]] = ["events"]
```

Returns markdown formatted as:

```
## by_keyword "AAPL trim" — 7 hits

### [2026-05-22 14:23 #1284] user
We should trim AAPL by 3% this week.

### [2026-05-22 14:24 #1285] assistant
Got it — preparing the trade...
```

Each hit carries the event id (`#1284`) for follow-up `around_event`
queries. `include="memory"` adds a section per matching memory document.

### 5.2 `remember` (destructive)

```python
class RememberArgs:
    content: str
    category: Literal["user", "feedback", "project", "reference", "fact"] = "fact"
```

Writes a bullet to LTM under the given category with trailer
`[src:explicit, ts:<today>]`. `episodic` is not a valid explicit category
(it's compaction-only).

### 5.3 `note` (destructive on mutating actions)

Discriminated union by `action`:

| action | extra args | annotation |
|---|---|---|
| `add` | `title?: str, content: str, tags?: list[str]` | destructive |
| `list` | `tags?: list[str]` | read_only |
| `get` | `id: str` | read_only |
| `update` | `id: str, content: str` | destructive |
| `delete` | `id: str` | destructive |

Notes use slugged ids derived from title or auto-generated
(`note-<YYYY-MM-DD>-<4hex>`).

### 5.4 `todo` (destructive on mutating actions)

| action | extra args | annotation |
|---|---|---|
| `add` | `content: str, due?: ISO, tags?: list[str]` | destructive |
| `list` | `status?: "pending|done|cancelled|all" = "pending"` | read_only |
| `done` | `id: str` | destructive |
| `update` | `id: str, content?: str, due?: ISO, tags?: list[str]` | destructive |
| `delete` | `id: str` | destructive |

### 5.5 `memory` (mixed)

| action | extra args | annotation |
|---|---|---|
| `show` | `store?: "stm|ltm|notes|todos|all" = "all"` | read_only |
| `compact` | – | destructive |
| `compact_ltm` | – | destructive |
| `pause` | – | destructive |
| `resume` | – | destructive |

`compact` runs tier-1; `compact_ltm` runs tier-3. Tier-2 is not exposed as
a direct action — it triggers automatically as a follow-up to tier-1.

### 5.6 `forget` (destructive)

```python
class ForgetArgs:
    target: str          # id, partial content match, or category:bullet_index
    confirm: bool = False
```

Without `confirm=True`, returns a dry-run preview of what would be deleted.
With `confirm=True`, performs the deletion and emits `mem_ltm_forgotten`
with `dropped_digest` carrying the removed content.

### 5.7 Removed tools and fields

The current `notes_read` / `notes_append` tools (v0.0.x) are **removed**
by P2 of this spec. The new `note.add` action subsumes `notes_append`;
`note.get` and `note.list` subsume `notes_read`.

The `memory.notes_files` and `memory.recent_messages_in_context` fields
in `agent.yaml` (v0.0.x schema) are **removed** outright. The new layout
has exactly one `notes.md` per eonlet, and recent-window sizing is
token-driven (`working_memory_tokens` + `keep_recent_messages_min` floor).
The config loader rejects the legacy fields with a `ConfigError`.

---

## 6. Slash Commands

Surface available inside `eonlet attach`. Each routes through IPC to the
worker; CLI is a thin client.

| Command | Effect |
|---|---|
| `/compact` | Force tier-1 |
| `/compact off` | Disable auto-compaction (session-scoped) |
| `/compact on` | Re-enable auto-compaction |
| `/compact ltm` | Force tier-3 |
| `/memory show [stm\|ltm\|notes\|todos]` | Render store contents to the terminal |
| `/memory edit <store>` | Open `$EDITOR` on the file; on close, reload |
| `/recall <query>` | Keyword recall, rendered with rich formatting |
| `/recall date <YYYY-MM-DD>` | By-date recall |
| `/note add <text>` | Append a note |
| `/note list [tag]` | List notes, optionally filtered by tag |
| `/todo add <text>` | Create a todo |
| `/todo done <id>` | Mark a todo done |
| `/todo list [status]` | List todos |
| `/remember <text>` | Write to LTM, default category `fact` |
| `/forget <id\|query>` | Forget bullets (CLI prompts for confirm) |

Slash commands MUST be parsed by the CLI; the IPC method exposed to the
worker is `memory.command(verb, args)` with a structured payload.

---

## 7. Events

All memory-related state changes append events to the event store. Kinds:

| Kind | Trigger | Payload shape |
|---|---|---|
| `mem_compacted` | tier-1 success | `{tier:1, snapshot_id, boundary_event_id, sections_added:int, tokens_before:int, tokens_after:int, model:str}` |
| `mem_ltm_promoted` | tier-2 success | `{snapshot_id, additions:[{section,content,src,ts}], kept_section_count:int, model:str}` |
| `mem_ltm_forgotten` | tier-3 success OR `forget` tool | `{snapshot_id?, kept_count:int, dropped_count:int, dropped_digest:[{section,preview,reason}], cause:"tier3"\|"forget", model?:str}` |
| `mem_note_added` | `note.add` | `{id, title?, tags}` |
| `mem_note_updated` | `note.update` | `{id}` |
| `mem_note_deleted` | `note.delete` | `{id}` |
| `mem_todo_added` | `todo.add` | `{id, content, due?, tags}` |
| `mem_todo_updated` | `todo.update`/`todo.done` | `{id, status, done_at?}` |
| `mem_todo_deleted` | `todo.delete` | `{id}` |
| `mem_remember` | `remember` tool | `{section, content_preview, src:"explicit", ts}` |
| `mem_recall_invoked` | `recall` tool entry | `{mode, query?, date?, hits:int}` |
| `mem_paused` / `mem_resumed` | `/compact off` / `/compact on` | `{}` |

All of these are first-class `EventKind` values. `eonlet replay`, `eonlet
tail`, and `eonlet export` MUST include them.

Bodies of memory documents are NOT carried in event payloads (too large) —
events carry counts, ids, and digests only. The current document state can
be read from disk.

---

## 8. `agent.yaml` Schema

The `memory` block in `agent.yaml`. **Old shape** (current code):

```yaml
memory:
  recent_messages_in_context: 50
  notes_files: [notes.md, todo.md]
```

**New shape** (this spec):

```yaml
memory:
  enabled: true
  compaction_model: "claude-haiku-4-5-20251001"

  conversation:
    working_memory_tokens: 10000
    keep_recent_messages_min: 4
    short_term_tokens: 4000
    long_term_tokens: 8000
    auto_compact: true

  notes:
    max_tokens: 4000
    inject: true

  todos:
    inject_active: true
    archive_done_after_days: 30      # 0 disables archival
```

Loader behavior:

- All sub-fields have defaults; the entire `memory:` block can be omitted.
- The legacy fields `recent_messages_in_context` and `notes_files` are
  **rejected** at load time with a `ConfigError` pointing to the
  migration tool. There is no silent acceptance period.
- `enabled: false` disables the entire subsystem: no preamble injection, no
  compaction, no tier triggers. The runtime falls back to the v0.0.x
  "replay everything" behavior. Memory tools remain registered but return
  `is_error=True` with a clear message.
- `compaction_model` accepts any string the existing provider router
  accepts (including `fake-*` for tests).

---

## 9. Disabled Mode

`memory.enabled = false` semantics in detail:

1. No memory preamble is added to the system prompt.
2. The runtime falls back to `recent_messages_in_context` for window
   selection (with the new spec's default = 30).
3. Compaction tasks are not scheduled.
4. STM/LTM/notes/todos files are not created. Pre-existing files are
   untouched.
5. Memory tools (`recall`, `remember`, `note`, `todo`, `memory`, `forget`)
   return `ToolResult(is_error=True, content="memory subsystem disabled in agent.yaml")`.
6. Slash commands print a one-line notice and do nothing.

This mode exists for tests, one-shot agents, and the migration period
before P2 has shipped.

---

## 10. Lifecycle Hooks

The memory subsystem hooks into the worker lifecycle at four points:

1. **Worker startup** — read watermark; verify `index.sqlite` schema and
   rebuild if missing/corrupt; load memory document file mtimes for the
   memory preamble cache.
2. **Event append** — incrementally update `msg_fts` and `msg_meta` for any
   event with text-bearing payload.
3. **Worker idle** — opportunistic time to run pending compaction tasks.
   The runtime checks token estimates at the end of every agent run; if a
   tier's threshold is crossed, it schedules the task before going idle.
4. **TODO archival sweep** — once per worker startup and once per day, scan
   `todos.jsonl` for entries with `status=done` and `done_at` older than
   `archive_done_after_days`; move them to `todos.archive.jsonl`. Skipped
   if `archive_done_after_days == 0`.

---

## 11. Migration from Legacy `auto memory`

`eonlet memory migrate <legacy_dir>` is a one-shot CLI subcommand that:

1. Reads `<legacy_dir>/MEMORY.md` and the per-fact files it indexes.
2. Maps each fact's frontmatter `metadata.type` to the corresponding LTM
   category (`user`/`feedback`/`project`/`reference`).
3. Constructs an LTM document, writing each fact as one bullet with
   trailer `[src:explicit, ts:<original mtime>]`.
4. Writes the result to the target eonlet's `memory/long_term.md`.
5. Refuses to overwrite an existing LTM file unless `--force` is passed.

The migration tool is documented in `CLI_REFERENCE.md`. Migration is
opt-in; agents created after P2 ships start with empty memory documents.

---

## 12. Invariants & Test Guidance

Implementations MUST verify these invariants:

- **M-I1** (Replayability) Given an event log, the post-compaction state
  of `memory/` can be reconstructed by replaying the log. Concretely:
  delete `memory/`, restart the worker — the recall index rebuilds, STM
  and LTM are reconstructed from `mem_compacted` / `mem_ltm_promoted` /
  `mem_ltm_forgotten` events.
- **M-I2** (Watermark monotonicity) Watermark never decreases across the
  worker's lifetime, including across restarts.
- **M-I3** (Boundary safety) The recent-messages window NEVER includes a
  `tool_result` event whose corresponding `tool_call` is outside the
  window.
- **M-I4** (Snapshot isolation) New events appended during a compaction
  run land in the next run's input, never the current run's output.
- **M-I5** (Budget) For any sequence of conversations, `tokens(injected) ≤
  runtime.max_context_tokens`. Provable by construction in §3.4.
- **M-I6** (Notes preservation) Auto-compaction NEVER deletes notes
  entries.
- **M-I7** (Forget audibility) After `forget` or tier-3, the dropped
  content's digest is recoverable from the event log.
- **M-I8** (Disabled-mode neutrality) With `memory.enabled = false`, no
  memory file is created and no memory event is emitted.

Test layout:

```
tests/unit/memory/
├── test_config.py            # schema, defaults, deprecation warnings
├── test_storage.py           # atomic write, lock, file format round-trip
├── test_watermark.py         # monotonicity, missing-file fallback
├── test_index.py             # FTS5 rebuild, incremental update
├── test_boundary.py          # M-I3 — never split tool_call pairs
├── test_injection.py         # preamble assembly, budget enforcement
├── test_compaction_t1.py     # uses FakeProvider for the compaction LLM
├── test_compaction_t2.py
├── test_compaction_t3.py
├── test_tools_recall.py
├── test_tools_remember.py
├── test_tools_note.py
├── test_tools_todo.py
├── test_tools_memory.py
└── test_disabled.py          # M-I8

tests/integration/
└── test_memory_e2e.py        # full loop: chat → tier-1 → tier-2 → recall
```

The compaction LLM in tests is the `FakeProvider` (added in v0.0.5),
parameterized to return canned JSON for tier-1/2/3 requests.

---

## 13. Versioning

This spec is `0.1.0`. Backwards-incompatible changes bump the major
version; field additions bump the minor.

Files written by this spec carry no embedded version — they are read
liberally. The `index.sqlite` schema has its own `PRAGMA user_version`
managed by the storage layer; missing or older versions trigger a
rebuild, not a migration.

---

## 14. References

- ADR-0003 — decision record for this subsystem
- `SPEC.md` §7.5 — to be updated when this spec lands (memory subsystem
  becomes non-trivial)
- `TOOL_SPEC.md` — tool protocol the memory tools implement
- `AGENT_CONFIG_SPEC.md` — to be updated with the new `memory:` block
- `src/eonlet/memory/` — implementation
- `src/eonlet/tools/builtin/{recall,remember,note,todo,memory,forget}.py`
