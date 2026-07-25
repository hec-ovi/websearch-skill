"""The three optional layers, all off unless you turn them on.

Nothing here changes what the tool does by default. The base install searches with the
keyless engines over a direct connection, and each of these adds one thing on top:

- ``vpn``     (``WEBSEARCH_VPN``)          the host's traffic is expected to leave through
                                           a VPN tunnel. Declarative: the tunnel is the
                                           OS/app's job, and setting this tells ``websearch
                                           doctor`` to verify it rather than assume it.
- ``proxy``   (``WEBSEARCH_PROXY``)        every network path in the tool leaves through
                                           one egress proxy URL. See ``proxy.py``.
- ``searxng`` (``WEBSEARCH_SEARXNG_URL``)  a self-hosted SearXNG joins the Layer-1 fanout.

Each is off when its variable is unset, empty, or one of the off words, so a fresh
install has all three off and no way to be surprised by one.

Credentials live in these values (a proxy URL carries user:pass), so every display path
goes through ``redact_url``/``scrub``. The doctor prints layer state constantly; it must
never print a password.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from .proxy import ProxyConfigError, resolve_proxy

VPN_ENV = "WEBSEARCH_VPN"
PROXY_ENV = "WEBSEARCH_PROXY"
SEARXNG_ENV = "WEBSEARCH_SEARXNG_URL"

LAYER_NAMES = ("vpn", "proxy", "searxng")

# Words that mean "off" in any of the three switches. `direct`/`none` are here because
# proxy.py already accepts them, and one vocabulary across the three is less to remember.
OFF_WORDS = {"", "off", "none", "no", "false", "0", "direct"}

# WEBSEARCH_VPN values. `nordvpn` is verifiable (NordVPN publishes a keyless endpoint that
# reports whether the caller is behind their network); `any` only asserts that egress is
# tunneled somehow, which no third party can confirm for us.
VPN_NORDVPN = "nordvpn"
VPN_ANY = "any"
_VPN_ANY_ALIASES = {"any", "on", "yes", "true", "1", "vpn"}


def redact_url(url: str | None) -> str | None:
    """A display-safe URL: userinfo replaced with ``***:***``.

    Applied to every proxy URL that reaches a log line, an error message, or the doctor
    payload. The host and port survive because they are what you need to read the output.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if not (parts.username or parts.password):
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def secrets_of(url: str | None) -> list[str]:
    """The substrings of ``url`` that must never be printed: its username and password."""
    if not url:
        return []
    try:
        parts = urlsplit(url)
    except ValueError:
        return [url]
    return [value for value in (parts.username, parts.password) if value]


def scrub(text: str, secrets: list[str]) -> str:
    """Replace every secret with ``***``.

    Longest first, so removing a username does not leave a longer secret containing it
    half-matched and still readable.
    """
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def scrub_proxy(text: str, url: str | None) -> str:
    """Make text that may quote a proxy URL safe to print.

    Exception text from an HTTP client routinely embeds the proxy URL it failed to reach,
    credentials and all. Swapping the URL for its redacted form first keeps the host and
    port, which are the part you need to debug, and drops only the userinfo.
    """
    if not url:
        return text
    return scrub(text.replace(url, redact_url(url) or "***"), secrets_of(url))


@dataclass(frozen=True)
class LayerState:
    """Whether one optional layer is on, what turned it on, and its display-safe value."""

    name: str
    enabled: bool
    source: str | None  # the env var or CLI flag that turned it on
    value: str | None  # redacted; safe to print
    detail: str  # one line of human explanation, including how to turn it on
    error: str | None = None  # configured but unusable (e.g. nordvpn without credentials)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "source": self.source,
            "value": self.value,
            "detail": self.detail,
            "error": self.error,
        }


def _is_off(value: str | None) -> bool:
    return value is None or value.strip().lower() in OFF_WORDS


