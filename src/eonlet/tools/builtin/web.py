"""Web tools — ``web_search`` and ``web_fetch`` (ADR-0004).

Thin shim over ``src/eonlet/web/``. Search dispatches on
``TAVILY_API_KEY`` env-var presence; fetch goes through the shared
``HTTPFetcher`` then content-type triage → extraction → pagination.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from ...errors import (
    HTTPFetchError,
    ResponseTooLargeError,
    SSRFRejectedError,
    UnsupportedSchemeError,
)
from ...runtime.events import web_fetch_performed, web_search_performed
from ...web.fetch import (
    ExtractedContent,
    extract_html,
    extract_text,
    is_html_content_type,
    is_text_content_type,
)
from ...web.pagination import paginate
from ...web.search import SearchResponse, ddg_search, tavily_search
from ..protocol import ToolAnnotations, ToolContext, ToolResult, tool

# ── web_search ───────────────────────────────────────────────────────────────


class WebSearchArgs(BaseModel):
    query: str
    max_results: int = Field(default=5, ge=1, le=20)
    include_raw_content: bool = Field(
        default=False,
        description="Tavily only: include each hit's extracted body. Ignored by DDG.",
    )


@tool
class WebSearchTool:
    name = "web_search"
    description = (
        "Search the web. Uses Tavily when TAVILY_API_KEY is set; otherwise "
        "falls back to a fragile DuckDuckGo HTML scrape. Returns "
        "title/url/snippet (and raw_content/answer when Tavily is in use)."
    )
    input_schema = WebSearchArgs
    annotations = ToolAnnotations(read_only=True, network=True, estimated_cost_usd=0.0)

    async def __call__(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        if ctx.http_fetcher is None:
            return ToolResult(
                content="web_search: HTTP transport not initialised (worker bug)",
                is_error=True,
            )
        fetcher = ctx.http_fetcher

        use_tavily = bool(os.environ.get("TAVILY_API_KEY"))
        provider = "tavily" if use_tavily else "ddg"
        warnings: list[str] = []
        try:
            if use_tavily:
                response = await tavily_search(
                    query=args.query,
                    max_results=args.max_results,
                    include_raw_content=args.include_raw_content,
                    fetcher=fetcher,
                )
            else:
                response = await ddg_search(
                    query=args.query,
                    max_results=args.max_results,
                    fetcher=fetcher,
                )
                if args.include_raw_content:
                    warnings.append("raw_content_unavailable_on_ddg")
        except (HTTPFetchError, SSRFRejectedError, UnsupportedSchemeError) as e:
            await _emit_search_event(ctx, provider, args, hit_count=0, error=str(e))
            return ToolResult(content=f"{provider} search error: {e}", is_error=True)

        response.warnings.extend(warnings)
        await _emit_search_event(ctx, provider, args, hit_count=len(response.hits))
        return ToolResult(content=_render_search(response), structured_output=_as_dict(response))


def _render_search(response: SearchResponse) -> str:
    head = f"Answer: {response.answer}\n\n" if response.answer else ""
    if not response.hits:
        return f"{head}no results"
    body = "\n\n".join(
        f"{i + 1}. {h.title}\n   {h.url}\n   {h.snippet}" for i, h in enumerate(response.hits)
    )
    return f"{head}{body}"


def _as_dict(response: SearchResponse) -> dict[str, object]:
    return {
        "provider": response.provider,
        "query": response.query,
        "answer": response.answer,
        "warnings": response.warnings,
        "results": [
            {
                "title": h.title,
                "url": h.url,
                "snippet": h.snippet,
                "raw_content": h.raw_content,
                "published_at": h.published_at.isoformat() if h.published_at else None,
            }
            for h in response.hits
        ],
    }


async def _emit_search_event(
    ctx: ToolContext,
    provider: str,
    args: WebSearchArgs,
    *,
    hit_count: int,
    error: str | None = None,
) -> None:
    if ctx.record_event is None:
        return
    await ctx.record_event(
        web_search_performed(
            provider=provider,
            query=args.query,
            max_results=args.max_results,
            hit_count=hit_count,
            error=error,
        )
    )


# ── web_fetch ────────────────────────────────────────────────────────────────


class WebFetchArgs(BaseModel):
    url: str
    max_tokens: int = Field(default=4000, ge=200, le=20000)
    offset_tokens: int = Field(default=0, ge=0)


@tool
class WebFetchTool:
    name = "web_fetch"
    description = (
        "Fetch a URL and return its main content as markdown. HTML pages "
        "are extracted via trafilatura; plain text and JSON pass through. "
        "Use offset_tokens / max_tokens to page through long pages. "
        "For PDFs, RSS, or JS-rendered content, write a custom tool."
    )
    input_schema = WebFetchArgs
    annotations = ToolAnnotations(read_only=True, network=True)

    async def __call__(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        if ctx.http_fetcher is None:
            return ToolResult(
                content="web_fetch: HTTP transport not initialised (worker bug)",
                is_error=True,
            )
        fetcher = ctx.http_fetcher

        try:
            raw, headers, final_url = await fetcher.get(args.url)
        except (SSRFRejectedError, UnsupportedSchemeError) as e:
            await _emit_fetch_event(ctx, args, "", 0, 0, truncated=False, error=str(e))
            return ToolResult(content=f"fetch rejected: {e}", is_error=True)
        except ResponseTooLargeError as e:
            await _emit_fetch_event(ctx, args, "", 0, 0, truncated=True, error=str(e))
            return ToolResult(content=f"response too large: {e}", is_error=True)
        except HTTPFetchError as e:
            await _emit_fetch_event(ctx, args, "", 0, 0, truncated=False, error=str(e))
            return ToolResult(content=f"fetch failed: {e}", is_error=True)

        content_type = headers.get("content-type", "")
        bytes_in = len(raw)

        if is_html_content_type(content_type):
            extracted = extract_html(raw, url=final_url)
        elif is_text_content_type(content_type):
            extracted = extract_text(raw, content_type=content_type, url=final_url)
        else:
            await _emit_fetch_event(
                ctx, args, content_type, bytes_in, 0, truncated=False, error="unsupported"
            )
            return ToolResult(
                content=(
                    f"Unsupported content type: {content_type!r}. "
                    f"For PDFs, RSS, or binary content, write a custom tool "
                    f"under your agent's tools/ directory or wait for MCP "
                    f"integration in v0.2."
                ),
                is_error=True,
            )

        sliced = paginate(
            extracted.content_markdown,
            offset_tokens=args.offset_tokens,
            max_tokens=args.max_tokens,
        )

        await _emit_fetch_event(
            ctx,
            args,
            content_type,
            bytes_in,
            sliced.total_tokens,
            truncated=sliced.truncated,
        )

        return ToolResult(
            content=sliced.text,
            structured_output=_fetch_structured(args, final_url, content_type, extracted, sliced),
        )


def _fetch_structured(
    args: WebFetchArgs,
    final_url: str,
    content_type: str,
    extracted: ExtractedContent,
    sliced: object,
) -> dict[str, object]:
    # ``sliced`` is duck-typed to expose the pagination fields; importing
    # PaginatedSlice here would create a tight circular import path.
    truncated = getattr(sliced, "truncated", False)
    total_tokens = getattr(sliced, "total_tokens", 0)
    next_offset = getattr(sliced, "next_offset", None)
    return {
        "url": final_url,
        "title": extracted.title,
        "content_type": content_type,
        "metadata": extracted.metadata,
        "offset_tokens": args.offset_tokens,
        "total_tokens": total_tokens,
        "truncated": truncated,
        "next_offset": next_offset,
    }


async def _emit_fetch_event(
    ctx: ToolContext,
    args: WebFetchArgs,
    content_type: str,
    bytes_in: int,
    total_tokens: int,
    *,
    truncated: bool,
    error: str | None = None,
) -> None:
    if ctx.record_event is None:
        return
    await ctx.record_event(
        web_fetch_performed(
            url=args.url,
            content_type=content_type,
            bytes_in=bytes_in,
            offset_tokens=args.offset_tokens,
            total_tokens=total_tokens,
            truncated=truncated,
            error=error,
        )
    )
