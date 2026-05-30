# Plan — Web Tools Upgrade (v0.1)

> Companion to [ADR-0004](../adr/0004-web-tools.md). The ADR fixes the design;
> this plan sequences the implementation, defines milestones, and lists what
> "done" looks like at each step.

| Field | Value |
|---|---|
| Owner | Ziyu |
| Started | 2026-05-26 |
| Target | v0.1.0 |
| Status | **Shipped 2026-05-28 (v0.0.7).** M1–M3 merged; ADR-0004 Accepted. 48h canary dogfood is owner-handled outside the implementation window. |
| Estimated effort | 2–3 working days, single committer |

## Why this plan exists

ADR-0004 picks a deliberately minimal scope: HTML-only fetch via trafilatura,
two search paths (Tavily + DDG fallback), no PDF / RSS / multi-backend
abstraction. Everything else is an extensibility concern (custom tools today,
MCP at v0.2). This plan turns that scope into reviewable steps.

## Guiding principles

1. **Land the new pipeline behind the existing tool name.** `web_search` and
   `web_fetch` keep the same tool names and `ToolResult` shape from the
   agent's perspective. The three bundled templates stay green at every
   step.
2. **Strangler pattern.** Build `src/eonlet/web/` alongside
   `tools/builtin/web.py`. Switch the tool body over only after the new
   subsystem passes its tests. Delete the legacy code in the final step.
3. **Fixtures over live calls in CI.** HTTP and Tavily tests use `respx` or
   recorded fixtures. Live tests live behind `pytest.mark.live` and only run
   when `EONLET_LIVE_TESTS=1`.
4. **Write the "when the built-in isn't enough" docs in the same PR as the
   code.** The extensibility story is the product positioning — it can't
   trail the implementation.

## Milestone map

```
M1  HTTPFetcher + SSRF + extract_html             (≈ 1 day)
M2  Tavily + DDG + tool rewrites + config + events (≈ 1 day)
M3  x-digest feed tool + docs + legacy removal     (≈ 0.5 day + dogfood)
```

Three milestones, three PRs.

### Suggested PR shape

```
PR1 (M1) — feat(web): HTTPFetcher + SSRF + extract_html + pagination
  - New: src/eonlet/web/{ssrf,transport,fetch,pagination}.py
  - New dep: trafilatura (kept as its own commit for rollback)
  - Tests + fixtures under tests/unit/web/ and tests/fixtures/web/html/
  - Coverage ≥80% on src/eonlet/web/
  - No call sites changed; tools/builtin/web.py untouched

PR2 (M2) — feat(web): rewrite web_search + web_fetch tool bodies
  - ToolContext gains `http_fetcher: HTTPFetcher | None`
  - Worker startup constructs the singleton and threads it through
  - Two new EventKind variants: WEB_SEARCH_PERFORMED, WEB_FETCH_PERFORMED
  - WebFetchConfig added to AgentConfig (`agent.yaml: web.fetch`)
  - Three bundled templates pass their smoke tests unchanged

PR3 (M3) — feat(x-digest): feed_read.py + docs + ADR-0004 → Accepted
  - templates/x-digest/tools/feed_read.py (new file)
  - Docs: TOOL_SPEC.md, AGENT_CONFIG_SPEC.md, SECURITY.md, CLAUDE.md,
    CHANGELOG.md
  - ADR-0004 status: Proposed → Accepted (shipped in v0.1.0)
  - 48h canary dogfood with three real feeds
```

---

## M1 — Transport + extraction core (≈ 1 day)

### Scope

- Create `src/eonlet/web/` package skeleton with `__init__.py`.
- `src/eonlet/web/ssrf.py` — IP-classification helpers (loopback,
  link-local, RFC1918, CGNAT, cloud metadata, IPv6 equivalents). Pure
  functions over `ipaddress.ip_address`.
- `src/eonlet/web/transport.py` — `HTTPFetcher`:
  - `httpx.AsyncClient` (HTTP/2, follow_redirects).
  - Retry: 3 attempts on `TransportError` / 5xx, backoff 0.5s/1s/2s. No
    retry on 4xx.
  - SSRF check at hostname resolution, pre-connect.
  - Scheme allow-list (`http`, `https` only).
  - Streaming body with `max_bytes` (default 10 MB) abort.
  - Configurable per-request connect/read/total timeouts.
  - Default `User-Agent: Eonlet/<version> (+https://eonlet.dev)`.
- `src/eonlet/web/fetch.py` — `extract_html(raw, url) -> ExtractedContent`
  using `trafilatura.extract(..., output_format="markdown", with_metadata=True)`.
  Plus `extract_text(raw, ctype, url)` for `text/*` and `application/json`.
