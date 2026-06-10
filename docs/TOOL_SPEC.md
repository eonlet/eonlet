# Tool Specification

> Tools are how an agent affects the world. This document specifies the tool interface that all builtin and custom tools implement, and catalogs the builtin tools shipped with Eonlet v0.1.

## 1. The Tool Protocol

Every tool — builtin or custom — implements this Python protocol:

```python
from typing import Protocol
from pydantic import BaseModel

class Tool(Protocol):
    """An agent capability."""
    
    name: str
    """Unique name. snake_case. Used in tool calls."""
    
    description: str
    """LLM-visible description. The agent reads this to decide when to call."""
    
    input_schema: type[BaseModel]
    """Pydantic model defining the tool's arguments."""
    
    output_schema: type[BaseModel] | None
    """Pydantic model for typed outputs. None = plain string."""
    
    annotations: ToolAnnotations
    """Metadata about behavior — used by permission system."""
    
    async def __call__(
        self,
        args: BaseModel,
        ctx: ToolContext,
    ) -> ToolResult:
        """Execute the tool."""
        ...
```

## 2. `ToolAnnotations`

Behavioral metadata. The permission system uses these to decide whether to gate the call.

```python
class ToolAnnotations(BaseModel):
    read_only: bool = False
    """True if the tool does not mutate state outside read."""
    
    destructive: bool = False
    """True if effects are irreversible (delete file, send email, etc)."""
    
    network: bool = False
    """True if the tool makes network calls."""
    
    requires_confirmation: bool = False
    """If True, ask permission even in 'yolo' mode."""
    
    estimated_cost_usd: float | None = None
    """Rough cost estimate for budget accounting."""
    
    estimated_duration_s: float | None = None
    """Rough wall-clock estimate."""
    
    idempotent: bool = True
    """True if calling twice with same args has same effect."""
```

## 3. `ToolContext`

Passed to every tool call. Gives the tool access to runtime services.

```python
class ToolContext(BaseModel):
    eonlet_id: str
    """The eonlet's id (type.name)."""
    
    workspace: Path
    """The eonlet's workspace directory. Tools should write only here."""
    
    memory_dir: Path
    """The eonlet's memory directory (notes.md etc.). Read-only for tools by default."""
    
    permission_gate: PermissionGate
    """Used by tools that need to escalate permission mid-call."""
    
    cancel_token: anyio.CancelScope
    """Tools should respect cancellation."""
    
    emit_event: Callable[[str, dict], Awaitable[None]]
    """Custom subevents the tool wants to record."""
    
    budget: BudgetTracker
    """For tools that incur cost (e.g. web_search via paid API)."""
    
    trigger_context: TriggerContext | None
    """If the tool was called during a triggered run, includes trigger info."""
    
    env: dict[str, str]
    """Read-only view of resolved env vars."""
```

## 4. `ToolResult`

Tools return a `ToolResult`, which is what the LLM sees.

```python
class ToolResult(BaseModel):
    content: str | list[ContentBlock]
    """What the LLM sees. Plain string for simple results.
    list[ContentBlock] for mixed content (text + images, etc.)."""
    
    is_error: bool = False
    """True if the tool failed. Useful for error recovery."""
    
    structured_output: BaseModel | None = None
    """Optional typed output for downstream consumers (e.g. UI rendering)."""
    
    artifacts: list[Path] = []
    """Files the tool created in workspace, for the framework to track."""
```

## 5. Writing a Custom Tool

The minimal `@tool` decorator pattern:

```python
# tools/get_weather.py
from eonlet.tools import tool, ToolContext, ToolResult, ToolAnnotations
from pydantic import BaseModel, Field
import httpx

class GetWeatherArgs(BaseModel):
    location: str = Field(description="City name, e.g. 'Yokohama, JP'")
    units: str = Field(default="celsius", description="'celsius' or 'fahrenheit'")

@tool
class GetWeather:
    name = "get_weather"
    description = "Get current weather for a location."
    input_schema = GetWeatherArgs
    annotations = ToolAnnotations(read_only=True, network=True)
    
    async def __call__(self, args: GetWeatherArgs, ctx: ToolContext) -> ToolResult:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"https://api.example.com/weather",
                                 params={"q": args.location, "u": args.units})
            r.raise_for_status()
            data = r.json()
        return ToolResult(
            content=f"{args.location}: {data['temp']}°, {data['conditions']}"
        )
```

