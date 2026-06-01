# Plan — Dual-Axis Memory (Episodic + Knowledge)

> Companion to [ADR-0005](../adr/0005-dual-axis-memory.md). The ADR fixes the
> design and resolves the four open questions; this plan sequences the
> implementation into reviewable milestones and defines what "done" looks
> like at each step.

| Field | Value |
|---|---|
| Owner | Ziyu |
| Started | 2026-05-29 |
| Target | v0.0.8 (pre-v0.1.0 normative rewrite) |
| Status | **Shipped 2026-05-29 (v0.0.8).** M1–M4 landed; migration deliberately dropped (pre-alpha, no cross-version migration). |
| Estimated effort | 4–6 working days, single committer |
| Supersedes (impl.) | the v0.0.6 memory subsystem (ADR-0003) |

## Why this plan exists

ADR-0005 is a **normative rewrite** of a subsystem that already shipped
"complete" in v0.0.6. That makes sequencing matter more than usual: a naive
big-bang rewrite would red-bar the whole test suite for days and make review
impossible. This plan splits the change into four milestones that each leave
the tree green, ordered so the *additive* work (the knowledge axis) lands and
proves out before the *destructive* work (narrowing episodic, retiring tools,
moving tasks).

## Guiding principles

1. **Additive before destructive.** Build the knowledge axis as a new,
   independently-tested package (M1) before touching the existing LTM
   categories, tools, or tasks. Nothing in M1 breaks an existing test.
2. **One axis, one policy — enforced by structure, not by exemptions.** The
   point of the rewrite is to delete the `src:explicit` "never drop"
   special-casing (MEMORY_SPEC §5). When M2 lands, that code is *removed*, not
   reconfigured. If a test still references `src:explicit`, M2 isn't done.
3. **Pre-1.0, no deprecation window.** Single author, no external users. The
   config loader *rejects* legacy `memory.conversation` / `memory.notes` /
   `memory.todos` blocks with a `ConfigError` — it does not silently migrate
   them. Same stance ADR-0003 took on its own renames.
4. **Event log stays the source of truth.** Both axes remain replay-derived
   (ADR-0003 invariant I-S1). New events (`kb_written` / `kb_deleted` /
   `kb_moved`) and renames (`mem_todo_*` → `task_*`) are append-only; no event
   rows are ever rewritten.
5. **Docs land in the same PR as the code they describe.** MEMORY_SPEC is the
   normative source; it cannot trail the implementation into a later PR.

## Milestone map

```
M1  Knowledge axis (new package + knowledge tool + index injection)   (≈ 1.5 day)
M2  Episodic narrowing (strip semantic LTM, retire remember/note/forget) (≈ 1.5 day)
M3  Tasks move-out (todo → task, tasks/, sibling <tasks> injection)    (≈ 1 day)
M4  Migration v2 + spec/template/doc rewrite + version bump           (≈ 1 day)
```

Four milestones, four PRs.

### Suggested PR shape