- `src/eonlet/web/pagination.py` — `paginate(text, offset_tokens, max_tokens)`
  returning `PaginatedSlice`. Uses the existing `memory/tokens.py` counter.
- Add `trafilatura` to `pyproject.toml`. (Only new *direct* runtime dep in
  this whole upgrade. Verified 2026-05-28: pulls 10 transitive packages
  totalling ~17 MB of wheels — lxml 5 MB and babel 10 MB dominate; all
  others combined are ~2 MB. All licenses compatible — Apache 2.0 / BSD /
  MIT, with `tld` offering MPL-1.1 OR GPL-2.0 OR LGPL-2.1+ which we use
  under the LGPL or MPL terms. No source compilation required: lxml ships
  manylinux2014 wheels for cp311–cp313.)

### Tests (`tests/unit/web/`)

- `test_ssrf.py`: each rejected IP class returns the right classification.
- `test_transport.py`:
  - SSRF: loopback / RFC1918 / metadata / link-local all rejected before
    network egress with typed `SSRFRejected`.
  - Scheme allow-list: `file://`, `ftp://`, `data:` rejected.
  - Retry: `respx` returns 502 twice then 200 → success with two retries.
  - No retry on 4xx.
  - Size cap: streamed response exceeding `max_bytes` aborts with
    `ResponseTooLarge`.
  - Redirect: follows up to N hops; final URL surfaced.
- `test_extract_html.py`: 5 fixtures in `tests/fixtures/web/html/`:
  - news article, blog with code blocks, GitHub README, Wikipedia excerpt,
    SPA-style page with no main content.
  - Assert: title extraction, heading preservation, ≥80% of `<a href>`
    links retained, SPA fixture returns the `no_main_content` warning.
- `test_extract_text.py`: UTF-8 BOM handled, JSON pretty-printed.
- `test_pagination.py`: token-accurate slicing, `next_offset` correctness,
  `next_offset` is `None` on the last slice.

### Done when

- All M1 tests pass.
- mypy strict and ruff strict clean on the new modules.
- No usage from `tools/builtin/web.py` yet — greenfield code.

---

## M2 — Search + tool bodies + config + events (≈ 1 day)

### Scope

- `src/eonlet/web/search/types.py` — `SearchHit`, `SearchResponse` (flat
  pydantic models; no Protocol).
- `src/eonlet/web/search/tavily.py` — `async def tavily_search(args, ctx)`.
  Calls Tavily API with `search_depth` mapped from `include_raw_content`.
  Maps response to `SearchResponse(provider="tavily", …)`.
- `src/eonlet/web/search/ddg.py` — port and harden the existing DDG HTML
  scrape. Same `SearchResponse(provider="ddg", …)` shape. Docstring labels
  it "fragile fallback; prefer setting `TAVILY_API_KEY`."
- Inject `HTTPFetcher` as a worker-level singleton via `ToolContext.deps`.
  Extend `ToolContext` if needed.
- Rewrite `src/eonlet/tools/builtin/web.py`:
  - `WebSearchTool`: dispatch on `TAVILY_API_KEY` env var. ~10 LOC.
  - `WebFetchTool`: `fetcher.get → content-type triage → extract → paginate
    → ToolResult`. ~30 LOC.
- `src/eonlet/config.py` — add `WebFetchConfig` model nested under
  `AgentConfig.web.fetch`. No `search` config block (env-var only).
- `runtime/events.py` — add `WEB_SEARCH_PERFORMED` and `WEB_FETCH_PERFORMED`
  to `EventKind`. AgentRuntime appends them at tool-call completion.

### Tests

- `tests/unit/web/test_search_tavily.py`: `respx` mocks Tavily API; assert
  `SearchResponse` mapping, `include_raw_content` toggles `search_depth`,
  retry on 502.
- `tests/unit/web/test_search_ddg.py`: `respx` mock of DDG HTML;
  assert hit extraction, URL decoding, snippet stripping.
- `tests/unit/test_web_tools.py` (rewrite):
  - `web_search` dispatches to Tavily when key present, DDG when absent.
  - `web_fetch` HTML round-trip: fixture URL → markdown body with title.
  - `web_fetch` pagination: large fixture → request `offset_tokens=N` →
    `next_offset` chains correctly to the end.
  - `web_fetch` on unsupported content type (e.g. `image/png`) returns
    `is_error=True` with the documented "use custom tool or MCP" message.
- Live tests behind `pytest.mark.live` (skipped without `EONLET_LIVE_TESTS=1`):
  - Tavily: real query, ≥3 hits.
  - DDG: real query, ≥3 hits (flaky by design; failure is logged, not
    fatal).

### Done when

- All M2 tests pass.
- `assistant` template smoke test: "search the web for Anthropic Claude
  release notes" returns hits and a fetched markdown body.