The framework discovers the tool by scanning `tools/` directory in the agent definition, importing each `.py` file, and registering all `@tool`-decorated classes.

### Tool development guidelines

- **One tool per file** is conventional but not required.
- **Input schema** should be self-documenting via `Field(description=...)`. LLMs read these.
- **Output** should be self-contained. Don't return "see message 5" — return the answer.
- **Errors** should be actionable. Tell the LLM what to fix.
- **Network calls** should respect `ctx.cancel_token`.
- **Workspace boundary**: file writes should go to `ctx.workspace` or `ctx.memory_dir`. Use `ctx.permission_gate` to escalate if you need to write elsewhere.
- **Idempotency**: prefer idempotent designs. The agent may retry.

### Anti-patterns

- ❌ Don't read or write outside workspace/memory without explicit permission
- ❌ Don't `print()` — use `ctx.emit_event` for visibility
- ❌ Don't catch every exception silently — let the framework see errors
- ❌ Don't keep state across calls — the next instance will lose it. Use the event store or memory files
- ❌ Don't depend on global modules being imported in a particular order — your tool must work in isolation

---

## 6. Builtin Tool Catalog

Plus `schedule` (one-off future trigger) — see TRIGGER_SPEC. The memory-suite tools (`recall` / `memory` / `knowledge` / `task`) reflect the dual-axis model (ADR-0005): the retired `notes_read` / `notes_append` / `remember` / `note` / `forget` / `todo` tools no longer exist.

### 6.1 `bash`

Run a shell command in the eonlet's workspace.

```yaml
input:
  command: string                         # the shell command
  timeout: int = 30                       # seconds
  cwd: string = "<workspace>"             # working dir (must be under workspace)
output: { stdout, stderr, return_code }
annotations: destructive (network depends on command)
permission: ask mode → always asks; yolo → hardcoded deny applies
```

Implementation notes:
- Spawned via `subprocess.run` with `shell=True`.
- Output truncated to 25k tokens.
- Working directory restricted to workspace.
- Inherits eonlet's resolved env vars.

### 6.2 `file_read`

Read a file's contents.

```yaml
input:
  path: string                            # absolute or relative
  offset: int = 0                         # for pagination
  limit: int = 2000                       # max lines per read
output: { content: string, total_lines: int, has_more: bool }
annotations: read_only
permission: read access checked; cannot read hardcoded-deny paths
```

### 6.3 `file_write`

Write or overwrite a file.

```yaml
input:
  path: string
  content: string
  mode: enum [overwrite, append] = "overwrite"
output: { bytes_written: int }
annotations: destructive
permission: write paths checked against deny list and workspace boundary
```

### 6.4 `file_edit`

SEARCH/REPLACE-style edit (more token-efficient than full rewrites).

```yaml
input:
  path: string
  search: string                          # exact text to find
  replace: string                         # text to replace with
  expected_count: int = 1                 # how many occurrences expected
output: { occurrences_replaced: int }
annotations: destructive
permission: same as file_write
```

Errors if `search` is not found exactly `expected_count` times. This avoids accidental over-replacement.

### 6.5 `glob`

Find files by glob pattern.

```yaml
input:
  pattern: string                         # e.g. "**/*.py"
  cwd: string = "<workspace>"
output: { paths: list[string] }
annotations: read_only
```

### 6.6 `grep`

Search file contents (uses ripgrep).

```yaml
input:
  pattern: string                         # regex
  path: string = "<workspace>"
  include: string = "*"                   # file glob
  context_lines: int = 0
output: { matches: list[{ file, line, content }], total: int }
annotations: read_only
```

