"""web_search + web_fetch — exercise the new ADR-0004 pipeline.

Uses ``httpx.MockTransport`` to drive a real ``HTTPFetcher`` instead of
mocking httpx itself; this mirrors how the worker constructs the fetcher
and keeps the tests honest about retry / SSRF behaviour.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import httpx
import pytest

from eonlet.runtime.events import Event, EventKind
from eonlet.tools.builtin.web import (
    WebFetchArgs,
    WebFetchTool,
    WebSearchArgs,
    WebSearchTool,
)
from eonlet.tools.protocol import ToolContext
from eonlet.web import HTTPFetcher


def _patch_resolver(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    async def fake_getaddrinfo(
        host: str, _port: int | None, *, type: int = socket.SOCK_STREAM, **_: Any
    ) -> list[tuple[int, int, int, str, tuple[Any, ...]]]:
        if host not in mapping:
            raise OSError(f"no such host: {host}")
        out: list[tuple[int, int, int, str, tuple[Any, ...]]] = []
        for addr in mapping[host]:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            sockaddr: tuple[Any, ...] = (addr, 0, 0, 0) if family == socket.AF_INET6 else (addr, 0)
            out.append((family, type, socket.IPPROTO_TCP, "", sockaddr))
        return out

    import anyio

    monkeypatch.setattr(anyio, "getaddrinfo", fake_getaddrinfo)


def _install_mock_transport(fetcher: HTTPFetcher, handler: Any) -> None:
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=fetcher.timeout,
        headers={"User-Agent": fetcher.user_agent},
    )


def _build_ctx(tmp_path: Path, fetcher: HTTPFetcher) -> tuple[ToolContext, list[Event]]:
    captured: list[Event] = []

    async def record_event(event: Event) -> Event:
        captured.append(event)
        return event

    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
        http_fetcher=fetcher,
        record_event=record_event,
    )
    return ctx, captured


# ── web_search ───────────────────────────────────────────────────────────────


async def test_web_search_uses_tavily_when_key_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        body = json.loads(request.content)
        assert body["api_key"] == "test-key"
        assert body["query"] == "anthropic"
        return httpx.Response(
            200,
            json={
                "answer": "Anthropic builds Claude.",
                "results": [
                    {
                        "title": "Anthropic",
                        "url": "https://anthropic.com",
                        "content": "Snippet",
                        "raw_content": None,
                    }
                ],
            },
            headers={"content-type": "application/json"},
        )

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebSearchTool()(WebSearchArgs(query="anthropic"), ctx)
    await fetcher.aclose()

    assert not result.is_error
    assert "Anthropic" in result.content
    assert "Anthropic builds Claude." in result.content
    assert result.structured_output is not None
    assert result.structured_output["provider"] == "tavily"
    assert events and events[0].kind == EventKind.WEB_SEARCH_PERFORMED
    assert events[0].payload["provider"] == "tavily"


async def test_web_search_falls_back_to_ddg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _patch_resolver(monkeypatch, {"duckduckgo.com": ["8.8.8.8"]})

    ddg_html = """
    <html><body>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">
      Example A
    </a>
    <a class="result__snippet">snippet A</a>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">
      Example B
    </a>
    <a class="result__snippet">snippet B</a>
    </body></html>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "duckduckgo.com"
        return httpx.Response(200, content=ddg_html, headers={"content-type": "text/html"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebSearchTool()(WebSearchArgs(query="hi", max_results=2), ctx)
    await fetcher.aclose()

    assert not result.is_error
    assert "Example A" in result.content
    assert result.structured_output is not None
    assert result.structured_output["provider"] == "ddg"
    urls = [r["url"] for r in result.structured_output["results"]]
    assert "https://example.com/a" in urls
    assert events[0].payload["provider"] == "ddg"
    assert events[0].payload["hit_count"] == 2


async def test_web_search_warns_when_include_raw_on_ddg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    _patch_resolver(monkeypatch, {"duckduckgo.com": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, _events = _build_ctx(tmp_path, fetcher)

    result = await WebSearchTool()(WebSearchArgs(query="hi", include_raw_content=True), ctx)
    await fetcher.aclose()

    assert result.structured_output is not None
    assert "raw_content_unavailable_on_ddg" in result.structured_output["warnings"]


async def test_web_search_missing_fetcher_returns_error(tmp_path: Path) -> None:
    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
    )
    result = await WebSearchTool()(WebSearchArgs(query="x"), ctx)
    assert result.is_error


async def test_web_search_records_error_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, content=b"unauthorized")

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebSearchTool()(WebSearchArgs(query="x"), ctx)
    await fetcher.aclose()

    assert result.is_error
    assert events[0].kind == EventKind.WEB_SEARCH_PERFORMED
    assert events[0].payload.get("error")
    assert events[0].payload["hit_count"] == 0


# ── web_fetch ────────────────────────────────────────────────────────────────


_PAGE_HTML = b"""<!doctype html><html><head><title>Sample Page</title></head>
<body><article><h1>Sample Page</h1>
<p>This is a long paragraph of body text that trafilatura should detect as
the main content of the page. The paragraph repeats. The paragraph repeats.
The paragraph repeats. The paragraph repeats.</p></article></body></html>
"""


async def test_web_fetch_html_returns_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PAGE_HTML, headers={"content-type": "text/html"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebFetchTool()(WebFetchArgs(url="https://example.com/post"), ctx)
    await fetcher.aclose()

    assert not result.is_error
    assert "trafilatura should detect" in result.content
    assert result.structured_output is not None
    assert result.structured_output["title"] == "Sample Page"
    assert result.structured_output["content_type"] == "text/html"
    assert events[-1].kind == EventKind.WEB_FETCH_PERFORMED
    assert events[-1].payload["bytes_in"] == len(_PAGE_HTML)
    assert events[-1].payload["content_type"] == "text/html"


async def test_web_fetch_paginates_long_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolver(monkeypatch, {"big.example": ["8.8.8.8"]})

    # ~5k tokens of plain text so two pages of 200 tokens get returned.
    big_body = ("hello world. " * 2000).encode("utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big_body, headers={"content-type": "text/plain"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    page1 = await WebFetchTool()(WebFetchArgs(url="https://big.example/", max_tokens=200), ctx)
    assert page1.structured_output is not None
    assert page1.structured_output["truncated"] is True
    next_offset = page1.structured_output["next_offset"]
    assert next_offset == 200

    page2 = await WebFetchTool()(
        WebFetchArgs(url="https://big.example/", max_tokens=200, offset_tokens=next_offset),
        ctx,
    )
    await fetcher.aclose()

    assert page2.structured_output is not None
    assert page2.structured_output["offset_tokens"] == 200
    assert events[0].payload["offset_tokens"] == 0
    assert events[1].payload["offset_tokens"] == 200


async def test_web_fetch_unsupported_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_resolver(monkeypatch, {"pdf.example": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"%PDF-1.4\n", headers={"content-type": "application/pdf"}
        )

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebFetchTool()(WebFetchArgs(url="https://pdf.example/doc.pdf"), ctx)
    await fetcher.aclose()

    assert result.is_error
    assert "Unsupported content type" in result.content
    assert "custom tool" in result.content
    assert events[-1].payload.get("error") == "unsupported"


async def test_web_fetch_ssrf_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No resolver patch needed — 127.0.0.1 is an IP literal.
    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover — never called
        return httpx.Response(200, content=b"x")

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    ctx, events = _build_ctx(tmp_path, fetcher)

    result = await WebFetchTool()(WebFetchArgs(url="http://127.0.0.1:8080/admin"), ctx)
    await fetcher.aclose()

    assert result.is_error
    assert "fetch rejected" in result.content
    assert events[-1].kind == EventKind.WEB_FETCH_PERFORMED
    assert events[-1].payload.get("error")


async def test_web_fetch_missing_fetcher_returns_error(tmp_path: Path) -> None:
    ctx = ToolContext(
        eonlet_id="t.x",
        workspace=tmp_path,
        memory_dir=tmp_path,
        skills={},
        env={},
    )
    result = await WebFetchTool()(WebFetchArgs(url="https://x.example/"), ctx)
    assert result.is_error
