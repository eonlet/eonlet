# ADR-0003: Memory System — Conversation Compaction, Long-term Memory, Notes, TODOs, Recall

| Field | Value |
|---|---|
| Status | Accepted (shipped in v0.0.6) |
| Date | 2026-05-22 |
| Deciders | Ziyu |
| Supersedes | – |
| Superseded by | – |

## Context

Today, the runtime's working context is naïve: every turn replays the full
event log into the provider, bounded only by the model's context window. This
breaks down quickly for long-running agents:

- **Cost** scales linearly with conversation length on every call.
- **Latency** grows for the same reason.
- **Context exhaustion** eventually kills the loop — there's no graceful path
  from "200k tokens of history" to "agent can still think."
- **Persistent knowledge** about the user (preferences, project facts) has no
  home other than the system prompt or an ad-hoc `auto memory` scheme
  prototyped in the user's main Claude Code config. The existing approach (a
  `MEMORY.md` index pointing at per-fact markdown files) is simplistic: it
  doesn't distinguish conversation summaries from durable user facts, doesn't
  decay, doesn't get injected on a budget, and isn't writable from inside the
  agent loop in a structured way.

The user wants a **memory system** with the rough shape of human memory:

1. **Working memory** — recent turns the model sees verbatim.
2. **Short-term memory** — a compressed summary of older turns in this
   conversation thread.
3. **Long-term memory** — durable knowledge across conversations, itself
   subject to compaction ("forgetting") when full.
4. **Notes** — explicit, user-managed knowledge the agent should always know
   and never forget on its own.
5. **TODOs** — action items with state.
6. **Recall** — when summaries are too lossy, the agent can search the original
   event log by date or keyword, like a human leafing back through chat logs.

The four persistent layers (short-term, long-term, notes, todos) are tightly
coupled — they share storage layout, injection budget, the same recall
surface, and the same compaction model. They belong in one ADR.

### Scope of this change

This ADR **replaces** the prototype `auto memory` scheme. The categories from
that scheme (`user` / `feedback` / `project` / `reference`) survive as
subsections inside long-term memory; the file layout and write path change.

This ADR is v0.1.x scope: all memory artifacts are **plain text**, no
embeddings, no vector store. Semantic/vector recall is deferred to v0.2+
(consistent with `src/eonlet/README.md`).

## Decision

Introduce a **Memory System** per eonlet. Four persistent stores on disk, a
fixed injection pipeline into every LLM call, a background compaction task, a
small family of builtin tools, and a set of slash commands. Per-eonlet
configuration lives under a new `memory:` block in `agent.yaml`.

### Vocabulary

| Term | Meaning | Decays? |
|---|---|---|
| Working memory | Most recent N raw messages in the LLM call | n/a (sliding) |
| Short-term memory (STM) | Summary of compressed conversation turns | yes → LTM |
| Long-term memory (LTM) | Durable facts/summaries about user, project, prior conversations | yes (compressed when full) |
| Notes | User-curated knowledge base, agent-writable on request | no (manual) |
| TODOs | Action items with `pending` / `done` state | done items archive |

Critically: **long-term memory forgets; notes do not.** When the user says
"remember X," the agent decides whether X is a soft recollection (LTM) or a
hard fact that must not erode (Notes). When in doubt, prefer Notes.

### Storage layout

```
~/.eonlet/eonlets/<eonlet_id>/memory/
├── short_term.md       # episodic conversation summaries
├── long_term.md        # durable knowledge (sections: user / feedback / project / reference / fact)
├── notes.md            # user-curated explicit knowledge
├── todos.jsonl         # one TODO per line
└── index.sqlite        # recall index: keyword/date → event_id
```

Format choices:

- **Markdown for short_term / long_term / notes.** Human-inspectable, easy to
  `cat`/`vim`, friendly to LLM compaction (it produces and consumes the same
  format). Each section is a `## [<timestamp range>] <topic>` header followed
  by free text, so that "by date" and "by topic" navigation is mechanical.
