# ADR-0004: Web Tools — Minimal Built-in, Extensible via Skills and MCP

| Field | Value |
|---|---|
| Status | Proposed |
| Date | 2026-05-26 |
| Deciders | Ziyu |
| Supersedes | – |
| Superseded by | – |

## Context

`web_search` and `web_fetch` shipped in v0.0.2 as placeholders (see
`src/eonlet/tools/builtin/web.py`):

- `web_search` calls Tavily when `TAVILY_API_KEY` is set, otherwise scrapes
  `duckduckgo.com/html` with regex.
- `web_fetch` runs `httpx.get`, then strips HTML tags with a one-line regex.
  Output is plain text — hyperlinks, lists, tables, code blocks collapsed
  into prose. The `prompt` argument exists in the signature but is unused.
  Content is hard-truncated at 50 KB.

For Eonlet to deliver on its core promise — **a local-first agent that does
its own research** — these tools need a real upgrade. The first draft of
this ADR proposed a "production" scope: multi-backend provider abstraction
(Tavily / Brave / Google CSE / DDG), structured extractor pipeline (HTML /
PDF / RSS / passthrough), full SSRF + retry + pagination layer.

That scope was wrong. It treated Eonlet's web tools as competitors to
specialist projects like Tavily, Crawl4AI, and FireCrawl — projects with
years of engineering invested in exactly this surface. Eonlet is a
**runtime** (per [MANIFESTO.md](../../MANIFESTO.md): *"We are not building
Yet Another Agent Framework. We are for running agents."*); it should not
ship a competitive-grade scraper.

The correct framing is the one Claude Code uses for its own `WebFetch` and
`WebSearch`: **be the floor, not the ceiling.** Ship a minimum so that
`pip install eonlet` → "search the web for X" works on first run, with no
extra installation; treat anything beyond that as an extensibility concern
solved by skills (v0.1) and MCP (v0.2).

### Why we must ship something in v0.1 (and can't just punt to MCP)

1. **MCP is v0.2.** Without a built-in, v0.1 has **no web access at all**.
2. **The three bundled templates depend on it.** `assistant` does research,
   `x-digest` summarizes web reading, `portfolio` reads market data. If the
   templates require third-party MCP servers to function, the 30-second
   demo GIF (a v0.1 release gate) cannot be filmed.
3. **First-run friction kills retention.** A HN visitor pasting our
   quickstart who then has to install and configure an MCP server before
   the agent can fetch a URL is gone by the second step.
4. **A local agent that fails closed when an external MCP server is
   unreachable contradicts "local-first."** The built-in must always work.

### Why the v0.1 built-in must be small

1. **Specialists will always be better.** Tavily for search-as-RAG. Crawl4AI
   for HTML scraping at scale. FireCrawl for hosted extraction. We cannot
   out-engineer them with single-committer bandwidth and shouldn't try.
2. **Every dependency is forever.** `pypdf`, `feedparser`, multiple search
   providers — each one is a release-blocker the day its upstream API
   changes. Ship the minimum that the templates need.
3. **Skill and MCP are the right extension points,** not "more providers in
   the runtime." When a user needs Brave search or PDF extraction or
   headless-browser rendering, the answer is "mount an MCP server" (v0.2)
   or "drop a custom tool in `tools/`" (already supported).

### Scope of this change

This ADR is **v0.1.x scope**. It defines a deliberately minimal in-tree
web subsystem:

- HTTP transport with SSRF guard, size cap, retries.
- HTML extraction to markdown via `trafilatura`.
- Two search paths: Tavily (recommended) and a DuckDuckGo HTML fallback
  (zero-config demo path, acknowledged fragile).
- Token-based pagination.

Everything else is explicitly **not built**:

- ❌ PDF extraction (`pypdf`) — point users at `mcp-server-pdf` post-v0.2 or
  let them drop a custom tool. PDF research is genuinely specialist.
- ❌ RSS/Atom feed extraction (`feedparser`) — `x-digest` will use a custom
  per-agent tool (a small `feed_read.py` under `templates/x-digest/tools/`)
  rather than promoting feed-parsing into the runtime. RSS is a polling
  pattern, not a fetch pattern; it shouldn't piggyback on `web_fetch`.
- ❌ Brave / Google CSE / multi-provider abstraction — Tavily is the
  recommended choice and DDG is the no-key fallback. If a user prefers
  Brave/CSE/etc., they write a 60-line custom search tool or wait for MCP.
- ❌ JavaScript rendering (Playwright / headless Chromium) — v0.2+ as an
  opt-in extra.
- ❌ Persistent on-disk HTTP cache — joint design with v0.2 hooks.
- ❌ `robots.txt` enforcement — single-user local context.
- ❌ Image/video/audio extraction.

The product stance, stated plainly in `docs/TOOL_SPEC.md`:

> **`web_fetch` is the floor.** It handles the 80% case — a research agent
> pulling readable text from typical news/blog/docs pages. For PDFs at
> scale, headless rendering, anti-bot evasion, scraping-as-a-service, mount
> an MCP server (v0.2+) or write a custom tool under your agent's `tools/`
> directory.

## Decision

### Part 1 — `web_search`: two paths, no abstraction

A single tool body with a tiny dispatch:

```python
@tool
class WebSearchTool:
    name = "web_search"
    description = "Search the web. Uses Tavily if TAVILY_API_KEY is set, otherwise DuckDuckGo HTML fallback."
    input_schema = WebSearchArgs
    annotations = ToolAnnotations(read_only=True, network=True, estimated_cost_usd=0.0)

    async def __call__(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        if os.environ.get("TAVILY_API_KEY"):
            return await tavily_search(args, ctx)
        return await ddg_search(args, ctx)
```

No `SearchProvider` Protocol, no factory, no auto-cascade across four
backends. Two functions in two files:

```
src/eonlet/web/search/
├── tavily.py   ── ~80 LOC: API call, response → SearchHit list, retry
└── ddg.py      ── existing scrape, regex hardened, doc-stringed "fragile"
```

The shared `SearchHit` / `SearchResponse` pydantic models live in
`src/eonlet/web/search/types.py`. They're a flat schema, not a Protocol:

```python
class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    raw_content: str | None = None     # Tavily can populate; DDG never does
    published_at: datetime | None = None

class SearchResponse(BaseModel):
    provider: str                      # "tavily" | "ddg"
    query: str
    hits: list[SearchHit]
    answer: str | None = None          # Tavily AI summary if present
```

`WebSearchArgs`:

```python
class WebSearchArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)
    include_raw_content: bool = False     # Tavily-only; ignored by DDG
```

#### What this gives up

- No Brave, no Google CSE. Users who want them write a 60-LOC custom tool
  under their agent's `tools/` directory and call the relevant SDK
  directly. We document this pattern in `docs/TOOL_SPEC.md`.
- No first-class `provider="…"` argument. If you set `TAVILY_API_KEY`, you
  get Tavily; if you don't, you get DDG. To force one or the other, unset
  the env var.

This is deliberate. The earlier multi-backend draft was building scaffolding
for a future we don't know will arrive.

### Part 2 — `web_fetch`: HTTP + HTML → markdown only

Pipeline (much smaller than the original draft):

```
web_fetch tool body
       │
       ▼
HTTPFetcher           ── httpx + SSRF + size cap + retries
       │
       ▼
content-type triage:
   text/html, application/xhtml+xml  → trafilatura → markdown
   text/*, application/json          → UTF-8 decode (pretty-print JSON)
   anything else                     → is_error=True with helpful message
       │
       ▼
paginate(text, offset, max_tokens)
       │
       ▼
FetchResult
```

No extractor registry, no `Extractor` protocol, no PDF, no RSS. Three
functions in one module (`src/eonlet/web/fetch.py`), plus a small
`HTTPFetcher` in `src/eonlet/web/transport.py`.

#### `HTTPFetcher` (`web/transport.py`)

`httpx.AsyncClient` wrapper. Encapsulates:

- HTTP/2, `follow_redirects=True`.
- Retry: 3 attempts on `TransportError` / 5xx, exponential backoff
  (0.5s / 1s / 2s). No retry on 4xx.
- SSRF guard at hostname-resolution time: reject loopback, link-local,
  RFC1918, CGNAT, cloud metadata endpoints (169.254.169.254), IPv6
  equivalents. Allow-list escape hatch via config.
- Scheme allow-list: `http`, `https` only.
- Streaming body read; abort if running byte total > `max_bytes`
  (default 10 MB).
- `User-Agent: Eonlet/<version> (+https://eonlet.dev)`.

The SSRF helpers (~50 LOC of IP classification) live in `web/ssrf.py`. If
a generic network-egress policy emerges later for other tools, this code
moves to `permissions/`; not before.

#### HTML extraction (`web/fetch.py`)

```python
import trafilatura

def extract_html(raw: bytes, *, url: str) -> ExtractedContent:
    html = raw.decode("utf-8", errors="replace")
    result = trafilatura.extract(
        html, output_format="markdown", with_metadata=True, url=url
    )
    if result is None:
        # SPA-style page with no detectable main content
        return ExtractedContent(
            title=None, content_markdown="", metadata={"warning": "no_main_content"}
        )
    # trafilatura's result is a markdown body; metadata is JSON-encoded
    # in its `--json` mode but here we ask for plain markdown and parse
    # the metadata via trafilatura.bare_extraction for title/date/etc.
    ...
```

`ExtractedContent`:

```python
class ExtractedContent(BaseModel):
    title: str | None
    content_markdown: str
    metadata: dict[str, Any]   # author, published_at, language, sitename
```

#### Pagination (`web/pagination.py`)

Token-based slicing using `memory/tokens.py`. Output:

```python
class PaginatedSlice(BaseModel):
    text: str
    truncated: bool
    total_tokens: int
    next_offset: int | None
```

#### `WebFetchArgs`

```python
class WebFetchArgs(BaseModel):
    url: str
    max_tokens: int = Field(default=4000, ge=200, le=20000)
    offset_tokens: int = 0
```

The unused `prompt` field is removed.

#### Tool body

```python
@tool
class WebFetchTool:
    async def __call__(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        raw, headers = await ctx.deps.http_fetcher.get(args.url)
        ctype = headers.get("content-type", "").lower()

        if "html" in ctype:
            extracted = extract_html(raw, url=args.url)
        elif ctype.startswith("text/") or "json" in ctype:
            extracted = extract_text(raw, ctype=ctype, url=args.url)
        else:
            return ToolResult(
                content=f"Unsupported content type: {ctype}. "
                        f"For PDFs, RSS, or binary content, use a custom tool or MCP server.",
                is_error=True,
            )

        sliced = paginate(extracted.content_markdown, args.offset_tokens, args.max_tokens)
        return ToolResult(
            content=sliced.text,
            structured_output={
                "url": args.url,
                "title": extracted.title,
                "content_type": ctype,
                "metadata": extracted.metadata,
                "truncated": sliced.truncated,
                "total_tokens": sliced.total_tokens,
                "next_offset": sliced.next_offset,
            },
        )
```

### Part 3 — Package layout (minimal)

```
src/eonlet/web/
├── __init__.py
├── transport.py        ── HTTPFetcher
├── ssrf.py             ── SSRF guard helpers
├── pagination.py       ── token-based slicing
├── fetch.py            ── extract_html, extract_text, ExtractedContent
└── search/
    ├── types.py        ── SearchHit, SearchResponse
    ├── tavily.py
    └── ddg.py
```

`src/eonlet/tools/builtin/web.py` becomes a thin shim wiring args → these
modules → `ToolResult`. About 80 lines total.

### Part 4 — Configuration

```yaml
# agent.yaml
web:
  fetch:
    max_bytes: 10485760              # 10 MB cap on raw response
    max_tokens_per_call: 4000
    timeout_seconds: 30
    allow_private_networks: false    # SSRF escape hatch
    user_agent: null                 # null → default Eonlet UA
  # no `search:` block — provider selection is just env-var presence
```

Environment variables:

```
TAVILY_API_KEY   # optional; absent → DDG fallback
```

`AGENT_CONFIG_SPEC.md` gains a small § documenting `web:`.

### Part 5 — Events

Two new `EventKind` variants (same as the original draft):

- `WEB_SEARCH_PERFORMED` — `{provider, query, max_results, hit_count, error?}`
- `WEB_FETCH_PERFORMED` — `{url, content_type, total_tokens, truncated, error?}`

Summary fields only. Full responses go through the normal `TOOL_RESULT`
event.

### Part 6 — Documentation: the extensibility story

Beyond the code, this ADR commits to documenting the boundary clearly:

- `docs/TOOL_SPEC.md` `web_fetch` / `web_search` sections include a
  **"When the built-in isn't enough"** subsection pointing at:
  1. Custom tools (template: a snippet showing how to add `tools/scrape.py`
     calling Crawl4AI or any chosen library).
  2. MCP integration (v0.2) — placeholder note that this section will
     expand when MCP lands.
- One example tutorial after v0.2: `tutorials/05-extending-web-tools.md`
  showing how to swap the built-in for `mcp-server-fetch`.

## Consequences

### Positive

- **Implementable in ~2 days, not ~6.** Plan goes from 8 milestones to 3.
- **One new runtime dependency: `trafilatura`.** Down from three. Smaller
  wheel, less to break on upstream changes.
- **First-run experience preserved.** `pip install eonlet` + `TAVILY_API_KEY`
  → works. No key → DDG fallback works (fragile but works).
- **Templates keep working.** `x-digest` switches its RSS handling to a
  per-template custom tool (one file, ~30 LOC with `feedparser`); other
  templates use the built-in unchanged.
- **Honest product positioning.** We stop pretending to compete with
  specialists. The doc says, in so many words, "for serious scraping, use
  MCP" — which is true and matches the runtime/framework split in the
  MANIFESTO.

### Negative

- **No built-in PDF.** Agents that follow PDF links land on an `is_error`
  result. Documented; ungrateful but defensible. Mitigation: an early
  v0.2 deliverable is `mcp-server-pdf` integration documentation.
- **No built-in RSS.** `x-digest` now ships a per-template tool that does
  RSS parsing. Slightly less "clean" (logic in template, not runtime) but
  arguably more correct (RSS polling is a per-agent concern).
- **DDG remains fragile.** Same as before; documented as fragile demo
  fallback. Long-term answer: users set `TAVILY_API_KEY` or write a
  custom tool.
- **Less optionality.** A user who *wants* Brave or Google CSE today has
  to write a custom tool. Documented pattern; ~60 LOC; not infeasible.

### Neutral

- **`include_raw_content` Tavily blur.** Two ways to get a page's content:
  Tavily's `include_raw_content=True` during search, or follow-up
  `web_fetch`. Documented — prefer Tavily's pre-fetch when available.
- **No persistent cache.** In-process LRU only.

## Alternatives considered

### A. Earlier draft of this ADR (HTML + PDF + RSS + 4 search providers)

Rejected after a product-strategy review on 2026-05-26. It was overbuilt —
treating Eonlet as competing with Tavily/Crawl4AI/FireCrawl. The runtime's
job is to provide a *floor*, not the *ceiling*. See "Context."

### B. Punt entirely to MCP — keep v0.0.2 placeholders

Rejected. MCP is v0.2; v0.1 needs working web access for the demo, the
three bundled templates, and basic dogfood. The local-first thesis means
the agent must not require external servers to fetch a URL.

### C. Adopt Crawl4AI wholesale as the fetch backend

Tempting — it solves most of what `web_fetch` does. Rejected for v0.1:
- Pulls a meaningfully larger dependency tree (≈ 100 MB with optional
  Playwright).
- Crawl4AI is an actively evolving project; coupling our minimum-viable
  fetch to it is high upstream-volatility for a small gain.
- Its abstractions (CrawlResult, extraction strategies, dispatchers) don't
  map cleanly onto our small ToolResult shape; we'd write an adapter
  anyway.

Revisit at v0.2 as an opt-in extra: `pip install eonlet[crawl4ai]`. At that
point the comparison is Crawl4AI vs MCP-based scrapers as the "advanced"
path, with our built-in as the floor.

### D. Use Jina Reader (`r.jina.ai/<url>`) as the fetch implementation

One-line implementation. Rejected: violates local-first. Adopting a hosted
proxy as the runtime's only fetch path means an offline machine cannot read
the web. (Users are still free to add Jina as a custom tool if they want
that trade-off.)

### E. Make `web_search` / `web_fetch` "default skills" instead of built-ins

Interesting structurally — fits Eonlet's "configured not coded" ethos
better. Rejected for v0.1 because skills are markdown loaded into context,
not Python tools that get registered with `@tool`; that's a meaningful
extension of the skill mechanism and a v0.2-scope architectural change.
Could happen at v0.2 simultaneously with MCP.

## Migration

- No backward-compatibility concerns; Eonlet hasn't released to PyPI.
- `tools/builtin/web.py` becomes a shim; new code lives in `src/eonlet/web/`.
- `tests/unit/test_web_tools.py` updated to exercise the new pipeline.
- `templates/x-digest/agent.yaml` declares a new per-template tool
  `tools/feed_read.py` (~30 LOC `feedparser` wrapper). This tool becomes a
  documented example of "how to extend Eonlet's web capabilities with
  custom tools" — turning what could have been runtime bloat into a
  teaching moment.

## Validation

This ADR is validated when:

1. SSRF guard refuses loopback / RFC1918 / metadata endpoints (unit test).
2. HTML extractor produces markdown with preserved heading and link
   structure on a fixture corpus of 5 pages (news, blog, GitHub README,
   Wikipedia, SPA fallback). ≥80% of `<a href>` retained.
3. Tavily integration test passes against `respx` fixtures (CI) and a
   live call (`pytest.mark.live`, manual).
4. DDG fallback returns ≥3 hits for "anthropic claude" on a live run
   (manual; flaky by design).
5. `x-digest` template's new per-template `feed_read.py` tool parses
   RSS 2.0, Atom 1.0, and JSON Feed fixtures.
6. End-to-end smoke: `assistant` template searches Tavily, fetches the top
   hit, returns a markdown summary; `eonlet replay` shows the two new
   summary events plus the standard `TOOL_RESULT`s.
7. Coverage: `src/eonlet/web/` ≥ 80%; project ≥ 70%.
