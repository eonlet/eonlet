"""Web tools — web_search (Tavily or DDG fallback) and web_fetch (httpx + strip)."""

from __future__ import annotations

import html
import os
import re

import httpx
from pydantic import BaseModel, Field

from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

# ── web_search ───────────────────────────────────────────────────────────────


class WebSearchArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)


@tool
class WebSearchTool:
    name = "web_search"
    description = (
        "Search the web. Uses Tavily if TAVILY_API_KEY is set, otherwise "
        "DuckDuckGo Instant Answer + HTML fallback. Returns title/url/snippet."
    )
    input_schema = WebSearchArgs
    annotations = ToolAnnotations(read_only=True, network=True, estimated_cost_usd=0.0)

    async def __call__(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        if os.environ.get("TAVILY_API_KEY"):
            return await _tavily_search(args)
        return await _ddg_search(args)


async def _tavily_search(args: WebSearchArgs) -> ToolResult:
    key = os.environ["TAVILY_API_KEY"]
    payload = {
        "api_key": key,
        "query": args.query,
        "max_results": args.max_results,
        "search_depth": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.tavily.com/search", json=payload)
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return ToolResult(content=f"tavily error: {e}", is_error=True)
    results = [
        {"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")}
        for x in (data.get("results") or [])
    ]
    body = "\n\n".join(
        f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)
    )
    return ToolResult(content=body or "no results", structured_output={"results": results})


async def _ddg_search(args: WebSearchArgs) -> ToolResult:
    """DuckDuckGo HTML scrape — best-effort, no key needed."""
    headers = {"User-Agent": "Mozilla/5.0 (eonlet)"}
    params = {"q": args.query}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=headers) as c:
            r = await c.get("https://duckduckgo.com/html/", params=params)
            r.raise_for_status()
            html_text = r.text
    except Exception as e:
        return ToolResult(content=f"ddg error: {e}", is_error=True)

    # The DDG HTML page wraps each result in `<a class="result__a" href="…">title</a>`
    # with a snippet in `<a class="result__snippet">…</a>`. Use regex — fragile,
    # but acceptable for an MVP fallback that has no key.
    titles = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html_text,
        flags=re.DOTALL,
    )
    snippets = re.findall(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', html_text, flags=re.DOTALL
    )
    results: list[dict[str, str]] = []
    for i, (url, title) in enumerate(titles[: args.max_results]):
        snippet = snippets[i] if i < len(snippets) else ""
        results.append(
            {
                "url": _decode_ddg_url(url),
                "title": _strip_tags(title),
                "snippet": _strip_tags(snippet),
            }
        )
    body = "\n\n".join(
        f"{i + 1}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results)
    )
    return ToolResult(content=body or "no results", structured_output={"results": results})


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _decode_ddg_url(u: str) -> str:
    """DDG wraps real URLs as ``/l/?uddg=<urlencoded>``. Extract."""
    m = re.search(r"uddg=([^&]+)", u)
    if not m:
        return u
    from urllib.parse import unquote

    return unquote(m.group(1))


# ── web_fetch ────────────────────────────────────────────────────────────────


class WebFetchArgs(BaseModel):
    url: str
    prompt: str = Field(default="", description="Optional summarization hint (unused at v0.0.2).")


@tool
class WebFetchTool:
    name = "web_fetch"
    description = "Fetch a URL, return readable text content (HTML tags stripped)."
    input_schema = WebFetchArgs
    annotations = ToolAnnotations(read_only=True, network=True)

    async def __call__(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        try:
            async with httpx.AsyncClient(
                timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (eonlet)"}
            ) as c:
                r = await c.get(args.url)
                r.raise_for_status()
        except Exception as e:
            return ToolResult(content=f"fetch failed: {e}", is_error=True)

        ctype = r.headers.get("content-type", "")
        if "html" in ctype:
            text = _readable(r.text)
            title = _find_title(r.text)
        else:
            text = r.text
            title = args.url

        # Cap at ~50KB for context-budget safety.
        if len(text) > 50_000:
            text = text[:50_000] + "\n…[truncated]…"

        return ToolResult(
            content=text,
            structured_output={"url": args.url, "title": title, "bytes": len(r.content)},
        )


def _find_title(html_text: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    return _strip_tags(m.group(1)) if m else ""


def _readable(html_text: str) -> str:
    """Crude HTML→text. Strips scripts, styles, then all remaining tags."""
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html_text)
    body = re.sub(r"(?is)<!--.*?-->", " ", body)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html.unescape(body)
    # Collapse whitespace.
    body = re.sub(r"\n\s*\n+", "\n\n", body)
    body = re.sub(r"[ \t]+", " ", body)
    return body.strip()