```
PR1 (M1) — feat(memory): knowledge axis — curated hierarchical store
  - New: src/eonlet/memory/knowledge.py (KnowledgeStore: tree + index.md sync)
  - New: src/eonlet/tools/builtin/knowledge.py (open/write/edit/delete/move/list)
  - injection.py: inject knowledge/index.md whole; bodies on demand
  - config.py: memory.knowledge block (inject_index, index_max_tokens, warn_file_tokens)
  - events.py: KB_WRITTEN / KB_DELETED / KB_MOVED
  - Tests: tests/unit/memory/test_knowledge_store.py, test_tools_knowledge.py
  - No existing test changed; remember/note/LTM all still work

PR2 (M2) — refactor(memory)!: narrow episodic LTM to one population
  - memory/ episodic scope: long_term.md holds only `episodic`
  - tier2.py: emit episodic summaries only (no semantic ltm_additions)
  - tier3.py: delete src:explicit exemption — uniform recency/salience policy
  - Remove tools: remember.py, note.py, forget.py (folded into knowledge / tier-3)
  - Remove memory/notes.py + NotesStore; recall.py now indexes knowledge tree
  - injection.py: <memory> = knowledge_index + short_term + long_term(episodic)
  - events.py: retire mem_remember / mem_note_*; ltm_forgotten loses `forget` cause
  - Tests: delete/rewrite test_remember_forget.py, test_notes_store.py; adjust tiers

PR3 (M3) — refactor!: todo → task, out of memory
  - Move memory/todos.py → tasks/store.py (or src/eonlet/tasks/)
  - Rename tool todo → task (tools/builtin/task.py), beside schedule
  - todos.jsonl path: memory/ → tasks/
  - Sibling <tasks> injection assembled by runtime, OUTSIDE <memory>
  - config.py: top-level `tasks` block (inject_pending, archive_done_after_days)
  - events.py: mem_todo_* → task_added / task_done / task_cancelled
  - Tests: rename test_todos_store.py, test_tools_note_todo.py → task equivalents

PR4 (M4) — feat(memory): migration v2 + normative doc/spec/template rewrite
  - memory/migrate.py: v2 path (semantic LTM → one knowledge file per category;
    notes → knowledge; todos → tasks/; Claude-Code MEMORY.md → knowledge tree)
  - MEMORY_SPEC.md: large rewrite for dual-axis model
  - AGENT_CONFIG_SPEC.md §8, TOOL_SPEC.md, SECURITY.md, CLI_REFERENCE.md
  - Three bundled templates: agent.yaml memory.* → episodic/knowledge/tasks
  - CHANGELOG.md, CLAUDE.md version history → v0.0.8
  - Tests: test_migrate.py v2 cases
```

---

## M1 — Knowledge axis (≈ 1.5 day)

### Scope

Build the curated, hierarchical knowledge store **alongside** the existing
LTM — purely additive. Nothing here removes a category or a tool.

- `src/eonlet/memory/knowledge.py` — `KnowledgeStore`:
  - root at `memory/knowledge/`; files at arbitrary relative paths.
  - `open(path)` / `write(path, content, index_line)` / `edit(path, old, new)`
    / `delete(path)` / `move(src, dst)` / `list()`.
  - Path safety: reject `..`, absolute paths, symlink escapes — confined to
    the knowledge root (reuse the `files.py` confinement helper if one exists).
  - **Atomic writes only** — `storage.atomic_write_text()` (invariant 3).
  - `index.md` kept in sync on every mutating op: `write` updates/inserts the
    `- [Title](path) — hook` line; `delete`/`move` adjust it. A tree-walk
    fallback regenerates a bare listing if `index.md` is missing.
- `src/eonlet/tools/builtin/knowledge.py` — the `knowledge` action tool
  (`KnowledgeArgs` per ADR). `open`/`list` are `read_only=True`;
  `write`/`edit`/`delete`/`move` are `destructive=True`.
- `memory/injection.py` — inject `knowledge/index.md` whole inside `<memory>`
  as `<knowledge_index>`. Bodies are **never** injected.
- `config.py` — `memory.knowledge` block: `inject_index: bool`,
  `index_max_tokens: int` (warn), `warn_file_tokens: int` (warn).
- `runtime/events.py` — `KB_WRITTEN`, `KB_DELETED`, `KB_MOVED` (append to the
  `EventKind` enum; bump the variant count note in CLAUDE.md in M4).

### Done when

- `tests/unit/memory/test_knowledge_store.py` covers CRUD, index sync,
  path-escape rejection, atomic-write behavior, tree-fallback regeneration.
- `tests/unit/memory/test_tools_knowledge.py` covers the tool surface +
  permission flags + emitted events.
- Knowledge index injects into the preamble (extend `test_injection.py`).
- `mypy src` clean, `ruff check .` clean.
- **Every pre-existing test still passes** — remember/note/LTM untouched.

---

## M2 — Episodic narrowing (≈ 1.5 day)

### Scope