### 6.7 `web_search`

Search the web. **Tavily** when `TAVILY_API_KEY` is set; otherwise a
**DuckDuckGo HTML** scrape (fragile zero-config fallback). Two paths,
no abstraction — see [ADR-0004](adr/0004-web-tools.md).

```yaml
input:
  query: string
  max_results: int = 5                       # 1–20
  include_raw_content: bool = false          # Tavily only; ignored on DDG
output:
  provider: "tavily" | "ddg"
  query: string
  answer: string | null                      # Tavily AI summary if any
  warnings: list[str]                        # e.g. "raw_content_unavailable_on_ddg"
  results: list[{ title, url, snippet, raw_content?, published_at? }]
annotations: read_only, network
```

Provider selection is by env-var presence — there is no `provider="…"`
argument. To force one backend, unset / set `TAVILY_API_KEY` accordingly.

Emits `WEB_SEARCH_PERFORMED` event (summary only; full hits in the
normal `TOOL_RESULT`).

**When the built-in isn't enough.**

- **Brave / Google CSE / Serper** — write a ~60-line custom tool under
  your agent's `tools/` directory that calls the relevant SDK and
  returns the same `{ results: list[...] }` shape.
- **More providers, smarter fallback chains** — wait for v0.2 MCP, or
  mount an MCP search server. `TODO(v0.2): link to mcp-server-fetch /
  search MCP servers once they ship.`

### 6.8 `web_fetch`

Fetch a URL and return its main content as **markdown**. HTML pages are
extracted via `trafilatura`; plain text / JSON pass through. PDFs, RSS,
JavaScript-rendered pages, anti-bot evasion are **out of scope** — see
the extensibility note below.

```yaml
input:
  url: string                                # http or https only
  max_tokens: int = 4000                     # 200–20000; output window per call
  offset_tokens: int = 0                     # chain pages with next_offset
output:
  content: string                            # markdown body
  structured_output:
    url: string                              # final URL after redirects
    title: string | null
    content_type: string
    metadata: { author?, date?, language?, sitename?, hostname?, … }
    offset_tokens: int
    total_tokens: int
    truncated: bool
    next_offset: int | null
annotations: read_only, network
```

The shared `HTTPFetcher` enforces:

- **SSRF guard** — refuses loopback, link-local, RFC1918, CGNAT,
  multicast, and cloud-metadata endpoints (AWS / GCP / Azure / OCI /
  Alibaba IMDS, including 169.254.169.254). Escape hatch:
  `agent.yaml: web.fetch.allow_private_networks: true`.
- **Scheme allow-list** — `http`, `https` only.
- **Retry** — 3 attempts on transport errors and 5xx, backoff 0.5/1/2s.
  No retry on 4xx.
- **Size cap** — streaming response aborts when raw bytes exceed
  `web.fetch.max_bytes` (default 10 MB).
- **Stable User-Agent** — `Eonlet/<version> (+https://eonlet.dev)`,
  overridable via `web.fetch.user_agent`.

Pagination is token-based (≈ 4 chars / token) — feed `next_offset` back
in as `offset_tokens` to read the next slice.

Emits `WEB_FETCH_PERFORMED` event (summary only; full body in the
normal `TOOL_RESULT`).

**When the built-in isn't enough.**

