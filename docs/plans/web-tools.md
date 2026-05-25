# Plan — Web Tools Upgrade (v0.1)

> Companion to [ADR-0004](../adr/0004-web-tools.md). The ADR fixes the design;
> this plan sequences the implementation, defines milestones, and lists what
> "done" looks like at each step.

| Field | Value |
|---|---|
| Owner | Ziyu |
| Started | 2026-05-26 |
| Target | v0.1.0 |
| Status | Not started — ADR-0004 just landed |
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

Three milestones, three PRs (or three commit groups).

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
- Add `trafilatura` to `pyproject.toml`. (Only new runtime dep in this whole
  upgrade.)

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

- **`x-digest` template gains a per-agent custom tool.**
  `src/eonlet/templates/x-digest/tools/feed_read.py` (~30 LOC `feedparser`
  wrapper). Returns top-N entries as `[{title, url, summary, published_at}]`.
  Update `templates/x-digest/agent.yaml` to declare it.
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

## Open questions (resolve as implementation proceeds)

1. **SSRF policy location.** Currently `web/ssrf.py`. If a generic network-
   egress policy emerges for `send_email` recipient whitelisting or future
   MCP transports, promote into `permissions/`. Not before.
2. **Should DDG fallback emit a warning event when used?** Lean yes — emit
   a `WEB_SEARCH_FALLBACK` event so `eonlet replay` makes the fragile path
   visible. Decide during M2.
3. **`include_raw_content=True` with DDG.** DDG can't provide raw content.
   Current draft: silently ignore. Consider returning a typed warning in
   `structured_output`. Decide during M2.
4. **PDF / MCP guidance docs.** When v0.2 MCP lands, the "use an MCP server"
   subsection in `TOOL_SPEC.md` needs concrete pointers (e.g.
   `mcp-server-fetch`, `mcp-server-pdf`). Out of scope here; tracked for
   v0.2.

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

## Acceptance — plan complete when

- All three milestones merged.
- ADR-0004 status updated to Accepted.
- All three templates pass a manual smoke: search → fetch → summarize.
- `x-digest` template runs successfully against a real RSS feed using its
  per-template `feed_read.py`.
- `docs/TOOL_SPEC.md` includes the "When the built-in isn't enough"
  subsections under both tool entries.
- README quickstart still works on a fresh machine with only
  `TAVILY_API_KEY` set.