- `eonlet replay` on the smoke session shows the two new summary events.

---

## M3 — `x-digest` feed tool + docs + legacy removal + canary (≈ 0.5 day + 48h)

### Scope

- **`x-digest` template gains a new per-agent custom tool** (addition, not
  migration — see clarification A above).
  `src/eonlet/templates/x-digest/tools/feed_read.py` (~30 LOC `feedparser`
  wrapper). Returns top-N entries as `[{title, url, summary, published_at}]`.
  Update `templates/x-digest/agent.yaml` to declare it (kept alongside the
  existing `x_timeline.py`; the user can pick which to schedule against).
  - This becomes the canonical example of "how to extend Eonlet's web
    capabilities with a custom tool." Documented as such in `TOOL_SPEC.md`.
- **Documentation:**
  - `docs/TOOL_SPEC.md` — rewrite `web_search` and `web_fetch` sections.
    Each ends with a **"When the built-in isn't enough"** subsection
    pointing at custom tools (with the `x-digest` feed reader as example)
    and at MCP (placeholder note pending v0.2).
  - `docs/AGENT_CONFIG_SPEC.md` — new § for the `web.fetch` block.
  - `docs/SECURITY.md` — SSRF guard + escape hatch.
  - `CLAUDE.md` — cross-reference ADR-0004; tool count remains correct.
  - `CHANGELOG.md` — `[Unreleased]` entry for the web-tools upgrade.
- **Legacy removal.**
  - Delete the bodies of the old `WebSearchTool` and `WebFetchTool` in
    `src/eonlet/tools/builtin/web.py`; keep only the thin shims importing
    from `eonlet.web`.
- **Coverage check.** `src/eonlet/web/` ≥ 80%; project ≥ 70%.
- **ADR status flip.** Once M3 merges, move ADR-0004 to
  `Accepted (shipped in v0.1.0)` at release tagging.

### Canary dogfood (passive, 48 hours)

- Run `x-digest` against three real feeds (one news, one blog, one
  developer release feed) every two hours for 48 hours.
- Acceptance: no unhandled exceptions in the worker log; output digests
  non-empty; `eonlet replay` shows clean event chains; no SSRF or
  size-cap false positives.

### Done when

- M3 PR merged.
- `eonlet replay` on a full session (search → fetch → feed → summarize)
  reads cleanly end-to-end.
- README quickstart still works on a fresh machine with only
  `TAVILY_API_KEY` set.

---

## Test fixture inventory

```
tests/fixtures/web/
├── html/
│   ├── news_article.html         ── wire-service piece
│   ├── blog_post.html            ── personal blog with code blocks
│   ├── github_readme.html        ── nested headings, links, tables
│   ├── wikipedia_excerpt.html    ── many inter-page links
│   └── spa_minimal.html          ── ~empty body — should warn no_main_content
└── (no PDF or feed fixtures — out of scope for the runtime)
```

Feed fixtures (for the `x-digest` per-template tool) live alongside the
template:

```
src/eonlet/templates/x-digest/tests/fixtures/
├── rss_2_0.xml
├── atom_1_0.xml
└── json_feed.json
```

This co-location matters: it reinforces that feed parsing is **a template
concern, not a runtime concern**.

## Resolved decisions (closed 2026-05-28)

The four "open questions" from the original draft are now resolved. Captured
here so the implementation PRs don't relitigate them.

1. **SSRF policy location — keep in `web/ssrf.py`.**
   Only one egress caller exists (`HTTPFetcher`). Per CLAUDE.md
   ("three similar lines is better than a premature abstraction"), promotion
   into `permissions/` waits for a second caller (likely v0.2 MCP transport
   or `send_email` recipient policy). The helpers are pure functions over
   `ipaddress.ip_address` and trivially re-exportable when that day comes.

2. **DDG fallback signalling — provider field, no new event.**
   The `WEB_SEARCH_PERFORMED` event's `provider` field (`"tavily"` |
   `"ddg"`) already makes the fragile path visible to `eonlet replay`.
   Do **not** add a separate `WEB_SEARCH_FALLBACK` variant — `EventKind` is
   already 37 members and the information is recoverable from one filter.

3. **`include_raw_content=True` with DDG — silent + structured warning.**
   The flag is silently honoured as best-effort (DDG never populates
   `raw_content`). The tool surfaces this via
   `structured_output["warnings"] = ["raw_content_unavailable_on_ddg"]` so
   the LLM can decide whether to chain a follow-up `web_fetch`. No
   exception, no `is_error=True` — DDG fallback already implies degraded
   quality.

