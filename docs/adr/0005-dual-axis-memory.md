# ADR-0005: Dual-Axis Memory — Episodic Timeline vs Curated Knowledge Base

| Field | Value |
|---|---|
| Status | Accepted |
| Proposed | 2026-05-29 |
| Accepted | 2026-05-29 |
| Deciders | Ziyu |
| Supersedes | ADR-0003 (memory model: LTM categories, notes, `remember`/`forget`/`note` tools) |
| Superseded by | – |

## Context

[ADR-0003](0003-memory-system.md) shipped a working memory subsystem in
v0.0.6: working → STM → LTM compaction, notes, todos, and FTS5 recall. It
works, it is tested (56 unit tests), and the compaction machinery is sound.
But after living with it, two structural problems surface that no amount of
tuning fixes — they are baked into the data model.

### Problem 1 — one store, two incompatible kinds of memory

`long_term.md` ([MEMORY_SPEC §2.2](../MEMORY_SPEC.md)) holds six categories:

```
user / feedback / project / reference / fact   ← durable semantic knowledge
episodic                                        ← dated conversation summaries
```

These are two fundamentally different kinds of memory, in the cognitive
sense and in their correct management policy:

- **Episodic** — *what happened, when.* A timeline. It naturally decays:
  last month's conversation summary is less useful than today's. Managing it
  by a token budget and dropping the stale tail (tier-3) is exactly right.
- **Semantic / procedural** — *what I know, what the rules are, how to do X.*
  Atemporal. A fact like "the user dislikes hedging" or a rule like "never
  mock the database in tests" does **not** become less true because LTM grew
  past a token budget. Yet tier-3 manages it with a decay policy: when
  `long_term.md` exceeds `long_term_tokens`, an LLM is asked to drop or merge
  bullets to fit. We then bolt on special-casing (`src:explicit` bullets are
  "merge candidates only, never drop") to stop the decay policy from eating
  knowledge it should never touch.

That special-casing is the tell. We are running one policy (budget-driven
forgetting) over two populations, then exempting one population from the
policy. The honest design is two stores with two policies.

### Problem 2 — overlapping, oversized write surface

The same conflation produces a confusing tool surface. To write something
durable, the agent currently chooses among:

- `remember(content, category)` → a bullet in LTM,
- `note add(content)` → an entry in `notes.md` (the "never auto-deleted" store),
- and to remove: `forget(target)` → delete an LTM bullet, **plus** tier-3
  auto-forgetting, **plus** `memory compact_ltm` (manual tier-3).

`remember` vs `note` is a distinction the model gets wrong constantly,
because the real difference (does this decay?) is an artifact of which store
forgets — which is itself the artifact we are removing in Problem 1. And
there are three doors to "forget" (the redundancy that prompted this ADR).
Six memory tools (`recall` / `remember` / `note` / `todo` / `memory` /
`forget`) is the densest tool cluster in the runtime.

### What we want

1. **Two memory axes, each with its own correct policy:**
   - an **episodic** axis (the timeline) that auto-compacts and forgets —
     essentially today's working/STM/LTM, minus the semantic categories;
   - a **knowledge** axis (experience, knowledge, rules) that is
     **hierarchically organized, agent-curated, and deliberately edited** —
     never auto-deleted by a budget.
2. **A smaller, clearer tool surface** that falls out of (1): one way to
   write durable knowledge, not two; forgetting that means one thing per axis.
3. **`todo` removed from "memory" entirely** — it is task/workflow state, not
   memory, and belongs with `schedule` on the triggers/workflow side.

There is strong precedent for the knowledge axis: it is the file-based memory
model this project's author already dogfoods in Claude Code (a `MEMORY.md`
index pointing at one-fact-per-file markdown, linked with `[[name]]`,
recalled by relevance). ADR-0003 §"Compatibility" actually *flattened* that
model into LTM bullets and called the index "dropped." This ADR brings the
hierarchy back — as a first-class, agent-writable store — because the flat
model lost the structure that makes a growing knowledge base navigable. It is
also a writable cousin of the existing static `skills/*.md` mechanism.

## Decision

Split memory into **two axes** under `memory/`, plus move `todos` out.

```
~/.eonlet/eonlets/<id>/
├── memory/
│   ├── episodic/                  # AXIS 1 — timeline, auto-compacted
│   │   ├── short_term.md          #   STM (tier-1 target)
│   │   └── long_term.md           #   episodic summaries only (tier-2/3)
│   ├── knowledge/                 # AXIS 2 — curated, hierarchical, manual
│   │   ├── index.md               #   the MAP (injected every call)
│   │   ├── user.md
│   │   ├── projects/
│   │   │   └── auth-rewrite.md
│   │   ├── rules/
│   │   │   └── testing.md
│   │   └── ...
│   └── index.sqlite               # recall FTS5 over events + both axes
└── tasks/
    └── todos.jsonl                # MOVED OUT of memory/ (workflow state)
```

