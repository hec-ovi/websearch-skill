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
"""

from __future__ import annotations

import os
from urllib.parse import quote

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


def proxy_type(url: str) -> str:
    """The Layer 2A ``Proxy.type`` for a proxy URL."""
    return "socks5" if url.lower().startswith("socks") else "http"


def as_fetch_proxy(url: str | None) -> dict | None:
    """A ``FetchRequest``-shaped proxy dict for Layer 2A, or None."""
    if not url:
        return None
    return {"url": url, "type": proxy_type(url)}
