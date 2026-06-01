"""SSRF guard helpers.

Refuses outbound HTTP targets that resolve to loopback, link-local,
RFC1918/CGNAT, multicast, broadcast, unspecified, or cloud-metadata
endpoints. Pure functions over the stdlib ``ipaddress`` module — no I/O
beyond a single ``getaddrinfo`` call in :func:`check_url`.

ADR-0004 §"`HTTPFetcher`" — the policy lives here for now; if a generic
network-egress policy emerges for other tools (e.g. ``send_email``
recipients, MCP transports) the helpers move to ``permissions/``. Not
before. See ``docs/plans/web-tools.md`` "Resolved decisions" §1.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import anyio

from ..errors import SSRFRejectedError, UnsupportedSchemeError

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# AWS / GCP / Azure / Oracle / Alibaba IMDS endpoints. The IPv4 link-local
# block (169.254.0.0/16) already covers most of these, but we list them
# explicitly so failures from a misconfigured allow-list still bite at the
# hostname stage.
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
    }
)

_METADATA_IPS: frozenset[str] = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, GCP, OCI
        "100.100.100.200",  # Alibaba
        "fd00:ec2::254",  # AWS IPv6
    }
)


def classify_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a human-readable reject reason if ``ip`` is forbidden, else ``None``.

    The order of checks favours the most-specific category so error
    messages tell the operator something useful ("metadata endpoint",
    not just "private network").
    """
    if str(ip) in _METADATA_IPS:
        return "cloud metadata endpoint"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if isinstance(ip, ipaddress.IPv4Address) and ip.is_reserved:
        return "reserved IPv4 address"
    if ip.is_private:
        # Covers RFC1918 (10/8, 172.16/12, 192.168/16), CGNAT (100.64/10
        # falls under is_private only via the broader unique-local check
        # for IPv6; for IPv4 we add it explicitly below), and ULA
        # (fc00::/7) for IPv6.
        return "private network"
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return "carrier-grade NAT"
    return None


def is_metadata_host(host: str) -> bool:
    """Hostname-level check for known cloud metadata names."""
    return host.lower() in _METADATA_HOSTS


def parse_url(url: str) -> tuple[str, str, int]:
    """Validate ``url`` shape and return ``(scheme, host, port)``.

    Raises :class:`UnsupportedSchemeError` for non-http(s) URLs and
    :class:`SSRFRejectedError` for syntactically valid URLs with no host.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsupportedSchemeError(url, scheme or "(missing)")
    if not parts.hostname:
        raise SSRFRejectedError(url, "missing hostname")
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, parts.hostname, port


async def resolve_host(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every address ``host`` resolves to.

    If ``host`` is itself an IP literal, return it without a DNS query.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    infos = await anyio.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    seen: set[str] = set()
    result: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for family, _stype, _proto, _canon, sockaddr in infos:
        addr_str = sockaddr[0] if isinstance(sockaddr, tuple) else ""
        if not addr_str or addr_str in seen:
            continue
        seen.add(addr_str)
        if family == socket.AF_INET6:
            result.append(ipaddress.IPv6Address(addr_str.split("%", 1)[0]))
        else:
            result.append(ipaddress.IPv4Address(addr_str))
    if not result:
        raise SSRFRejectedError(host, "name resolution returned no addresses")
    return result


async def check_url(url: str, *, allow_private_networks: bool = False) -> None:
    """Validate scheme + host, resolve, and reject any forbidden destination.

    Pre-connect SSRF check. Acknowledges a residual TOCTOU window between
    this resolution and the one httpx performs at connect time — closing
    that fully needs a custom resolver in the transport and is out of
    scope for v0.1 per ADR-0004.

    When ``allow_private_networks`` is true, the private/loopback/CGNAT
    categories are accepted but cloud-metadata endpoints and link-local
    addresses are still refused (they're load-bearing for SSRF
    regardless of operator preference).
    """
    _scheme, host, _port = parse_url(url)
    if is_metadata_host(host):
        raise SSRFRejectedError(url, "cloud metadata hostname")

    addrs = await resolve_host(host)
    for ip in addrs:
        reason = classify_ip(ip)
        if reason is None:
            continue
        if allow_private_networks and reason in {
            "loopback address",
            "private network",
            "carrier-grade NAT",
        }:
            continue
        raise SSRFRejectedError(url, f"{reason} ({ip})")