The destructive heart of the ADR. Narrow LTM to a single population and
delete the policy exemption that motivated the whole rewrite.

- `memory/ltm.py` — `long_term.md` now holds **only** the `episodic`
  category. The five semantic categories are removed from the store model.
- `memory/tier2.py` — STM → LTM produces only episodic summaries. Drop the
  `ltm_additions` path into semantic categories.
- `memory/tier3.py` — **delete the `src:explicit` "never drop" exemption.**
  Every episodic bullet is uniformly forgettable by recency/salience. This is
  the single most important diff in the rewrite (principle 2).
- Remove tools: `tools/builtin/remember.py`, `note.py`, `forget.py`. Their
  jobs are now `knowledge.write` (durable knowledge) and tier-3 auto-forget
  (episodic decay) + `knowledge.delete` (knowledge removal).
- Remove `memory/notes.py` + `NotesStore` (subsumed by the knowledge axis).
- `recall` tool — the `notes` scope is replaced by a `knowledge` scope that
  **scans the knowledge tree directly** (the same direct-store pattern the
  `notes`/`todos` scopes already used — `memory_fts` was declared but never
  populated). Hits return file paths the agent can `knowledge.open`. FTS5 over
  knowledge bodies stays deferred (alongside `memory_fts` population), so this
  is covered in `test_recall_tool.py`, not `test_recall_index.py`.
- `memory/injection.py` — finalize the `<memory>` preamble:
  `<knowledge_index>` + `<short_term>` + `<long_term>` (episodic only).
- `runtime/events.py` — retire `mem_remember`, `mem_note_added/updated/deleted`;
  `mem_ltm_forgotten` keeps the recency/salience cause, retires `cause:"forget"`.

### Done when

- `test_remember_forget.py` and `test_notes_store.py` are deleted; nothing in
  the suite references `remember`, `note`, `forget`, `NotesStore`, or
  `src:explicit`.
- `test_tier2.py` / `test_tier3.py` rewritten for episodic-only behavior;
  tier-3 test asserts uniform forgettability (no exempt bullets).
- `test_recall_index.py` covers searching across the knowledge tree.
- Grep gate: `rg "src:explicit|NotesStore|def remember|def note|def forget"
  src/ tests/` returns nothing.
- `mypy src` + `ruff check .` clean; full suite green.

---

## M3 — Tasks move-out (≈ 1 day)

### Scope

Tasks are workflow, not memory. Move them beside `schedule`.

- Move `memory/todos.py` → `src/eonlet/tasks/store.py` (new top-level
  `tasks` package) or `triggers/`-adjacent — match wherever `schedule`'s
  storage lives (`triggers/dynamic_store.py` is the precedent; see ADR-0002).
- `todos.jsonl` path: `memory/` → `tasks/`. Storage format and state machine
  (`pending`/`done`/`cancelled`, `due`, `tags`) are **unchanged**.
- Rename tool `todo` → `task` (`tools/builtin/task.py`); actions
  `add`/`done`/`cancel`/`list` unchanged.
- Sibling `<tasks>` injection: the runtime assembles a `<tasks>` block from
  `tasks/`, injected **outside** `<memory>` (resolved decision 1). Gated by
  `tasks.inject_pending`.
- `config.py` — top-level `tasks` block: `inject_pending: bool`,
  `archive_done_after_days: int`. (Was `memory.todos`.)
- `runtime/events.py` — `mem_todo_*` → `task_added` / `task_done` /
  `task_cancelled`.

### Done when

- `test_todos_store.py` / `test_tools_note_todo.py` renamed to task
  equivalents and passing.
- A runtime injection test asserts `<tasks>` is a sibling of `<memory>`, not
  nested inside it.
- No code path reads or writes `memory/todos.jsonl` anymore.
- Full suite green; mypy + ruff clean.

---

## M4 — Migration v2 + normative doc/spec rewrite (≈ 1 day)

### Scope

Make existing eonlets migratable, then make the docs match reality.