- **JSONL for todos.** TODOs have structured state (`id`, `content`,
  `status`, `created_at`, `due`, `done_at`). Markdown checkboxes can't carry
  these reliably. JSONL stays atomic-append-friendly and trivially diffable.
- **SQLite for the recall index.** FTS5 over message text + a
  `(timestamp, event_id)` table is the smallest piece of machinery that
  supports both keyword search and "what was happening on YYYY-MM-DD."

Atomicity follows the same pattern as `dynamic_triggers.json` (ADR-0002):
write-temp-then-rename, single writer (the worker), per-eonlet `anyio.Lock`.

The **SQLite event store remains the source of truth** for raw history. The
files above are derived state — if `memory/` is deleted, the system rebuilds
on next idle by replaying. Memory is a cache over the event log, not a
parallel timeline.

### Context injection pipeline

Every LLM call assembles messages in this order:

```
[ system prompt (from system.md) ]
  └─ <long_term>{long_term.md}</long_term>
  └─ <notes>{notes.md}</notes>
  └─ <todos>{active todos from todos.jsonl}</todos>
  └─ <short_term>{short_term.md}</short_term>
[ recent_messages_window: last m raw events ]
[ current user message / trigger envelope ]
```

The four `<...>` blocks are appended to the system prompt as a single
"memory" preamble, separated by clear delimiters. They are **not** sent as a
fake user/assistant turn — they're system context.

Injection respects budgets:

