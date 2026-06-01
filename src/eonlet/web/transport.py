"""``HTTPFetcher`` — the worker-level outbound HTTP client.

Wraps ``httpx.AsyncClient`` with:

* scheme + SSRF check before the connection opens
* exponential-backoff retry on transport errors and 5xx
* streamed body with a hard ``max_bytes`` ceiling
* a project-stable ``User-Agent`` header

Both :meth:`get` and :meth:`post_json` share one retry + size-cap path so
the two callers (``web_fetch`` and Tavily) get identical reliability
guarantees. Per ADR-0004 §"HTTPFetcher" the fetcher is one instance per
worker process — see ``ToolContext.http_fetcher``. It does not enforce
``robots.txt`` (single-user local context) and does not cache responses
(joint design with v0.2 hooks).
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version
from typing import Final

import httpx

from ..errors import HTTPFetchError, ResponseTooLargeError
from .ssrf import check_url

DEFAULT_MAX_BYTES: Final[int] = 10 * 1024 * 1024
DEFAULT_TIMEOUT_S: Final[float] = 30.0
_RETRY_BACKOFF_S: Final[tuple[float, ...]] = (0.5, 1.0, 2.0)


def _default_user_agent() -> str:
    try:
        eonlet_version = version("eonlet")
    except PackageNotFoundError:  # editable install before metadata is built
        eonlet_version = "0.0.0"
    return f"Eonlet/{eonlet_version} (+https://eonlet.dev)"


class HTTPFetcher:
    """Outbound HTTP client with SSRF guard, retries, and size cap.

    The class owns one ``httpx.AsyncClient``; call :meth:`aclose` when the
    worker shuts down. Per-call timeouts and bytes-caps are configurable
    via :meth:`get` / :meth:`post_json` arguments — the constructor sets
    the defaults.
    """

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = DEFAULT_TIMEOUT_S,
        allow_private_networks: bool = False,
        user_agent: str | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.allow_private_networks = allow_private_networks
        self.user_agent = user_agent or _default_user_agent()
        self._client = httpx.AsyncClient(
            http2=True,  # ADR-0004 §"HTTPFetcher"; requires the `h2` package
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": self.user_agent},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HTTPFetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def get(
        self,
        url: str,
        *,
        max_bytes: int | None = None,
        timeout_s: float | None = None,
    ) -> tuple[bytes, httpx.Headers, str]:
        """Fetch ``url`` and return ``(body, response_headers, final_url)``.

        Raises :class:`SSRFRejectedError` / :class:`UnsupportedSchemeError`
        if the target is forbidden, :class:`ResponseTooLargeError` if
        streamed bytes exceed the cap, and :class:`HTTPFetchError` if all
        retries fail. 4xx responses are returned as-is so the caller can
        surface the status to the agent.
        """
        await check_url(url, allow_private_networks=self.allow_private_networks)
        cap = self.max_bytes if max_bytes is None else max_bytes
        per_call_timeout = self.timeout if timeout_s is None else timeout_s

        async def attempt() -> tuple[bytes, httpx.Headers, str]:
            return await self._stream("GET", url, cap, per_call_timeout)

        return await self._run_with_retry(url, attempt)

    async def post_json(
        self,
        url: str,
        payload: dict[str, object],
        *,
        max_bytes: int | None = None,
        timeout_s: float | None = None,
    ) -> tuple[bytes, httpx.Headers, str]:
        """POST a JSON body. Same retry + size-cap + SSRF policy as :meth:`get`.

        Encodes ``payload`` as JSON and streams the response so the
        ``max_bytes`` cap also bounds the response (Tavily ``raw_content``
        replies can be large).
        """
        await check_url(url, allow_private_networks=self.allow_private_networks)
        cap = self.max_bytes if max_bytes is None else max_bytes
        per_call_timeout = self.timeout if timeout_s is None else timeout_s
        body = _json.dumps(payload).encode("utf-8")
        headers = {"content-type": "application/json"}

        async def attempt() -> tuple[bytes, httpx.Headers, str]:
            return await self._stream(
                "POST", url, cap, per_call_timeout, content=body, headers=headers
            )

        return await self._run_with_retry(url, attempt)

    async def _run_with_retry(
        self,
        url: str,
        attempt: Callable[[], Awaitable[tuple[bytes, httpx.Headers, str]]],
    ) -> tuple[bytes, httpx.Headers, str]:
        last_exc: Exception | None = None
        for i in range(len(_RETRY_BACKOFF_S) + 1):
            try:
                return await attempt()
            except ResponseTooLargeError:
                raise
            except _RetryableSignal as e:
                last_exc = e.original
                if i >= len(_RETRY_BACKOFF_S):
                    break
                await _sleep(_RETRY_BACKOFF_S[i])
                continue
            except httpx.HTTPError as e:
                raise HTTPFetchError(url, str(e)) from e

        assert last_exc is not None
        raise HTTPFetchError(url, f"retries exhausted: {last_exc}") from last_exc

    async def _stream(
        self,
        method: str,
        url: str,
        cap: int,
        timeout_s: float,
        *,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[bytes, httpx.Headers, str]:
        chunks: list[bytes] = []
        total = 0
        async with self._client.stream(
            method, url, content=content, headers=headers, timeout=timeout_s
        ) as response:
            if 500 <= response.status_code < 600:
                await response.aread()
                raise _RetryableSignal(
                    httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                )
            try:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > cap:
                        raise ResponseTooLargeError(url, cap)
                    chunks.append(chunk)
            except httpx.TransportError as e:
                raise _RetryableSignal(e) from e
            return b"".join(chunks), response.headers, str(response.url)


class _RetryableSignal(Exception):  # noqa: N818  — internal marker, not an *Error
    """Wraps an exception so the retry loop can distinguish it from terminal errors."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


async def _sleep(seconds: float) -> None:
    # Indirected so tests can monkeypatch without touching anyio internals.
    import anyio

    await anyio.sleep(seconds)
