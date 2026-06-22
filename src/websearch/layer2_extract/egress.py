"""SSRF egress guard.

A web-fetch tool an AI agent can point anywhere must not be coaxed into reaching
internal infrastructure. ``guard_url`` enforces an http(s) scheme allowlist (so the
libcurl tier never sees file://, gopher://, dict://) and resolves the host, rejecting
any address that is private, loopback, link-local (this covers the 169.254.169.254
cloud-metadata endpoint), reserved, multicast, or unspecified, unless the caller
explicitly opts in with ``allow_private``. It runs before the first request and again
on every redirect hop, so a public URL cannot 30x its way into the internal network.

Resolution closes the common case; it does not pin the resolved address, so a
deliberate DNS-rebind between this check and the client's own resolution remains a
known residual (documented). The resolver is injectable for tests.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


class BlockedEgress(Exception):
    """Raised when a URL is refused by the egress policy (not an anti-bot block)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _ip_is_internal(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # Allowlist, not denylist: an address is internal unless it is globally routable.
    # `is_global` is False for private, loopback, link-local (incl. the 169.254.169.254
    # cloud-metadata endpoint), reserved, multicast, unspecified, AND CGNAT 100.64.0.0/10
    # (RFC 6598) which an explicit denylist of the above flags silently misses. Requiring
    # is_global closes every non-public range at once and fails safe on future ones.
    return not ip.is_global


def _resolve(host: str) -> set[str]:
    """Resolve a hostname to the set of its IP addresses (all families)."""
    infos = socket.getaddrinfo(host, None)
    return {info[4][0] for info in infos}


def guard_url(
    url: str,
    *,
    allow_private: bool = False,
    resolve: Callable[[str], set[str]] | None = None,
) -> None:
    """Raise ``BlockedEgress`` if ``url`` violates the egress policy."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedEgress(f"refused: scheme '{parts.scheme or ''}' is not http or https")
    host = parts.hostname
    if not host:
        raise BlockedEgress("refused: url has no host")
    if allow_private:
        return

    # Literal IP: check directly without resolving.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_internal(literal):
            raise BlockedEgress(f"refused: {host} is a private or reserved address")
        return

    resolver = resolve or _resolve
    try:
        addrs = resolver(host)
    except OSError as exc:
        raise BlockedEgress(f"refused: cannot resolve host '{host}' ({exc})") from exc
    if not addrs:
        raise BlockedEgress(f"refused: host '{host}' resolved to no addresses")
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_internal(ip):
            raise BlockedEgress(
                f"refused: host '{host}' resolves to private or reserved address {addr}"
            )
