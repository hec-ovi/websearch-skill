"""Optional egress proxy: one switch for every network path.

``WEBSEARCH_PROXY`` controls it:

- unset, empty, ``off``, ``none``, or ``direct``: direct connection (the default).
- ``nordvpn``: NordVPN SOCKS5, expanded to ``socks5h://USER:PASS@HOST:1080`` from
  ``NORDVPN_USER`` / ``NORDVPN_PASS`` (the service credentials shown in the Nord
  Account dashboard under "Set up NordVPN manually", not the account login) and
  optional ``NORDVPN_HOST`` (default ``nl.socks.nordhold.net``; any official
  ``*.socks.nordhold.net`` server works).
- any other value: used verbatim as the proxy URL
  (``http://``, ``https://``, ``socks5://``, ``socks5h://``).

A ``--proxy`` CLI value takes precedence over the environment and accepts the same
three forms, so ``--proxy off`` forces a direct connection even when the variable is
set. ``socks5h`` resolves DNS through the proxy, so target hostnames never reach the
local resolver.

One exception: a host on your own machine or LAN is never proxied. Sending
``http://127.0.0.1:8888`` (a self-hosted SearXNG) through a remote SOCKS exit asks that
exit to connect to *its* loopback, which fails every time. See ``bypasses_proxy``.
"""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import quote, urlsplit

NORDVPN_DEFAULT_HOST = "nl.socks.nordhold.net"
NORDVPN_PORT = 1080

_OFF = {"", "off", "none", "direct"}


class ProxyConfigError(ValueError):
    """A proxy was requested but its configuration is unusable."""


def _nordvpn_url() -> str:
    user = os.environ.get("NORDVPN_USER", "")
    password = os.environ.get("NORDVPN_PASS", "")
    if not user or not password:
        raise ProxyConfigError(
            "proxy 'nordvpn' needs NORDVPN_USER and NORDVPN_PASS set to the NordVPN "
            "service credentials (Nord Account dashboard, 'Set up NordVPN manually'; "
            "these are not the account email/password)."
        )
    host = os.environ.get("NORDVPN_HOST", NORDVPN_DEFAULT_HOST)
    return f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{NORDVPN_PORT}"


def resolve_proxy(cli_value: str | None = None) -> str | None:
    """The effective egress proxy URL, or None for a direct connection."""
    value = cli_value if cli_value is not None else os.environ.get("WEBSEARCH_PROXY")
    if value is None or value.strip().lower() in _OFF:
        return None
    value = value.strip()
    if value.lower() == "nordvpn":
        return _nordvpn_url()
    return value


_LOCAL_SUFFIXES = (".local", ".localdomain", ".internal", ".home.arpa")


def bypasses_proxy(url: str | None) -> bool:
    """True when ``url`` names a host on this machine or this network.

    An egress proxy exists to change where requests leave the internet from. A
    self-hosted service on loopback or a LAN address is not on the internet, and a
    remote exit node resolving ``localhost`` would reach its own machine, so proxying
    it is always wrong rather than merely wasteful. Hostnames that are not obviously
    local are left to the proxy: guessing would mean a DNS lookup on every call, and
    leaking a lookup for a host the user wanted resolved proxy-side.
    """
    if not url:
        return False
    host = urlsplit(url if "://" in url else f"//{url}").hostname
    if not host:
        return False
    host = host.lower()
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def proxy_for(target_url: str | None, proxy: str | None) -> str | None:
    """The proxy to use when talking to ``target_url``: None for a local target."""
    return None if bypasses_proxy(target_url) else proxy


def proxy_type(url: str) -> str:
    """The Layer 2A ``Proxy.type`` for a proxy URL."""
    return "socks5" if url.lower().startswith("socks") else "http"


def as_fetch_proxy(url: str | None) -> dict | None:
    """A ``FetchRequest``-shaped proxy dict for Layer 2A, or None."""
    if not url:
        return None
    return {"url": url, "type": proxy_type(url)}