def vpn_state(cli_value: str | None = None) -> LayerState:
    """The VPN layer: off, ``nordvpn``, or ``any``.

    This never routes anything. A VPN tunnel is set up by the OS or the provider's app;
    declaring it here is how you ask the doctor to check that it is actually up, so a
    silently-dropped tunnel fails a check instead of leaking your real IP unnoticed.
    """
    source = "--vpn" if cli_value is not None else VPN_ENV
    raw = cli_value if cli_value is not None else os.environ.get(VPN_ENV)
    if _is_off(raw):
        return LayerState(
            name="vpn",
            enabled=False,
            source=None,
            value=None,
            detail=f"off (set {VPN_ENV}=nordvpn, or =any for a provider-agnostic tunnel check)",
        )
    value = raw.strip().lower()  # type: ignore[union-attr]
    if value in _VPN_ANY_ALIASES:
        return LayerState(
            name="vpn",
            enabled=True,
            source=source,
            value=VPN_ANY,
            detail="a tunnel is expected; the provider is not asserted",
        )
    if value == VPN_NORDVPN:
        return LayerState(
            name="vpn",
            enabled=True,
            source=source,
            value=VPN_NORDVPN,
            detail="egress is expected to be behind NordVPN",
        )
    return LayerState(
        name="vpn",
        enabled=True,
        source=source,
        value=value,
        detail=f"unknown VPN '{value}'",
        error=(
            f"{source} accepts 'off', 'nordvpn', or 'any'; got '{value}'. "
            "Use 'any' for a provider this tool cannot verify by name."
        ),
    )


def proxy_state(cli_value: str | None = None) -> LayerState:
    """The egress-proxy layer: off, or one resolved proxy URL for every network path."""
    source = "--proxy" if cli_value is not None else PROXY_ENV
    raw = cli_value if cli_value is not None else os.environ.get(PROXY_ENV)
    if _is_off(raw):
        return LayerState(
            name="proxy",
            enabled=False,
            source=None,
            value=None,
            detail=f"off (set {PROXY_ENV} to a proxy URL, or to 'nordvpn')",
        )
    try:
        resolved = resolve_proxy(raw)
    except ProxyConfigError as exc:
        return LayerState(
            name="proxy",
            enabled=True,
            source=source,
            value=None,
            detail=f"{source}={raw!r} could not be resolved",
            error=str(exc),
        )
    return LayerState(
        name="proxy",
        enabled=True,
        source=source,
        value=redact_url(resolved),
        detail="every network path leaves through this proxy (local hosts stay direct)",
    )


def searxng_state(cli_value: str | None = None) -> LayerState:
    """The SearXNG layer: off, or one base URL joining the Layer-1 fanout."""
    source = "--searxng-url" if cli_value is not None else SEARXNG_ENV
    raw = cli_value if cli_value is not None else os.environ.get(SEARXNG_ENV)
    if _is_off(raw):
        return LayerState(
            name="searxng",
            enabled=False,
            source=None,
            value=None,
            detail=f"off (set {SEARXNG_ENV}; ./docker/searxng/searxng.sh up starts one)",
        )
    url = raw.strip().rstrip("/")  # type: ignore[union-attr]
    if not url.startswith(("http://", "https://")):
        return LayerState(
            name="searxng",
            enabled=True,
            source=source,
            value=url,
            detail=f"{source}={url!r} is not an absolute http(s) URL",
            error=f"{source} must be an absolute http(s) URL, e.g. http://127.0.0.1:8888",
        )
    return LayerState(
        name="searxng",
        enabled=True,
        source=source,
        value=url,
        detail="a self-hosted SearXNG joins the Layer-1 engine fanout",
    )


def resolved_proxy_url(cli_value: str | None = None) -> str | None:
    """The effective proxy URL WITH credentials, or None. Never print this."""
    state = proxy_state(cli_value)
    if not state.enabled or state.error:
        return None
    return resolve_proxy(cli_value)


def layer_states(
    *,
    vpn: str | None = None,
    proxy: str | None = None,
    searxng_url: str | None = None,
) -> dict[str, LayerState]:
    """All three layers at once, in the order they sit in the egress path."""
    return {
        "vpn": vpn_state(vpn),
        "proxy": proxy_state(proxy),
        "searxng": searxng_state(searxng_url),
    }
