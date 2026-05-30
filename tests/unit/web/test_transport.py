"""HTTPFetcher — retries, size cap, SSRF wiring."""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest

from eonlet.errors import (
    HTTPFetchError,
    ResponseTooLargeError,
    SSRFRejectedError,
    UnsupportedSchemeError,
)
from eonlet.web.transport import HTTPFetcher


def _patch_resolver(
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, list[str]],
) -> None:
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


async def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("eonlet.web.transport._sleep", instant)


async def test_fetch_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    body, headers, final_url = await fetcher.get("https://example.com/")
    assert body == b"hello"
    assert headers["content-type"] == "text/plain"
    assert final_url == "https://example.com/"
    await fetcher.aclose()


async def test_fetch_rejects_unsupported_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = HTTPFetcher()
    with pytest.raises(UnsupportedSchemeError):
        await fetcher.get("file:///etc/passwd")
    await fetcher.aclose()


async def test_fetch_rejects_ssrf(monkeypatch: pytest.MonkeyPatch) -> None:
    fetcher = HTTPFetcher()
    with pytest.raises(SSRFRejectedError):
        await fetcher.get("http://127.0.0.1:8080/admin")
    await fetcher.aclose()


async def test_fetch_retries_on_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"flaky.example": ["8.8.8.8"]})
    await _no_backoff(monkeypatch)

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, content=b"bad gateway")
        return httpx.Response(200, content=b"finally", headers={"content-type": "text/plain"})

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    body, _h, _u = await fetcher.get("https://flaky.example/")
    assert body == b"finally"
    assert calls["n"] == 3
    await fetcher.aclose()


async def test_fetch_exhausts_retries_on_persistent_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"broken.example": ["8.8.8.8"]})
    await _no_backoff(monkeypatch)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    with pytest.raises(HTTPFetchError, match="retries exhausted"):
        await fetcher.get("https://broken.example/")
    await fetcher.aclose()


async def test_fetch_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"unauth.example": ["8.8.8.8"]})

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, content=b"missing")

    fetcher = HTTPFetcher()
    _install_mock_transport(fetcher, handler)
    body, _h, _u = await fetcher.get("https://unauth.example/")
    assert body == b"missing"
    assert calls["n"] == 1  # no retry on 4xx
    await fetcher.aclose()


async def test_fetch_aborts_on_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"huge.example": ["8.8.8.8"]})

    big = b"x" * 4096

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "text/plain"})

    fetcher = HTTPFetcher(max_bytes=1024)
    _install_mock_transport(fetcher, handler)
    with pytest.raises(ResponseTooLargeError):
        await fetcher.get("https://huge.example/")
    await fetcher.aclose()


async def test_fetch_per_call_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"override.example": ["8.8.8.8"]})

    big = b"y" * 4096

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "text/plain"})

    fetcher = HTTPFetcher(max_bytes=10 * 1024 * 1024)
    _install_mock_transport(fetcher, handler)
    # Per-call cap below body size should still trigger ResponseTooLarge.
    with pytest.raises(ResponseTooLargeError):
        await fetcher.get("https://override.example/", max_bytes=1024)
    await fetcher.aclose()


async def test_fetch_async_context_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"ctx.example": ["8.8.8.8"]})

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    async with HTTPFetcher() as fetcher:
        _install_mock_transport(fetcher, handler)
        body, _h, _u = await fetcher.get("https://ctx.example/")
        assert body == b"ok"