- **PDFs.** The tool returns `is_error=true` with a "use a custom tool
  or MCP server" message. Drop a `pypdf`-based extractor into your
  agent's `tools/` directory, or wait for v0.2 MCP. `TODO(v0.2): link
  to mcp-server-pdf once MCP integration lands.`
- **RSS / Atom / JSON Feed.** Feed parsing is a polling concern, not a
  fetch concern; it belongs in a per-agent custom tool. The bundled
  `x-digest` template ships
  [`tools/feed_read.py`](../src/eonlet/templates/x-digest/tools/feed_read.py)
  as the canonical extensibility example (~30 LOC `feedparser` wrapper).
- **JavaScript-rendered pages, headless rendering, anti-bot evasion.**
  Out of scope for v0.1. Mount a hosted scraper via MCP at v0.2, or
  write a custom tool calling Playwright / Crawl4AI / FireCrawl.
- **Brave / Google CSE / specialist scrapers in general.** Same pattern
  — Eonlet is a *runtime*, not a competitor to Tavily / Crawl4AI /
  FireCrawl. The 60-LOC custom-tool route is the supported path.

### 6.9 `recall`

Search the event log and memory stores when the compressed context isn't enough (MEMORY_SPEC §5.1). FTS5 over the event log plus direct scans of the knowledge tree / tasks.

```yaml
input:
  mode: "by_keyword" | "by_date" | "by_date_range" | "around_event"
  query: string | null                    # for by_keyword
  date: string | null                     # YYYY-MM-DD (UTC), for by_date
  include: ["events" | "knowledge" | "tasks"] = ["events"]
output: { rendered hits; knowledge hits return file paths to open }
annotations: read_only
```

### 6.10 `memory`

Inspect and control the episodic memory subsystem (MEMORY_SPEC §4, §5.5).

```yaml
input:
  action: "show" | "compact" | "compact_ltm" | "propose_compact" | "pause" | "resume"
  store: "stm" | "ltm" | "all" = "all"    # for action=show
  boundary_event_id: int | null            # for action=propose_compact
  reason: string | null                    # for action=propose_compact
annotations: not destructive (compaction is bounded to memory/)
```

`propose_compact` is the agent's only path to compaction (ADR-0006): it proposes
folding the conversation up to `boundary_event_id` into STM and **blocks for the
user's consent** before acting (auto-approved under `yolo`). It is gated by the
`episodic.propose_*` floor/cooldown config and is a no-op in headless/cron runs.
`compact` (full, clean-slate) is reachable only from the user's `/compact`, not
as an agent action.

### 6.11 `knowledge`

The single durable-write surface — the curated, hierarchical knowledge base (ADR-0005). Its `index.md` map is always in context; bodies are opened on demand. Never auto-deleted.

```yaml
input:
  action: "open" | "list" | "write" | "edit" | "delete" | "move"
  path: string | null                     # relative under knowledge/, e.g. "rules/testing.md"
  content: string | null                  # full body for write
  index_line: string | null               # one-line map hook for write/move
  old_string / new_string: string | null  # for edit (string-replace)
  new_path: string | null                 # for move
output: { path / list / body depending on action }
annotations: destructive (write/edit/delete/move); open/list are read_only-equivalent
```

Paths are confined to the knowledge tree: `..`, absolute paths, and the reserved `index.md` are rejected (`KnowledgePathError`).

### 6.12 `task`

Hierarchical, event-sourced workflow state — not memory (ADR-0005/0007). The
forest is a fold of the task event log (no `todos.jsonl`); pending **leaves**
plus suspended tasks inject as a `<tasks>` block on each chat turn (task-scoped
runs see no forest-wide backlog). Full model:
[TASK_SPEC](TASK_SPEC.md).

```yaml
input:
  action: "add" | "list" | "done" | "cancel" | "resume" | "update" | "delete"
  content: string | null                  # for add/update
  id: string | null                       # for done/cancel/update/delete (defaults to
                                          #   the current task inside a scheduled run);
                                          #   required for resume
  goal: string | null                     # durable objective (used on resume)
  parent_id: string | null               # for add → create a subtask (tree)
  priority: int | null                    # higher runs first (default 0)
  result: string | null                   # for done → outcome summary (REQUIRED when a
                                          #   task-scoped run finishes its own task)
  schedule: string | null                 # for add → cron; hatches a fresh instance per fire
  timezone: string | null                 # required with schedule (IANA)
  due: string | null                      # optional ISO-8601
  tags: [string]
  status: "pending"|"active"|"suspended"|"blocked"|"done"|"cancelled"|"all" = "pending"  # list