- `memory/migrate.py` — v2 path (extend `eonlet memory migrate`):
  - `## episodic` LTM bullets → `episodic/long_term.md`.
  - Each semantic category → one knowledge file
    (`knowledge/{user,feedback,projects,reference,facts}.md`) with bullets as
    body + generated `index.md` lines (resolved decision 4).
  - `notes.md` entries → `knowledge/notes.md` (or split by heading), indexed.
  - `todos.jsonl` → `tasks/todos.jsonl` (file move).
  - Claude-Code `MEMORY.md` import (the old P6 path) now maps **directly**:
    index → `index.md`, per-fact files → knowledge files. Far more faithful
    than ADR-0003's flatten-to-bullets.
- **Doc rewrite (normative):**
  - `MEMORY_SPEC.md` — large rewrite for the dual-axis model (the big one).
  - `AGENT_CONFIG_SPEC.md` §8 — `memory.episodic` / `memory.knowledge` /
    top-level `tasks` blocks; reject legacy blocks.
  - `TOOL_SPEC.md` — `knowledge` + `task` tool definitions; remove
    `remember`/`note`/`todo`/`forget`.
  - `SECURITY.md` — knowledge path-confinement note (no escape from
    `knowledge/`).
  - `CLI_REFERENCE.md` — `eonlet memory migrate` v2 behavior.
- **Templates:** update `assistant`, `x-digest`, `portfolio` `agent.yaml`
  memory blocks to the new schema; seed a small `knowledge/index.md` +
  example file where it illustrates the model.
- **Version bump:** `CHANGELOG.md` + `CLAUDE.md` version history → **v0.0.8**;
  update the `EventKind` variant count (39 → new total after add/retire/rename)
  and the package-layout / tool-catalog sections of CLAUDE.md.

### Done when

- `test_migrate.py` covers each v2 mapping (semantic→files, notes→knowledge,
  todos→tasks, Claude-Code MEMORY.md→tree) on a fixture legacy dir.
- All three templates load (`runtime/definition.py`) and pass their smoke
  tests under the new schema.
- Config loader raises `ConfigError` on `memory.conversation` /
  `memory.notes` / `memory.todos`.
- Docs grep clean: no surviving reference to `remember`/`note`/`forget`/`todo`
  tools or semantic LTM categories in normative docs.
- CI gate `--cov-fail-under=70` still met; mypy + ruff clean.

---

## Cross-cutting checklist (all milestones)

- [ ] Atomic writes for every memory/knowledge file (invariant 3).
- [ ] anyio only; structlog only; no `print()` (coding standards).
- [ ] Project exception hierarchy — new `KnowledgeError` / path-escape error
      in `errors.py` rather than bare raises.
- [ ] Events append-only; replay still reconstructs both axes (invariant 1).
- [ ] `memory.enabled: false` still disables both axes (replay-everything).
- [ ] `export`/`import` bundles the whole `knowledge/` tree + `tasks/`
      (regression: the archive walk must include new dirs).

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Big-bang rewrite red-bars the suite for days | Additive-first ordering; each milestone leaves tree green |
| Index/tree drift (new failure mode) | `knowledge.write` updates index atomically; tree-walk fallback regen |
| `export`/`import` silently drops the new dirs | Explicit regression test on the archive walk in M3/M4 |
| Knowledge base sprawl over time | Injected index keeps it visible; `warn_file_tokens` / `index_max_tokens`; gardening deferred per ADR |
| Path-escape via crafted `knowledge` path | Confinement check + symlink resolution; covered in M1 tests |

## References

- [ADR-0005](../adr/0005-dual-axis-memory.md) — the design this plan implements
- [ADR-0003](../adr/0003-memory-system.md) — the memory model being superseded
- [ADR-0002](../adr/0002-dynamic-triggers.md) — `schedule` storage/IPC pattern `task` reuses
- [MEMORY_SPEC.md](../MEMORY_SPEC.md) — rewritten in M4
- `src/eonlet/memory/` — restructured across M1–M3
- `skills/*.md` + `load_skill` — static precedent for on-demand, path-addressed knowledge

