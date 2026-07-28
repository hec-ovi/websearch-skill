"""Optional egress proxy: one switch for every network path.

Two switches feed one answer. ``WEBSEARCH_PROXY`` names an egress proxy;
``WEBSEARCH_TOR`` says egress goes through the local Tor SOCKS port instead. When both
are on they are not in competition: ``egress_proxy`` hands out Tor's SOCKS address, and
the configured proxy is written into Tor's own torrc as its upstream (see
``tor_local.py``), so the path is you -> proxy -> Tor -> destination and neither hop is
silently dropped.

``WEBSEARCH_PROXY`` controls the proxy hop:

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

TOR_ENV = "WEBSEARCH_TOR"
TOR_SOCKS_VAR = "WEBSEARCH_TOR_SOCKS"
TOR_PORT_VAR = "WEBSEARCH_TOR_PORT"
TOR_DEFAULT_HOST = "127.0.0.1"
TOR_DEFAULT_PORT = 9050

# The words that mean "off" in any layer switch. One vocabulary across all of them is
# less to remember, and this is the definition the other layers import.
OFF_WORDS = {"", "off", "none", "no", "false", "0", "direct"}
# resolve_proxy reads a URL, not a switch, so it only spends the unambiguous words here;
# the layer state checks OFF_WORDS first, which is what makes WEBSEARCH_PROXY=false off
# rather than a hostname.
_OFF = {"", "off", "none", "direct"}
# The words that turn the tor layer on. Anything else that is not an off word is a
# typo rather than a value: the switch has no third setting.
_TOR_ON = {"on", "yes", "true", "1", "tor", "up", "enabled"}

# socks5h keeps name resolution at the Tor client, which is not a preference: a .onion
# name has no answer in the local resolver, and asking it would leak the lookup.
TOR_SCHEMES = ("socks5h://", "socks5://")


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
    """The configured egress proxy URL, or None for a direct connection.

    This is the ``WEBSEARCH_PROXY`` hop on its own. It does not know about Tor: callers
    that want the address to actually connect through want ``egress_proxy``.
    """
    value = cli_value if cli_value is not None else os.environ.get("WEBSEARCH_PROXY")
    if value is None or value.strip().lower() in _OFF:
        return None
    value = value.strip()
    if value.lower() == "nordvpn":
        return _nordvpn_url()
    return value


def tor_enabled(cli_value: str | None = None) -> bool:
    """Whether the tor layer is on. Off unless something says otherwise."""
    raw = cli_value if cli_value is not None else os.environ.get(TOR_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in OFF_WORDS:
        return False
    if value in _TOR_ON:
        return True
    raise ProxyConfigError(
        f"{TOR_ENV}={raw!r} is not a switch value; use 'on' or 'off' "
        f"(the SOCKS address is {TOR_SOCKS_VAR})."
    )


def tor_port() -> int:
    raw = os.environ.get(TOR_PORT_VAR, "").strip()
    if not raw:
        return TOR_DEFAULT_PORT
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProxyConfigError(f"{TOR_PORT_VAR}={raw!r} is not a port number") from exc
    if not 1 <= value <= 65535:
        raise ProxyConfigError(f"{TOR_PORT_VAR}={value} is outside 1-65535")
    return value


def tor_socks_url() -> str:
    """Where the Tor SOCKS port is: the override, else loopback on the configured port."""
    explicit = os.environ.get(TOR_SOCKS_VAR, "").strip()
    if not explicit:
        return f"socks5h://{TOR_DEFAULT_HOST}:{tor_port()}"
    if not explicit.lower().startswith(TOR_SCHEMES):
        raise ProxyConfigError(
            f"{TOR_SOCKS_VAR}={explicit!r} must be a socks5h:// or socks5:// URL, "
            "e.g. socks5h://127.0.0.1:9050"
        )
    return explicit


def egress_proxy(cli_value: str | None = None, tor_value: str | None = None) -> str | None:
    """The address every network path should actually connect through, or None.

    Tor answers first when it is on. That is not the configured proxy being ignored: the
    proxy becomes Tor's upstream in torrc, so it is still in the path, one hop earlier.
    """
    if tor_enabled(tor_value):
        return tor_socks_url()
    return resolve_proxy(cli_value)


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


def as_fetch_proxy(url: str | None, *, tor: bool | None = None) -> dict | None:
    """A ``FetchRequest``-shaped proxy dict for Layer 2A, or None.

    ``tor`` marks the hop as a Tor SOCKS port, which is what tells the egress guard that
    a ``.onion`` target is reachable rather than a name nothing can resolve. It defaults
    to the current layer state, so a caller that already resolved the URL through
    ``egress_proxy`` does not have to say it twice.
    """
    if not url:
        return None
    return {"url": url, "type": proxy_type(url), "tor": tor_enabled() if tor is None else tor}