annotations: destructive
```

Inside a scheduled task-run the agent calls `task(done)` / `task(add …)` without
restating the id (they default to the *current task*); `add` without a parent is
a subtask of it (the decomposition signal). `done` for the run's own task
requires a non-empty `result` — it is the only payload that flows up the tree.
`resume` re-queues a suspended task (→ pending) so the scheduler picks it up.
Creation respects the `tasks.scheduling` depth/fan-out caps.

### 6.13 `send_email`

Send an email via configured SMTP.

```yaml
input:
  subject: string
  body: string                            # markdown
  to: string | null = null                # default from env $EMAIL_TO
  reply_to: string | null = null
output: { sent: bool, message_id: string }
annotations: destructive, network
permission: in `ask` mode, always asks; in `yolo`, allowed
```

Requires env vars: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`.
Body Markdown is rendered to HTML with a fallback plaintext part.

### 6.14 `sleep`

Pause execution (useful for retry backoffs in scheduled agents).

```yaml
input:
  seconds: float
output: { slept_for: float }
annotations: read_only (no side effects)
```

Capped at 5 minutes per call to prevent runaway. For longer waits, use the trigger system.

### 6.15 `load_skill`

Load a skill's full content into the conversation.

```yaml
input:
  name: string                            # skill filename without .md
output: { content: string, skill_name: string }
annotations: read_only
```

Skills are Markdown files in the agent's `skills/` directory. They're listed in the system prompt by name + description; the agent calls `load_skill` to fetch the full body when relevant.

---

## 7. Permission Gate Semantics

When the agent calls a tool:

1. **Hardcoded deny check.** If the call matches a hardcoded deny pattern → denied. (Cannot be overridden.)
2. **Extra deny check.** If matches a pattern in `agent.yaml.permissions.extra_deny` → denied.
3. **Mode check.**
   - **`yolo` mode** → allowed (unless tool has `requires_confirmation: true`)
   - **`ask` mode**:
     - If `annotations.destructive: false` → allowed
     - If `annotations.destructive: true`:
       - If a session is attached → prompt user; user's answer decides
       - If no session is attached → denied with informative error

Every decision (allow or deny, by what rule) is recorded as a `permission_*` event.

---

## 8. Provider-Specific Tool Implementations

Some builtin tools (notably `web_search`) have a default provider but support alternatives via env. Configuration is detected automatically:

| Tool | Default | Env keys |
|---|---|---|
| `web_search` | Tavily | `TAVILY_API_KEY` set → Tavily |
|   | DuckDuckGo (free fallback) | No key set → DuckDuckGo via duckduckgo-search |
|   | Serper | `SERPER_API_KEY` set → Serper |
| `send_email` | SMTP | `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` |

v0.2 will add a config-driven selection.

---

## 9. Future Tool Sources

- **v0.2:** MCP server tools. `mcp.json` declares servers; their tools are imported and wrapped in the Tool protocol.
- **v0.3:** Code execution mode. Tools are exposed as a sandbox-callable Python API; the agent writes code rather than discrete tool calls.
- **v0.4:** Peer tools. Tools that send messages to other eonlets (`peer.query`, `peer.send`).
- **v0.6 (Phase C — Teams):** Team-coordination tools. `team.delegate(member, task)` for leaders, `team.report(result)` for members, `team.notes_read` / `team.notes_append` for shared team memory.
- **v0.8 (Phase D — Organizations):** Cross-team routing. `org.route(target_team, task)` for routing requests up to common ancestor and back down.

See [`concepts/teams-and-organizations.md`](concepts/teams-and-organizations.md) for the conceptual model behind these tools.

---

## 10. Tool Versioning

Builtin tools are versioned with the runtime. The agent's prompt receives the full tool catalog at startup; if the schema changes between runtime versions, the agent sees the new schema. Definitions don't need to declare tool versions.

Custom tools are versioned by the agent definition's `metadata.version` — if you change a tool's behavior, bump the definition version.
