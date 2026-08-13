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

from ..proxy import EGRESS_LOCK_VAR, bypasses_proxy, lock_enabled

ALLOWED_SCHEMES = ("http", "https")

ONION_TLD = ".onion"


class BlockedEgress(Exception):
    """Raised when a URL is refused by the egress policy (not an anti-bot block)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def is_onion(url: str) -> bool:
    """Whether this URL names a Tor onion service."""
    host = (urlsplit(url).hostname or "").lower()
    return host.endswith(ONION_TLD)


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
    proxied: bool = False,
    tor: bool = False,
) -> None:
    """Raise ``BlockedEgress`` if ``url`` violates the egress policy.

    ``proxied`` says the request will be made by a remote exit node rather than by this
    machine. That changes what the hostname check is worth: see below. ``tor`` says that
    exit is a Tor client, which is the only case where a ``.onion`` name means anything.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedEgress(f"refused: scheme '{parts.scheme or ''}' is not http or https")
    host = parts.hostname
    if not host:
        raise BlockedEgress("refused: url has no host")
    if not proxied and not allow_private and lock_enabled() and not bypasses_proxy(url):
        # The lock's last line: a fetcher handed no proxy would open this socket from the
        # machine's own address. Refused here rather than in the caller, because this guard
        # is the one thing every tier passes through.
        raise BlockedEgress(
            f"refused: {EGRESS_LOCK_VAR} is on and this fetch has no proxy, so it would "
            "leave from your own address"
        )
    if host.lower().endswith(ONION_TLD):
        # Checked before allow_private, and before anything resolves: an onion name has no
        # answer outside Tor, so without the layer this is not a fetch that might work but
        # a lookup that leaks the address to the local resolver on its way to failing.
        if not tor:
            raise BlockedEgress(
                f"refused: {host} is a Tor onion service and the tor layer is off. "
                "Start it with `websearch tor up`, then retry."
            )
        return
    if allow_private:
        return

    # Literal IP: check directly without resolving. This runs on every path, proxied or
    # not, so http://127.0.0.1 and the 169.254.169.254 metadata endpoint stay refused.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_internal(literal):
            raise BlockedEgress(f"refused: {host} is a private or reserved address")
        return

    if proxied:
        # Behind an egress proxy, resolving the hostname here is both a leak and a lie.
        # A leak because the local resolver, and therefore the ISP, sees every host the
        # tool visits even though the traffic itself is tunneled, which is the one thing
        # the proxy was turned on to prevent. A lie because a socks5h proxy resolves the
        # name at the exit node, so this lookup never decides where the connection goes:
        # "internal" would mean the exit node's network, which our resolver cannot see
        # and our answer cannot bind. The literal-IP check above still holds, and the
        # remote exit is not a route into this machine's LAN, which is what the guard
        # exists to prevent.
        return

    resolver = resolve or _resolve
    try:
        addrs = resolver(host)
    except OSError as exc:
        raise BlockedEgress(f"refused: cannot resolve host '{host}' ({exc})") from exc
    if not addrs:
        raise BlockedEgress(f"refused: host '{host}' resolved to no addresses")
    parsed_any = False
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        parsed_any = True
        if _ip_is_internal(ip):
            raise BlockedEgress(
                f"refused: host '{host}' resolves to private or reserved address {addr}"
            )
    # Fail closed: a resolver whose every entry is unparseable must not slip through
    # (the loop above would otherwise skip everything and allow the URL).
    if not parsed_any:
        raise BlockedEgress(f"refused: host '{host}' resolved to no parseable addresses")