4. **PDF / MCP guidance docs — placeholder in M3, expand at v0.2.**
   M3's `TOOL_SPEC.md` rewrite includes the "When the built-in isn't
   enough" subsection with a `> TODO(v0.2): link to mcp-server-fetch /
   mcp-server-pdf once MCP integration lands` line. Concrete pointers wait
   until MCP ships and we know which servers are battle-tested.

## Implementation deviations from ADR-0004 (clarifications)

These are not changes to the design — they're places where the ADR's prose
was slightly ahead of the code. Each is a small correction that makes the
PRs easier to write and review.

### A. x-digest's `feed_read.py` is an **addition**, not a migration

ADR-0004 and the earlier draft of this plan implied that `x-digest`
currently has runtime-level RSS handling that the v0.1 upgrade pushes into
the template. **Not true.** The shipped `x-digest` template fetches its
content via a per-template `x_timeline.py` (X/Twitter API v2 endpoint) and
has no RSS path. Therefore M3's deliverable is reframed as:

> Add `templates/x-digest/tools/feed_read.py` as the canonical example of
> "how to extend Eonlet's web capabilities with a custom tool." It is not
> required for the existing `x-digest` schedule to keep working.

This matters because the M3 acceptance criterion "x-digest runs against a
real RSS feed" is then an *opt-in demo path*, not a regression check on the
template's primary behaviour.

### B. `HTTPFetcher` injection — extend `ToolContext`, do not use `extra`

The ADR says "Inject `HTTPFetcher` as a worker-level singleton via
`ToolContext.deps`." `ToolContext` has no `deps` attribute today (see
`src/eonlet/tools/protocol.py` — there is only `extra: dict[str, Any]`).

**Decision:** add a typed field, do not piggyback on `extra`.

```python
# src/eonlet/tools/protocol.py
@dataclass(slots=True)
class ToolContext:
    ...
    http_fetcher: HTTPFetcher | None = None  # set by worker startup
```

Rationale: this is a long-lived dependency, not transient runtime state. A
typed field keeps mypy strict happy and makes the wiring discoverable. The
`None` default preserves backwards compatibility for the few test sites
that construct `ToolContext` directly without a worker.

Tools that need it call `assert ctx.http_fetcher is not None` at the top
and raise the project's `RuntimeError` subclass on the `None` path. (Tools
in the agent loop will always have it; standalone unit tests for tools
that don't exercise HTTP can leave it `None`.)

### C. `WEB_FETCH_PERFORMED` payload — add `bytes_in` and `offset_tokens`

ADR-0004 specifies `{url, content_type, total_tokens, truncated, error?}`.
For dogfood-period debugging (especially "why did this page truncate at N
tokens?") add two summary fields:

```python
{
    "url": str,
    "content_type": str,
    "bytes_in": int,         # raw response size before extraction
    "offset_tokens": int,    # caller-supplied pagination offset
    "total_tokens": int,
    "truncated": bool,
    "error": str | None,
}
```

Still summary-only; the full markdown body stays in the `TOOL_RESULT`
event.

## What this plan deliberately does **not** include

These were in the earlier draft of ADR-0004 and were cut for the reasons in
its "Why the v0.1 built-in must be small" section. Listed here to make the
cuts visible:

- ❌ Brave Search API backend
- ❌ Google Custom Search backend
- ❌ `SearchProvider` Protocol + factory + `auto` cascade
- ❌ PDF extractor (`pypdf`)
- ❌ RSS / Atom / JSON Feed runtime extractor (moved into the `x-digest`
  template as a per-agent custom tool)
- ❌ `ExtractorRegistry` + `Extractor` Protocol
- ❌ JavaScript rendering (Playwright)
- ❌ Persistent on-disk HTTP cache
- ❌ `robots.txt` enforcement

The first three are user-facing capability cuts and must be reflected in
the README's "what's included / what's not" table. The rest are internal
abstractions we don't need yet.

## Acceptance — status as of 2026-05-28

| Criterion | Status |
|---|---|
| All three milestones merged | ✅ M1 + M2 + M3 landed in v0.0.7 |
| ADR-0004 status flipped to Accepted | ✅ |
| Three templates pass manual smoke (search → fetch → summarize) | ⏳ owner-handled dogfood |
| `x-digest` runs against a real RSS feed via `feed_read.py` | ⏳ owner-handled dogfood |
| `docs/TOOL_SPEC.md` has the "When the built-in isn't enough" subsections | ✅ §6.7 + §6.8 |
| README quickstart works on a fresh machine with only `TAVILY_API_KEY` | ⏳ owner-handled dogfood |
| 48 h `x-digest` feed canary | ⏳ owner-handled dogfood |

The remaining ⏳ items are dogfood activities owned by the maintainer
outside the engineering window. Engineering completion: **done.**