## Update history

- 2026-05-29: Initial plan; companion to ADR-0005 (Accepted same day).
- 2026-05-29: **M1 landed.** New `src/eonlet/memory/knowledge.py`
  (`KnowledgeStore`), `tools/builtin/knowledge.py` (`knowledge` tool),
  `memory.knowledge` config block, `<knowledge_index>` injection, and
  `KB_WRITTEN`/`KB_DELETED`/`KB_MOVED` events + `KnowledgeError`/
  `KnowledgePathError`. 33 new tests (`test_knowledge_store.py`,
  `test_tools_knowledge.py`, extended `test_injection.py`). Full suite 560
  passed; ruff + mypy clean. No pre-existing test changed (additive).
- 2026-05-29: **M2 landed.** Episodic narrowing — `LTM CATEGORIES` →
  `("episodic",)`, tier-2 emits episodic-only, tier-3 prompt drops the
  `src:explicit` exemption (uniform recency/salience). Retired `notes.py` +
  `NotesStore`, the `remember`/`note`/`forget` tools, the `mem_remember`/
  `mem_note_*` events, the `memory.notes` config block, and LTM's
  `find_bullets`/`delete_bullets` (forget-only dead code). `recall` `notes`
  scope → `knowledge` scope (direct tree scan). Threaded through the worker
  IPC (`memory.knowledge.*`), CLI (`/knowledge` replaces `/note`; `/memory
  show knowledge`), and `status` (knowledge tier row). `mem_ltm_forgotten`
  cause is now `tier3`-only. Migration's LTM round-trip xfailed pending the M4
  rewrite to the knowledge axis. Grep gate clean. Full suite 528 passed,
  1 xfailed; ruff + mypy clean.
- 2026-05-29: **M3 landed.** Tasks moved out of memory into a new top-level
  `src/eonlet/tasks/` package (`TaskStore`, `TasksConfig`, `mint_task_id`),
  storing `tasks/todos.jsonl` (sibling of `memory/`, new `paths.tasks_dir`).
  `todo` tool → `task` tool (adds a `cancel` action); `memory/todos.py` and
  `memory/ids.py` deleted. `mem_todo_*` events → `task_added`/`task_updated`/
  `task_deleted`. Pending tasks now inject as a sibling `<tasks>` block
  (`build_tasks_block`, gated by `tasks.inject_pending`) outside `<memory>` —
  `ToolContext`/`AgentRuntime` gained `tasks_dir`. Top-level `tasks:` config
  block added (was `memory.todos`, now rejected). Threaded through worker IPC
  (`task.*`), CLI (`/task` replaces `/todo`), `memory show` (todos dropped),
  recall (`tasks` scope), and `status` (reads `tasks/`). New
  `tests/unit/tasks/` package (store + tool tests). Full suite 533 passed,
  1 xfailed; ruff + mypy clean.
- 2026-05-29: **M4 landed; migration dropped.** Per a scope call, the
  migration deliverable was cut entirely — pre-alpha has no cross-version
  migration burden, so `memory/migrate.py`, the `eonlet memory migrate`
  command, and their tests were **removed** (the M2 xfail is gone). Also:
  renamed `memory.conversation` → `memory.episodic` (`EpisodicMemoryConfig`);
  updated the three bundled templates (`knowledge`+`task` tools, new config
  blocks, rewritten `assistant` memory guidance); rewrote the normative docs
  (`TOOL_SPEC` §6 catalog, `AGENT_CONFIG_SPEC` §8 + new §8.1 `tasks`,
  `SECURITY` §2.3 knowledge confinement, `CLI_REFERENCE` slash commands,
  `MEMORY_SPEC` superseding banner) and `CLAUDE.md`/`CHANGELOG.md` to v0.0.8.
  `EventKind` settles at 36 variants. Full suite **521 passed**; ruff + mypy
  clean.
