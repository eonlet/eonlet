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

## 6. Builtin Tool Catalog (MVP — 13 tools)

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

Search the web. Backend configurable (Tavily by default; can swap for DuckDuckGo, Serper, etc.).

```yaml
input:
  query: string
  max_results: int = 5
output: { results: list[{ title, url, snippet }] }
annotations: read_only, network
```

Configured via env vars: `TAVILY_API_KEY` (or alternative).

### 6.8 `web_fetch`

Fetch a URL, return as Markdown.

```yaml
input:
  url: string
  prompt: string = ""                     # optional summarization hint
output: { content: string, title: string }
annotations: read_only, network
```

Uses `httpx` + `readability-lxml` for cleanup. Strips ads / navigation when possible.

### 6.9 `notes_read`

Read from the eonlet's memory markdown files.

```yaml
input:
  file: string = "notes.md"               # restricted to memory_dir
output: { content: string }
annotations: read_only
```

Only files declared in `agent.yaml.memory.notes_files` are accessible.

### 6.10 `notes_append`

Append to a memory markdown file.

```yaml
input:
  file: string
  content: string
  with_timestamp: bool = true             # prefixes "## YYYY-MM-DD HH:MM"
output: { bytes_appended: int }
annotations: destructive (but bounded to memory/)
```

### 6.11 `send_email`

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

### 6.12 `sleep`

Pause execution (useful for retry backoffs in scheduled agents).

```yaml
input:
  seconds: float
output: { slept_for: float }
annotations: read_only (no side effects)
```

Capped at 5 minutes per call to prevent runaway. For longer waits, use the trigger system.

### 6.13 `load_skill`

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