- LTM and STM are bounded by `long_term_tokens` / `short_term_tokens` (they
  can't exceed these on disk because compaction enforces it).
- Notes are bounded by `notes.max_tokens`. Exceeding triggers a warning to the
  user (the user owns notes — the system does **not** auto-trim them).
- TODOs inject only `status == "pending"`. Done items are kept in-file for
  audit but not injected.

The remaining context budget goes to the recent-messages window.

### Compaction pipeline

Compaction runs in the worker, off the request hot path. Two trigger sources:

1. **Token-threshold trigger.** After each turn, the runtime estimates the
   tokens in the current working window (raw events not yet summarized). When
   that exceeds `working_memory_tokens`, schedule a compaction pass.
2. **Explicit trigger.** Slash command `/compact` or tool call.

Compaction is **not** synchronous with the user's next message. It runs in a
background task scoped to the worker's main task group. While running, the
worker continues to accept new turns; those new turns are appended to the
event log normally and become eligible for the next compaction pass.

#### Snapshot semantics

When compaction starts, it captures the current event-log upper bound
(`event_id_high_water`). It only summarizes events ≤ that bound. New events
arriving during compaction are untouched. This makes compaction safe under
concurrent writes without needing a global lock.

#### Boundary selection — "where does the recent window end?"

Naïvely chopping at "last m messages" splits tool_call/tool_result pairs and
mid-topic, hurting coherence. The chosen approach:

1. The runtime computes a **suggested boundary** by walking backwards through
   recent events until it has at least `keep_recent_messages_min` messages
   *and* at least ~30% of the token budget. The boundary is then nudged
   forward to the nearest tool_call/tool_result pair boundary.
2. The compaction LLM is given (a) the full to-be-compacted region and (b)
   the suggested boundary, and asked to return:

   ```json
   {
     "summary": "...",
     "boundary_event_id": "evt-...",
     "topics": ["...", "..."]
   }
   ```

   The model is allowed to **move the boundary backwards** (compress less, keep
   more) if doing so preserves topic coherence, but not forwards. This is the
   "model decides where to cut" behavior — bounded so it can't over-compress
   recent context.
3. If the model's JSON is malformed or the boundary is invalid, fall back to
   the heuristic boundary. The compaction LLM never gets a chance to break
   the conversation.

#### What gets compressed

Tool calls and their results compress aggressively: a chain of "agent called
`web_fetch`, got HTML, called `bash`, got output, called `web_fetch` again..."
becomes one sentence in the summary. Raw tool I/O is **not** preserved in
STM — if the agent needs it, `recall` retrieves it from the event log.

User messages and assistant text responses are compressed less aggressively
because they carry intent and reasoning the model needs to stay coherent.

The compaction model is instructed to produce a section with a header in the
exact format `## [<ISO timestamp> – <ISO timestamp>] <topic>` so the recall
index can parse it.

#### Tier 2: STM → LTM

When `short_term.md` exceeds `short_term_tokens`, run a second-pass compaction
that takes STM sections and proposes additions to LTM. Output structure:

```json
{
  "ltm_additions": [
    {"section": "user", "content": "..."},
    {"section": "project", "content": "..."},
    {"section": "fact", "content": "..."}
  ],
  "stm_keep": ["section-id-1", "section-id-2"]
}
```

Sections moved to LTM are removed from STM; sections in `stm_keep` (recent or
high-salience) stay.

#### Tier 3: LTM forgetting

When `long_term.md` exceeds `long_term_tokens`, run a compaction over LTM
itself: merge related items, drop low-salience ones, deduplicate. This is
"forgetting" — items don't vanish without trace because the original event
log still has them, but they're no longer in the injected context.

LTM compaction is conservative on `notes`-adjacent material: anything tagged
or written via `remember(category="...")` from the explicit path is marked
`source: explicit` and is the last to be dropped. Implicitly-derived LTM is
dropped first.

#### Compaction model

Configured per-eonlet via `memory.compaction_model`. Defaults to
`claude-haiku-4-5-20251001` — Haiku is fast, cheap, and good at summarization;
using the main agent's Opus would be wasteful. The provider is selected by
the same `fake-*` / model-prefix routing the agent uses, so tests can plug
`fake-echo` here too.

### Recall

Recall is a **builtin tool**, not implicit context injection. The agent
decides when to leaf through the original log.

```python
@tool
def recall(
    mode: Literal["by_keyword", "by_date", "by_date_range", "around_event"],
    query: str | None = None,
    date: str | None = None,          # YYYY-MM-DD
    date_range: tuple[str, str] | None = None,
    around_event_id: str | None = None,
    context_radius: int = 5,          # how many events before/after to include
    limit: int = 20,
    include: list[Literal["events", "notes", "todos", "memory"]] = ["events"],
) -> str: ...
```

Returns markdown-formatted snippets with timestamps and `event_id`s, so the
agent can drill in via `around_event` after a keyword hit — matching the
"keyword → date → detail" pattern described in design.

The recall index (`index.sqlite`) is updated incrementally by the event store
on every append (FTS5 over message content + `(ts, event_id, role)` table).
Rebuild on startup if the file is missing or corrupt.

`include="memory"` searches the markdown files too — useful when the agent
wants to know "did I already note this somewhere?" before writing a duplicate.

### Tools

A small family, action-style where it makes sense (matching the `schedule`
tool pattern from ADR-0002):

| Tool | Actions / purpose |
|---|---|
| `recall` | as above |
| `remember` | `(content, category, ttl?)` — write directly to LTM. Categories: `user`, `feedback`, `project`, `reference`, `fact`. |
| `note` | `add` / `list` / `get` / `update` / `delete` |
| `todo` | `add` / `list` / `done` / `update` / `delete` |
| `memory` | `compact` (force tier-1), `compact_ltm` (force tier-3), `show` (return current STM/LTM/notes), `pause` / `resume` (toggle auto-compaction) |
| `forget` | `(target_id_or_query)` — explicit deletion from LTM/notes |

Permissions:

- `recall`, `note.list`, `note.get`, `todo.list`, `memory.show` —
  `read_only=True`.
- All other actions — `destructive=True`. `ask` mode prompts; `yolo` mode
  lets the agent self-manage.

### Slash commands

User-facing CLI commands inside `eonlet attach`:

| Command | Effect |
|---|---|
| `/compact` | Force tier-1 compaction now |
| `/compact off` / `/compact on` | Toggle auto-compaction for this session |
| `/memory show [stm\|ltm\|notes\|todos]` | Print current memory content |
| `/memory edit <store>` | Open `$EDITOR` on the file (trust the user) |
| `/recall <query>` | User-side keyword recall (renders nicer than tool output) |
| `/note add <text>` / `/note list` | Direct note management |
| `/todo add <text>` / `/todo done <id>` / `/todo list` | Direct todo management |
| `/forget <id\|query>` | Direct deletion |

`/compact off` sets a session-scoped flag on the worker; it does not persist
to `agent.yaml`. Restart re-reads config defaults. (Same pattern as static
trigger toggles in ADR-0002.)

### `agent.yaml` schema additions

```yaml
memory:
  enabled: true
  compaction_model: "claude-haiku-4-5-20251001"

  conversation:
    working_memory_tokens: 10000      # tier-1 trigger threshold
    keep_recent_messages_min: 4       # boundary floor
    short_term_tokens: 4000           # tier-2 trigger threshold
    long_term_tokens: 8000            # tier-3 trigger threshold
    auto_compact: true

  notes:
    max_tokens: 4000
    inject: true

  todos:
    inject_active: true
    archive_done_after_days: 30       # 0 = never
```

Defaults (when `memory:` is absent or partially specified) live in
`runtime/memory/config.py` and are chosen for an agent running on
Claude Opus / Sonnet with a ~200k window — conservative on injection (~26k
tokens worst-case across all stores + recent window) to leave headroom for
tool I/O.

`memory: { enabled: false }` is a valid escape hatch: the system disables
itself entirely, the runtime falls back to the current "replay everything"
behavior. Useful for one-shot agents and tests.

### Compatibility with existing `auto memory` scheme

The user's existing `MEMORY.md`-and-per-file scheme migrates as follows:

- The four category names (`user`, `feedback`, `project`, `reference`) become
  H2 headers inside `long_term.md`.
- Each existing per-fact file becomes a bullet under its category header,
  carrying the original `description:` line as a sub-bullet for searchability.
- The `MEMORY.md` index is dropped — `long_term.md` is now small enough to
  read whole, and the recall index handles search.
- A one-off migration tool (`eonlet memory migrate <old_dir>`) ships in the
  same release. It's not run automatically; the user invokes it per eonlet.

The verbose "auto memory" preamble currently sitting in the system prompt is
removed. Memory injection is now driven by the runtime, not by prose in the
prompt — the runtime tells the model what's in each store via the structured
`<long_term>`/`<notes>`/etc. delimiters, and the system prompt only needs a
short paragraph explaining the tools.

### Concurrency model summary

- **One writer per file**: the worker. CLI slash commands route through IPC.
- **Per-eonlet locks**: one `anyio.Lock` per memory file family, held briefly
  around write-temp-then-rename.
- **Snapshot-based compaction**: captures `event_id_high_water` at start;
  new events during compaction are appended to the log normally and picked up
  in the next pass.
- **No cross-eonlet sharing**: every eonlet has its own `memory/` directory.
  No shared/global memory in v0.1.

### Event-store integration

New event kinds emitted by the memory subsystem:

- `mem_compacted` — tier-1 fired. Carries `boundary_event_id`, tokens before/
  after, model used.
- `mem_ltm_promoted` — tier-2 fired.
- `mem_ltm_forgotten` — tier-3 fired. Carries a digest of what was dropped.
- `mem_note_added` / `mem_note_deleted` / `mem_todo_added` / `mem_todo_done` /
  etc. — explicit writes.
- `mem_recall_invoked` — agent called `recall`. Useful for understanding what
  the agent didn't know on its own.

These events make the memory subsystem replayable and auditable. They also
appear in `eonlet tail` and `eonlet replay` for free.

## Consequences

### Positive

- Long-running agents stop linearly bleeding tokens. A weeks-old `x-digest`
  or `portfolio` eonlet fits in a small fraction of context regardless of
  conversation length.
- Persistent knowledge has a proper home with clear semantics (forget vs.
  don't-forget). The current ad-hoc auto-memory scheme retires.
- TODOs as a first-class concept gives agents something they currently fake
  with prose.
- Recall as an explicit tool keeps the model honest — "I don't remember,
  let me check" is a visible tool call in the event log, not hidden RAG.
- Configurable per eonlet — a chatty `assistant` can have aggressive
  compaction, a focused `portfolio` agent can keep more raw history.
- Compaction model is decoupled from agent model — cheap Haiku does the
  summary work even when Opus is the agent.

### Negative

- New subsystem with non-trivial surface: 4 storage files, 6 tools, 7 slash
  commands, 1 new index DB, 5+ new event kinds. Test surface grows
  accordingly.
- Compaction is a background task with its own failure modes (model output
  malformed, model unavailable, file corruption). The fallback paths must be
  conservative — when in doubt, do nothing and let the next pass try.
- Two LLM providers in play per agent (main + compaction). Doubles the
  credential surface for users on non-Anthropic providers.
- `eonlet export`/`import` (v0.0.3) must include the entire `memory/`
  directory in the bundle and re-stamp paths on import.
- The compaction LLM sees user conversation content. For privacy-conscious
  setups, `memory.compaction_model` must accept a local/offline provider
  (e.g. fake/local), which we already support via the provider routing.

### Neutral

- v0.1.x ships text-only. Vector recall (v0.2+) will plug into the existing
  `recall` tool by adding a `mode="by_semantic"` action — no breaking change
  to the tool surface.
- The `recall` tool's `include="memory"` mode is the seam through which a
  future semantic store gets exposed without inventing a new tool.

## Alternatives Considered

### A. Implicit RAG on every turn

Run a recall query against history on every user message and silently inject
top-k snippets. Rejected: opaque, hard to debug, inflates tokens
unpredictably, and bypasses the "model decides when it needs to remember"
property that makes `recall` legible in event logs.

### B. Single unified `memory.md` (no STM/LTM/notes/todos distinction)

One markdown file the model reads and writes freely. Rejected: conflates
"forgettable" with "must-keep" content, has no natural compaction signal,
and merges action items (TODOs need state) with knowledge.

### C. Compress incrementally on every turn

Run a tiny summarization on every user/assistant turn and only keep the
summary, never the raw turn. Rejected: too lossy for short conversations,
double the LLM calls per turn, and recall becomes useless because raw
events were summarized away on the fly. Keeping raw events in the event log
and compressing on a threshold preserves the option to drill down.

### D. Store memory in the event log only, project on read

Don't write `short_term.md` etc. to disk — derive them from
`mem_compacted` events every time. Rejected: every LLM call would need to
fold the projection, adding latency. The snapshot files are a derived cache;
that's the right shape.

### E. Keep TODOs in markdown checkboxes

Rejected for v0.1: status, due dates, IDs are awkward in markdown. JSONL is
tiny and structured. We can render TODOs as markdown for `/todo list` and
for context injection — the storage format is an internal choice.

### F. Per-conversation memory (one short_term per `attach` session)

Rejected: an eonlet's identity persists across attaches and across triggers.
A conversation isn't a clean boundary. Time-and-topic-based STM sections
(via the section header) carry the same information without needing a
"session" concept.

## References

- `docs/SPEC.md` — runtime, event store, agent loop
- `docs/TOOL_SPEC.md` — tool protocol the memory tools implement
- `docs/TRIGGER_SPEC.md` — how trigger envelopes interact with memory
  injection (the envelope counts toward working-memory tokens)
- `docs/AGENT_CONFIG_SPEC.md` — to be updated with the `memory:` block
- `docs/adr/0002-dynamic-triggers.md` — same storage and IPC patterns
- `src/eonlet/runtime/store.py` — extension points for memory events and
  the FTS5 recall index
- `src/eonlet/memory/` — package home (currently an empty placeholder)

## Update history

- 2026-05-22: Initial proposal.
- 2026-05-22: Deprecation-period removed — legacy `memory.notes_files` and
  `memory.recent_messages_in_context` fields are rejected outright by the
  config loader rather than warned-and-accepted. Project is pre-1.0 and the
  author owns all in-flight agents; backwards-compat warnings add cost
  without benefit.
