"""The three optional layers, all off unless you turn them on.

Nothing here changes what the tool does by default. The base install searches with the
keyless engines over a direct connection, and each of these adds one thing on top:

- ``vpn``     (``WEBSEARCH_VPN``)          the host's traffic is expected to leave through
                                           a VPN tunnel. Declarative: the tunnel is the
                                           OS/app's job, and setting this tells ``websearch
                                           doctor`` to verify it rather than assume it.
- ``proxy``   (``WEBSEARCH_PROXY``)        every network path in the tool leaves through
                                           one egress proxy URL. See ``proxy.py``.
- ``tor``     (``WEBSEARCH_TOR``)          egress goes through a local Tor SOCKS port,
                                           which is also what makes ``.onion`` reachable
                                           and the onion engines selectable. When a proxy
                                           is configured too, Tor dials out through it
                                           rather than replacing it. See ``tor_local.py``.
- ``searxng`` (``WEBSEARCH_SEARXNG_URL``)  a self-hosted SearXNG joins the Layer-1 fanout.

Each is off when its variable is unset, empty, or one of the off words, so a fresh
install has all four off and no way to be surprised by one. The variables can come from
a gitignored ``.env`` (see ``load_env_file``) instead of your shell history.

Credentials live in these values (a proxy URL carries user:pass), so every display path
goes through ``redact_url``/``scrub``. The doctor prints layer state constantly; it must
never print a password.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .proxy import (
    OFF_WORDS,
    ProxyConfigError,
    resolve_proxy,
    tor_enabled,
    tor_socks_url,
)

VPN_ENV = "WEBSEARCH_VPN"
PROXY_ENV = "WEBSEARCH_PROXY"
SEARXNG_ENV = "WEBSEARCH_SEARXNG_URL"
TOR_ENV = "WEBSEARCH_TOR"


# WEBSEARCH_VPN values. `nordvpn` is verifiable (NordVPN publishes a keyless endpoint that
# reports whether the caller is behind their network); `any` only asserts that egress is
# tunneled somehow, which no third party can confirm for us.
VPN_NORDVPN = "nordvpn"
VPN_ANY = "any"
_VPN_ANY_ALIASES = {"any", "on", "yes", "true", "1", "vpn"}


ENV_FILE = ".env"
ENV_FILE_VAR = "WEBSEARCH_ENV_FILE"
CONFIG_DIR_NAME = "websearch"


def user_config_dir() -> Path:
    """This user's config directory for the tool: ``$XDG_CONFIG_HOME/websearch``."""
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base).expanduser() / CONFIG_DIR_NAME


def user_env_file() -> Path:
    """The one settings file that does not depend on where you run the command from."""
    return user_config_dir() / ENV_FILE


def env_file_candidates(path: str | None = None) -> list[Path]:
    """Every file ``load_env_file`` reads, most significant first.

    An explicit path (or ``WEBSEARCH_ENV_FILE``) is the whole chain when it is set: a
    caller that named a file means that file. Otherwise the working directory's ``.env``
    comes first, for a project that keeps its own, and the user config file last, which
    is the one place a setting survives changing directories.
    """
    explicit = path or os.environ.get(ENV_FILE_VAR)
    if explicit:
        return [Path(explicit).expanduser()]
    return [Path(ENV_FILE), user_env_file()]


def _load_one(target: Path) -> list[str]:
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    loaded: list[str] = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key in os.environ:  # an exported variable, or an earlier file, beats this one
            continue
        os.environ[key] = value
        loaded.append(key)
    return loaded


def load_env_file(path: str | None = None) -> list[str]:
    """Read the settings files into the environment and return the names they set.

    The credentials these layers need (NordVPN service credentials, a proxy URL) are the
    kind you do not want in shell history or a systemd unit, and both candidate files are
    outside the repo or gitignored. Precedence, highest first: an exported variable, then
    ``WEBSEARCH_ENV_FILE`` when set, then ``./.env``, then ``~/.config/websearch/.env``.
    Nothing already set is ever overwritten, so a file can only fill in a gap. Stdlib
    only: a 30-line parser is cheaper than a dependency.
    """
    loaded: list[str] = []
    for target in env_file_candidates(path):
        loaded.extend(_load_one(target))
    return loaded


def settings_file() -> Path:
    """The file settings are written to: ``WEBSEARCH_ENV_FILE``, else the user config file.

    The one path to print when telling someone where to put a credential. It is the write
    target whether or not it exists yet, which is what makes the answer stable.
    """
    return Path(os.environ.get(ENV_FILE_VAR) or user_env_file()).expanduser()


