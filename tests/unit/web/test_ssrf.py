"""SSRF guard — IP classification + check_url behaviour."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from eonlet.errors import SSRFRejectedError, UnsupportedSchemeError
from eonlet.web import ssrf


@pytest.mark.parametrize(
    ("ip", "expected_substring"),
    [
        ("127.0.0.1", "loopback"),
        ("169.254.1.1", "link-local"),
        ("169.254.169.254", "metadata"),
        ("10.0.0.1", "private"),
        ("172.16.5.1", "private"),
        ("192.168.0.5", "private"),
        ("100.64.1.1", "carrier-grade NAT"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("::1", "loopback"),
        ("fe80::1", "link-local"),
        ("fc00::1", "private"),
    ],
)
def test_classify_ip_rejects(ip: str, expected_substring: str) -> None:
    addr = ipaddress.ip_address(ip)
    reason = ssrf.classify_ip(addr)
    assert reason is not None, f"{ip} should be rejected"
    assert expected_substring in reason


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_classify_ip_allows_public(ip: str) -> None:
    assert ssrf.classify_ip(ipaddress.ip_address(ip)) is None


def test_parse_url_rejects_non_http() -> None:
    for scheme in ("file://etc/passwd", "ftp://example.com", "javascript:alert(1)"):
        with pytest.raises(UnsupportedSchemeError):
            ssrf.parse_url(scheme)


def test_parse_url_requires_host() -> None:
    with pytest.raises(SSRFRejectedError, match="missing hostname"):
        ssrf.parse_url("http:///path-only")


def test_parse_url_returns_default_ports() -> None:
    assert ssrf.parse_url("http://example.com/x") == ("http", "example.com", 80)
    assert ssrf.parse_url("https://example.com/x") == ("https", "example.com", 443)
    assert ssrf.parse_url("https://example.com:8443/x") == ("https", "example.com", 8443)


@pytest.mark.parametrize(
    "host",
    ["metadata.google.internal", "metadata", "instance-data", "METADATA"],
)
def test_metadata_host_detected(host: str) -> None:
    assert ssrf.is_metadata_host(host)


def test_metadata_host_allows_normal_names() -> None:
    assert not ssrf.is_metadata_host("example.com")
    assert not ssrf.is_metadata_host("api.anthropic.com")


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


async def test_check_url_rejects_metadata_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"metadata.google.internal": ["8.8.8.8"]})
    with pytest.raises(SSRFRejectedError, match="metadata"):
        await ssrf.check_url("https://metadata.google.internal/computeMetadata/v1/")


async def test_check_url_rejects_private_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"intranet.local": ["10.0.0.5"]})
    with pytest.raises(SSRFRejectedError, match="private network"):
        await ssrf.check_url("https://intranet.local/")


async def test_check_url_rejects_loopback_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    # IP literals don't hit the resolver; mapping is irrelevant.
    _patch_resolver(monkeypatch, {})
    with pytest.raises(SSRFRejectedError, match="loopback"):
        await ssrf.check_url("http://127.0.0.1:8080/api")


async def test_check_url_allows_public(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"example.com": ["93.184.216.34"]})
    await ssrf.check_url("https://example.com/")  # should not raise


async def test_check_url_allow_private_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"intranet.local": ["10.0.0.5"]})
    await ssrf.check_url("https://intranet.local/", allow_private_networks=True)


async def test_check_url_allow_private_still_blocks_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolver(monkeypatch, {"x.example": ["169.254.169.254"]})
    with pytest.raises(SSRFRejectedError):
        await ssrf.check_url("https://x.example/", allow_private_networks=True)


async def test_check_url_rejects_multi_resolution_any_bad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Public + private → still rejected (strictest interpretation).
    _patch_resolver(monkeypatch, {"mixed.example": ["8.8.8.8", "10.0.0.5"]})
    with pytest.raises(SSRFRejectedError, match="private network"):
        await ssrf.check_url("https://mixed.example/")


async def test_check_url_rejects_empty_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_resolver(monkeypatch, {"nowhere.example": []})
    with pytest.raises(SSRFRejectedError):
        await ssrf.check_url("https://nowhere.example/")


# Make pytest aware that callable references silence ruff's unused-import linter.
_ = (Awaitable, Callable)