The SQLite event store remains the source of truth. Both axes are derived
state (rebuildable by replay), preserving ADR-0003 invariant I-S1.

### Axis 1 — Episodic memory (unchanged mechanism, narrowed scope)

Working → STM → LTM compaction is kept **as-is** (tier-1/2/3, watermark,
snapshot semantics, boundary selection — all of MEMORY_SPEC §3–§4 survive),
with one change: `long_term.md` now holds **only** the `episodic` category.
The five semantic categories (`user`/`feedback`/`project`/`reference`/`fact`)
are gone from LTM — they migrate to the knowledge axis (see Migration).

Consequences for tier-2 and tier-3:

- **Tier-2** (STM → LTM) now produces only episodic summaries. It no longer
  emits `ltm_additions` into semantic categories. Promotion of a *durable
  fact* surfaced during a conversation is no longer tier-2's job — it is a
  deliberate `knowledge.write` (the agent decides, like a human deciding to
  write something down in a notebook vs. just remembering the gist).
- **Tier-3** (LTM forgetting) loses all `src:explicit` special-casing. Every
  bullet in episodic LTM is now uniformly forgettable by recency/salience.
  The policy finally matches the population.

### Axis 2 — Knowledge base (new)

A directory tree of markdown files the agent curates deliberately. This is
where experience, knowledge, and rules live. Properties:

- **Hierarchical.** Files live at arbitrary relative paths under
  `knowledge/` (e.g. `projects/auth-rewrite.md`, `rules/testing.md`). The
  path *is* the organization; `rules/` is a convention, not special
  machinery.
- **Agent-curated, never auto-deleted.** No budget-driven forgetting. The
  agent adds, edits, and removes files on purpose. This subsumes the old
  `notes.md`, the semantic LTM categories, and the `remember` write path.
- **One file per coherent topic.** A file holds a single fact, rule, or
  cohesive cluster — matching the dogfooded one-fact-per-file discipline.

#### The index (`index.md`) — the retrieval backbone

Per the chosen retrieval design (option **c**: explicit map as backbone +
recall search as fallback):

- `knowledge/index.md` is an **agent-curated map**: one line per knowledge
  file, `- [Title](relative/path.md) — one-line relevance hook`. This is the
  same shape as the dogfooded `MEMORY.md` index.
- The runtime **injects `index.md` whole into every LLM call** (it is small —
  one line per file). The agent thus always knows *what it knows* and *where*,
  without the bodies bloating context.
- The agent reads a file's body **on demand** by path (`knowledge.open`),
  exactly like `load_skill`.
- When the map is insufficient ("did I write anything about X?"), `recall`
  does FTS5 full-text search across knowledge bodies (and episodic, and the
  event log). Recall returns paths the agent can then `knowledge.open`.
- Writing a file **must** keep `index.md` in sync. The `knowledge.write`
  action updates (or prompts the model to provide) the index line in the
  same operation, so the map never drifts from the tree. The runtime can
  also regenerate a fallback listing from the tree if `index.md` is missing.

Injected context per call therefore becomes:

```
<memory>
<knowledge_index>
{knowledge/index.md — the map, whole}
</knowledge_index>

<short_term>
{episodic/short_term.md}
</short_term>

<long_term>
{episodic/long_term.md — episodic summaries}
</long_term>
</memory>
```

Bodies of knowledge files are NOT injected — only the index map is. This is
the key budget win: a knowledge base can grow to hundreds of files while the
injected footprint stays one line each.

### Tool surface — 6 memory tools → 3 (+ todo moves out)

| Before (ADR-0003) | After (this ADR) |
|---|---|
| `recall` | `recall` (unchanged; now searches knowledge tree + episodic) |
| `remember` + `note` | `knowledge` — `open` / `write` / `edit` / `delete` / `move` / `list` |
| `forget` | folded: episodic→tier-3 auto; knowledge→`knowledge.delete` |
| `memory` (show/compact/compact_ltm/pause/resume) | `memory` (show/compact/pause/resume — `compact_ltm` kept under it) |
| `todo` | **moved out of memory** → `task` tool, beside `schedule` |

Net: the memory cluster drops from 6 tools to 3 (`recall`, `knowledge`,
`memory`), and the confusing `remember`-vs-`note` and triple-forget choices
disappear. `knowledge` is action-style, matching the existing `note`/`todo`
pattern:

```python
class KnowledgeArgs:
    action: Literal["open", "write", "edit", "delete", "move", "list"]
    path: str | None          # relative path under knowledge/, e.g. "rules/testing.md"
    content: str | None       # full body for write
    index_line: str | None    # the one-line index.md hook for write
    old_string: str | None    # for edit (string-replace, files.py semantics)
    new_string: str | None    # for edit
    new_path: str | None      # for move
```

- `open` / `list` are `read_only=True`.
- `write` / `edit` / `delete` / `move` are `destructive=True` (ask/yolo gate).

### `todo` → `task` (workflow side)

`todos.jsonl` moves from `memory/` to `tasks/`. The tool is renamed `task`
and conceptually grouped with `schedule` (both are "things the agent will
act on later," not "things the agent knows"). Storage format and state
machine (`pending`/`done`/`cancelled`, `due`, `tags`) are unchanged from
MEMORY_SPEC §2.4. Pending tasks still inject into context (now under a
`<tasks>` block sourced outside the memory preamble, or folded into the
trigger/workflow surface — see Open Questions). Memory-related todo events
(`mem_todo_*`) are renamed (`task_*`).

### `agent.yaml` schema changes

```yaml
memory:
  enabled: true
  compaction_model: "claude-haiku-4-5-20251001"

  episodic:                          # was: conversation:
    working_memory_tokens: 10000
    keep_recent_messages_min: 4
    short_term_tokens: 4000
    long_term_tokens: 8000
    auto_compact: true

  knowledge:
    inject_index: true               # inject index.md every call
    index_max_tokens: 2000           # warn if the map exceeds this
    warn_file_tokens: 4000           # warn on an oversized single file

tasks:                               # was: memory.todos
  inject_pending: true
  archive_done_after_days: 30
```

The legacy `memory.conversation`, `memory.notes`, and `memory.todos` blocks
are rejected by the config loader with a `ConfigError` (pre-1.0, single
author — no deprecation window, consistent with ADR-0003's update history).

### Event-store changes

| ADR-0003 event | Fate under this ADR |
|---|---|
| `mem_compacted` | kept (tier-1, episodic) |
| `mem_ltm_promoted` | kept (tier-2, episodic-only payload) |
| `mem_ltm_forgotten` | kept (tier-3 episodic; `cause:"forget"` variant retired with the `forget` tool) |
| `mem_remember` | **retired** → `kb_written` |
| `mem_note_added/updated/deleted` | **retired** → `kb_written` / `kb_deleted` |
| `mem_todo_*` | **renamed** → `task_added` / `task_done` / `task_cancelled` |
| `mem_recall_invoked`, `mem_paused`, `mem_resumed` | kept |
| — | **new** `kb_written` / `kb_deleted` / `kb_moved` |

### Migration

`eonlet memory migrate` is extended (or a v2 path added):

- Episodic LTM bullets (`## episodic`) → `episodic/long_term.md`.
- Each semantic LTM bullet (`user`/`feedback`/`project`/`reference`/`fact`)
  → a knowledge file. Default mapping: one file per category
  (`knowledge/user.md`, `knowledge/facts.md`, …) with the bullets as the
  body, plus generated `index.md` lines. The agent can later split/reorganize.
- `notes.md` entries → `knowledge/notes.md` (or split by heading), indexed.
- `todos.jsonl` → `tasks/todos.jsonl` (file move only).
- The Claude-Code-MEMORY.md import path (P6) maps **directly** onto the
  knowledge axis now — index → `index.md`, per-fact files → knowledge files —
  which is far more faithful than the ADR-0003 flatten-to-bullets approach.

## Consequences

### Positive

- Each memory axis is managed by the policy that actually fits it. The
  `src:explicit` "never drop" exemption — a workaround for the wrong policy —
  is deleted, not tuned.
- Knowledge scales without bloating context: bodies stay on disk, only the
  one-line-per-file map is injected. A knowledge base of hundreds of files
  costs roughly what today's notes block costs.
- The write surface collapses: one `knowledge.write` instead of
  `remember`-or-`note`; one meaning of "forget" per axis. Three fewer tools
  in the densest cluster.
- The knowledge axis is the author's dogfooded memory model, brought in-house
  faithfully (index + per-file + recall), rather than flattened.
- `todo` lands where it belongs conceptually, simplifying the mental model of
  "memory = what I know" vs "tasks = what I'll do."

### Negative

- This is a **normative rewrite** of a "complete" v0.0.6 subsystem. It touches
  MEMORY_SPEC (large rewrite), this ADR supersedes 0003's memory model,
  `src/eonlet/memory/`, injection, events, tools, CLI slash commands, the
  three bundled templates, and the migration tool. Test surface churns.
- A self-curated knowledge base can grow stale or sprawl. Mitigations: the
  injected index keeps it visible (a stale entry is in the agent's face every
  call), the `warn_file_tokens`/`index_max_tokens` warnings, and an *optional*
  future "knowledge gardening" pass (deferred — see Open Questions). Unlike
  episodic tier-3, gardening must never auto-delete substance.
- Index/tree drift is a new failure mode. Mitigated by making `knowledge.write`
  update the index atomically and by tree-fallback regeneration.
- More files on disk per eonlet; `export`/`import` must bundle the whole tree.

### Neutral

- Vector memory (v0.2, ADR-0003 forward-compat §) re-targets cleanly: embed
  knowledge files keyed by path instead of LTM bullet IDs. The `recall`-shaped
  retrieval contract is preserved; `memory_search` returns paths.
- `memory.enabled: false` escape hatch still disables both axes and falls back
  to replay-everything.

## Alternatives Considered

### A. Keep one LTM, just tune tier-3 to never touch semantic bullets

The status quo. Rejected: it is exactly the special-casing this ADR
diagnoses as the smell. Running two policies over two populations in one
file, with exemptions, is more complex than two stores, not less.

### B. Knowledge base as a flat single file (`knowledge.md`)

Rejected for the same reason ADR-0003 alt-B rejected a unified `memory.md`,
and because flat does not scale: a flat file must be injected whole (bloat)
or chunked (reinvents the index). The hierarchy + injected map is the point.

### C. Auto-inject relevant knowledge bodies via RAG each turn

Rejected, same grounds as ADR-0003 alt-A: opaque, unpredictable token cost,
and it bypasses "the model decides when to open a file," which keeps the
event log legible. The injected *index* gives awareness; `knowledge.open`
keeps retrieval an explicit, auditable act.

### D. Keep `todo` under memory

Rejected: it forces "memory" to mean both knowledge and pending actions,
which is the category error that made `remember`/`note`/`todo` feel
interchangeable. Tasks are workflow; they belong with `schedule`.

### E. Auto-generate `index.md` from the tree (no agent curation)

Tempting (no drift), but a generated listing carries only filenames, not the
*relevance hook* that makes a map useful ("— the auth rewrite is driven by
legal, not perf"). We keep the curated map as the backbone and use tree
generation only as a missing-file fallback.

## Resolved Decisions

These were the open questions; all four are resolved as of acceptance
(2026-05-29). The companion implementation plan
([plans/dual-axis-memory.md](../plans/dual-axis-memory.md)) sequences them.

1. **`<tasks>` injection home → sibling block, outside `<memory>`.** The
   runtime assembles a `<tasks>` block from `tasks/` and injects it as a
   sibling of `<memory>`, not inside the memory preamble. This keeps the
   "memory = what I know" vs "tasks = what I'll do" boundary that motivates
   moving `todo` out in the first place. `tasks.inject_pending` gates it.
2. **Knowledge gardening → deferred.** No automated merge/dedup pass in
   v0.1.x. Sprawl is held in check by the always-injected index (a stale
   entry is in the agent's face every call) plus the `index_max_tokens` /
   `warn_file_tokens` warnings. A conservative, never-auto-deleting gardening
   pass is a candidate for a later version.
3. **`knowledge` edit granularity → full-body `write` + string-replace
   `edit`.** `knowledge.write(path, content, index_line)` replaces a whole
   body; `knowledge.edit(path, old_string, new_string)` reuses `files.py`
   string-replace semantics for surgical changes. Both go through the
   destructive permission gate.
4. **Migration foldering → one file per old category.** `user`/`feedback`/
   `project`/`reference`/`fact` bullets migrate to
   `knowledge/{user,feedback,projects,reference,facts}.md` respectively, with
   generated `index.md` lines. Safe and reviewable; the agent reorganizes
   afterward at its own discretion.

## References

- [ADR-0003](0003-memory-system.md) — the memory model this supersedes
- [ADR-0002](0002-dynamic-triggers.md) — `schedule` tool + storage/IPC patterns `task` will share
- [MEMORY_SPEC.md](../MEMORY_SPEC.md) — to be rewritten for the dual-axis model
- [TOOL_SPEC.md](../TOOL_SPEC.md) — `knowledge` / `task` tool definitions
- [AGENT_CONFIG_SPEC.md](../AGENT_CONFIG_SPEC.md) — `memory.episodic` / `memory.knowledge` / `tasks` blocks
- `src/eonlet/memory/` — package to be restructured (episodic/ + knowledge/)
- `skills/*.md` + `load_skill` — the static precedent for on-demand, path-addressed knowledge

## Update history

- 2026-05-29: Initial proposal.
- 2026-05-29: All four open questions resolved (sibling `<tasks>` block;
  gardening deferred; full-body `write` + string-replace `edit`; one-file-
  per-category migration). Status → Accepted. Companion implementation plan
  added at [plans/dual-axis-memory.md](../plans/dual-axis-memory.md).