def defines_setting(path: Path, key: str) -> bool:
    """Whether ``path`` sets ``key``, however it spells the assignment."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=", re.MULTILINE)
    return bool(pattern.search(text))


def write_env_setting(key: str, value: str) -> Path | None:
    """Record ``key=value`` in the settings file, so the next command starts with it.

    A local process (the SearXNG bring-up, the Tor bring-up) knows a URL the next command
    has no way to guess, and a variable exported into this process dies with it. The
    target is ``WEBSEARCH_ENV_FILE`` when configured, else the user config file, which is
    this tool's own and safe to create. The working directory's ``.env`` is never written:
    it belongs to whatever project you happen to be standing in.

    Returns None when nothing was written, which happens when a
    higher-precedence file already sets the key: writing underneath it would produce a
    file that says one thing and an environment that does another.
    """
    target = settings_file()
    for candidate in env_file_candidates():
        if candidate.resolve() == target.resolve():
            break
        if defines_setting(candidate, key):
            return None
    target.parent.mkdir(parents=True, exist_ok=True)
    line = f"{key}={value}"
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
    kept = [ln for ln in existing if not pattern.match(ln)]
    kept.append(line)
    target.write_text("\n".join(kept) + "\n", encoding="utf-8")
    target.chmod(0o600)  # it holds proxy credentials often enough to assume it does
    return target


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
        if raw is None:
            # Unset is not off: the NordVPN credentials alone turn the proxy on
            # (see resolve_proxy), and the state must say so.
            auto = resolve_proxy()
            if auto:
                return LayerState(
                    name="proxy",
                    enabled=True,
                    source="NORDVPN_USER/NORDVPN_PASS",
                    value=redact_url(auto),
                    detail="on because the NordVPN credentials are set; every network "
                    f"path leaves through this proxy (set {PROXY_ENV}=off to go direct)",
                )
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


def tor_state(cli_value: str | None = None, *, proxy_cli: str | None = None) -> LayerState:
    """The Tor layer: off, or the local SOCKS port every path connects through.

    The detail line names the upstream proxy when there is one, because "on" alone would
    not tell you whether the VPN hop you configured survived. It did: Tor dials out
    through it. See ``tor_local.torrc_upstream``.
    """
    source = "--tor" if cli_value is not None else TOR_ENV
    try:
        enabled = tor_enabled(cli_value)
    except ProxyConfigError as exc:
        return LayerState(
            name="tor",
            enabled=True,
            source=source,
            value=None,
            detail=f"{source}={cli_value if cli_value is not None else os.environ.get(TOR_ENV)!r}"
            " is not a switch value",
            error=str(exc),
        )
    if not enabled:
        return LayerState(
            name="tor",
            enabled=False,
            source=None,
            value=None,
            detail=f"off (set {TOR_ENV}=on; `websearch tor up` starts one and sets it)",
        )
    try:
        socks = tor_socks_url()
    except ProxyConfigError as exc:
        return LayerState(
            name="tor",
            enabled=True,
            source=source,
            value=None,
            detail="the Tor SOCKS address is unusable",
            error=str(exc),
        )
    upstream = proxy_state(proxy_cli)
    chained = upstream.enabled and not upstream.error
    detail = "every network path leaves through Tor; .onion and the onion engines work"
    if chained:
        detail += f"; Tor dials out through {upstream.value}"
    return LayerState(name="tor", enabled=True, source=source, value=socks, detail=detail)


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
            detail=f"off (set {SEARXNG_ENV}; `websearch searxng up` starts one)",
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


def configured_proxy_url(cli_value: str | None = None) -> str | None:
    """The ``WEBSEARCH_PROXY`` hop WITH credentials, ignoring Tor. Never print this.

    Separate from ``resolved_proxy_url`` because two callers want different things: a
    request wants the address to connect through (Tor, when it is on), while the proxy
    check wants the proxy itself, which with Tor on is a real hop that still has to work.
    """
    state = proxy_state(cli_value)
    if not state.enabled or state.error:
        return None
    return resolve_proxy(cli_value)


def resolved_proxy_url(cli_value: str | None = None, tor_cli: str | None = None) -> str | None:
    """The effective egress address WITH credentials, or None. Never print this.

    Tor first when its layer is on, then the configured proxy. A proxy that is set but
    unusable resolves to None on its own; with Tor on it is Tor's problem instead, since
    that is the hop that has to dial through it.
    """
    tor = tor_state(tor_cli, proxy_cli=cli_value)
    if tor.enabled and not tor.error:
        return tor_socks_url()
    return configured_proxy_url(cli_value)


def layer_states(
    *,
    vpn: str | None = None,
    proxy: str | None = None,
    tor: str | None = None,
    searxng_url: str | None = None,
) -> dict[str, LayerState]:
    """All four layers at once, in the order they sit in the egress path."""
    return {
        "vpn": vpn_state(vpn),
        "proxy": proxy_state(proxy),
        "tor": tor_state(tor, proxy_cli=proxy),
        "searxng": searxng_state(searxng_url),
    }
