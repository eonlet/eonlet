"""Search backends — Tavily + DDG direct calls (separate from tool surface)."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from eonlet.errors import HTTPFetchError
from eonlet.web import HTTPFetcher
from eonlet.web.search import ddg_search, tavily_search


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


def _install_mock(fetcher: HTTPFetcher, handler: Any) -> None:
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=fetcher.timeout,
        headers={"User-Agent": fetcher.user_agent},
    )


# ── Tavily ───────────────────────────────────────────────────────────────────


async def test_tavily_search_maps_response(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "synthesized",
                "results": [
                    {
                        "title": "Page 1",
                        "url": "https://a.example/",
                        "content": "snippet 1",
                        "raw_content": "full body 1",
                        "published_date": "2026-04-01T12:00:00Z",
                    },
                    {
                        "title": "Page 2",
                        "url": "https://b.example/",
                        "content": "snippet 2",
                    },
                ],
            },
        )

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)

    response = await tavily_search(
        query="claude",
        max_results=2,
        include_raw_content=True,
        fetcher=fetcher,
        api_key="test-key",
    )
    await fetcher.aclose()

    assert response.provider == "tavily"
    assert response.answer == "synthesized"
    assert len(response.hits) == 2
    assert response.hits[0].raw_content == "full body 1"
    assert response.hits[0].published_at is not None
    assert response.hits[1].published_at is None


async def test_tavily_search_advanced_depth_when_raw_content_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)

    await tavily_search(
        query="x",
        max_results=5,
        include_raw_content=True,
        fetcher=fetcher,
        api_key="k",
    )
    await fetcher.aclose()

    assert captured["body"]["search_depth"] == "advanced"
    assert captured["body"]["include_raw_content"] is True


async def test_tavily_search_basic_depth_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)

    await tavily_search(
        query="x",
        max_results=5,
        include_raw_content=False,
        fetcher=fetcher,
        api_key="k",
    )
    await fetcher.aclose()

    assert captured["body"]["search_depth"] == "basic"


async def test_tavily_search_missing_key_raises() -> None:
    fetcher = HTTPFetcher()
    with pytest.raises(HTTPFetchError, match="TAVILY_API_KEY"):
        await tavily_search(
            query="x",
            max_results=1,
            include_raw_content=False,
            fetcher=fetcher,
            api_key="",
        )
    await fetcher.aclose()


async def test_tavily_search_handles_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"api.tavily.com": ["8.8.8.8"]})

    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("eonlet.web.transport._sleep", instant)

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, content=b"server down")

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)
    with pytest.raises(HTTPFetchError, match="retries exhausted"):
        await tavily_search(
            query="x",
            max_results=1,
            include_raw_content=False,
            fetcher=fetcher,
            api_key="k",
        )
    assert calls["n"] == 4  # 1 initial + 3 retries
    await fetcher.aclose()


# ── DDG ──────────────────────────────────────────────────────────────────────


_DDG_HTML = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.example%2Fone">First Result</a>
<a class="result__snippet">first snippet</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fb.example%2Ftwo">Second &amp; Third</a>
<a class="result__snippet">second snippet</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fc.example%2Fthree">Third Result</a>
<a class="result__snippet">third snippet</a>
</body></html>
"""


async def test_ddg_search_extracts_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"duckduckgo.com": ["8.8.8.8"]})

    def handler(request: httpx.Request) -> httpx.Response:
        assert "q=" in str(request.url.query)
        return httpx.Response(200, content=_DDG_HTML, headers={"content-type": "text/html"})

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)

    response = await ddg_search(query="anthropic", max_results=2, fetcher=fetcher)
    await fetcher.aclose()

    assert response.provider == "ddg"
    assert len(response.hits) == 2  # max_results respected
    assert response.hits[0].title == "First Result"
    assert response.hits[0].url == "https://a.example/one"
    assert response.hits[1].title == "Second & Third"  # entities decoded


async def test_ddg_search_no_hits_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"duckduckgo.com": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    fetcher = HTTPFetcher()
    _install_mock(fetcher, handler)
    response = await ddg_search(query="x", max_results=5, fetcher=fetcher)
    await fetcher.aclose()
    assert response.hits == []
