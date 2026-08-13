"""Optional egress proxy: one switch for every network path.

Two switches feed one answer. ``WEBSEARCH_PROXY`` names an egress proxy;
``WEBSEARCH_TOR`` says egress goes through the local Tor SOCKS port instead. When both
are on they are not in competition: ``egress_proxy`` hands out Tor's SOCKS address, and
the configured proxy is written into Tor's own torrc as its upstream (see
``tor_local.py``), so the path is you -> proxy -> Tor -> destination and neither hop is
silently dropped.

``WEBSEARCH_PROXY`` controls the proxy hop:

- ``nordvpn``: NordVPN SOCKS5, expanded to ``socks5h://USER:PASS@HOST:1080`` from
  ``NORDVPN_USER`` / ``NORDVPN_PASS`` (the service credentials shown in the Nord
  Account dashboard under "Set up NordVPN manually", not the account login) and
  optional ``NORDVPN_HOST`` (default ``nl.socks.nordhold.net``; any official
  ``*.socks.nordhold.net`` server works).
- empty, ``off``, ``none``, or ``direct``: direct connection.
- any other value: used verbatim as the proxy URL
  (``http://``, ``https://``, ``socks5://``, ``socks5h://``).
- unset: the NordVPN credentials decide. With ``NORDVPN_USER`` and ``NORDVPN_PASS``
  both present the proxy is on as if ``nordvpn`` had been written; without them the
  connection is direct. Credentials in the environment mean "use them": the setup is
  the two variables and nothing else, and only an explicit off word goes direct.

A configured proxy also locks egress: any path that cannot go through it fails rather
than leaving from the machine's own address (see ``lock_enabled``).

Which city the traffic comes out of is ``NORDVPN_HOST``, and ``NORDVPN_LOCATIONS`` is
the table of names you can put there: ``websearch proxy use dallas`` writes one, and a
``--location`` on a single command picks one for that command alone. See
``proxy_setup.py`` for the setup and selection commands.

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
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

NORDVPN_DEFAULT_HOST = "nl.socks.nordhold.net"
NORDVPN_PORT = 1080
NORDVPN_SUFFIX = ".socks.nordhold.net"
# NordVPN's own endpoint for "where does this connection come out": it answers with the
# exit IP, its city and country, and whether the address belongs to NordVPN. Used to
# confirm a selected location really is where the traffic leaves from.
NORDVPN_INSIGHTS = "https://api.nordvpn.com/v1/helpers/ips/insights"


@dataclass(frozen=True)
class NordvpnLocation:
    """One selectable NordVPN SOCKS5 exit."""

    slug: str
    city: str | None  # None for a country-wide pool, which lands in any of its cities
    country: str
    host: str

    @property
    def place(self) -> str:
        return f"{self.city}, {self.country}" if self.city else f"{self.country} (any city)"


def _nord_host(prefix: str) -> str:
    return f"{prefix}{NORDVPN_SUFFIX}"


# Every location NordVPN runs a SOCKS5 exit in. SOCKS5 is a small subset of their VPN
# network (three countries, nine cities), so this is the whole list rather than a
# selection from it, and the country rows are the pools the city rows belong to.
_NL, _SE, _US = "Netherlands", "Sweden", "United States"
NORDVPN_LOCATIONS: tuple[NordvpnLocation, ...] = (
    NordvpnLocation("netherlands", None, _NL, _nord_host("nl")),
    NordvpnLocation("amsterdam", "Amsterdam", _NL, _nord_host("amsterdam.nl")),
    NordvpnLocation("sweden", None, _SE, _nord_host("se")),
    NordvpnLocation("stockholm", "Stockholm", _SE, _nord_host("stockholm.se")),
    NordvpnLocation("united-states", None, _US, _nord_host("us")),
    NordvpnLocation("atlanta", "Atlanta", _US, _nord_host("atlanta.us")),
    NordvpnLocation("chicago", "Chicago", _US, _nord_host("chicago.us")),
    NordvpnLocation("dallas", "Dallas", _US, _nord_host("dallas.us")),
    NordvpnLocation("los-angeles", "Los Angeles", _US, _nord_host("los-angeles.us")),
    NordvpnLocation("new-york", "New York", _US, _nord_host("new-york.us")),
    NordvpnLocation("phoenix", "Phoenix", _US, _nord_host("phoenix.us")),
    NordvpnLocation("san-francisco", "San Francisco", _US, _nord_host("san-francisco.us")),
)

# The country pools answer to their code as well, because "us" is what people type.
_LOCATION_ALIASES = {"nl": "netherlands", "se": "sweden", "us": "united-states"}

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

EGRESS_LOCK_VAR = "WEBSEARCH_EGRESS_LOCK"
_LOCK_ON = {"on", "yes", "true", "1", "lock", "locked", "strict", "enabled"}

# socks5h keeps name resolution at the Tor client, which is not a preference: a .onion
# name has no answer in the local resolver, and asking it would leak the lookup.
TOR_SCHEMES = ("socks5h://", "socks5://")


class ProxyConfigError(ValueError):
    """A proxy was requested but its configuration is unusable."""


class EgressLocked(ProxyConfigError):
    """The lock is on and this path has no proxy to leave through, so it does not run."""


def lock_enabled(cli_value: str | None = None) -> bool:
    """Whether egress is locked to the proxy.

    Locked means one thing, and it is absolute: nothing this tool sends leaves the machine
    outside the proxy. A path that cannot use it fails instead of falling back to a direct
    connection, because a fallback is exactly the request that gives the address away, and
    it happens on the day the proxy is down rather than on the day you are watching.

    With ``WEBSEARCH_EGRESS_LOCK`` unset, a configured proxy is its own lock: turning the
    proxy on says "leave from there", so every path either uses it or fails. A proxy that
    is configured but unusable (nordvpn with a credential missing) locks too, because the
    alternative is opening direct egress on exactly the day the configuration broke. The
    explicit variable remains the override in both directions.

    Local targets (loopback, LAN) are not affected: they never leave the machine, and a
    remote exit could not reach them anyway. See ``bypasses_proxy``.
    """
    raw = cli_value if cli_value is not None else os.environ.get(EGRESS_LOCK_VAR)
    if raw is not None:
        value = raw.strip().lower()
        if value in OFF_WORDS:
            return False
        if value in _LOCK_ON:
            return True
        raise ProxyConfigError(
            f"{EGRESS_LOCK_VAR}={raw!r} is not a switch value; use 'on' or 'off'."
        )
    try:
        return resolve_proxy() is not None
    except ProxyConfigError:
        return True


def lock_explicit() -> bool:
    """Whether ``WEBSEARCH_EGRESS_LOCK`` itself says on, ignoring the implied lock.

    The two locks differ in one place: an explicit "go direct" (``--proxy off``, or
    ``websearch proxy off``) overrides the lock a configured proxy implies, and never
    this one.
    """
    raw = os.environ.get(EGRESS_LOCK_VAR)
    return raw is not None and raw.strip().lower() in _LOCK_ON


def find_location(name: str) -> NordvpnLocation | None:
    """The location a typed name means, or None when nothing matches.

    Accepts the slug, the city or country as written, a country code, and a full
    ``*.socks.nordhold.net`` hostname, because all four are things people have in hand.
    """
    key = " ".join(name.split()).strip().lower().replace(" ", "-")
    key = _LOCATION_ALIASES.get(key, key)
    for location in NORDVPN_LOCATIONS:
        names = {location.slug, location.country.lower().replace(" ", "-"), location.host}
        if location.city:
            names.add(location.city.lower().replace(" ", "-"))
        if key in names:
            return location
    return None


def location_of_host(host: str | None) -> NordvpnLocation | None:
    """The table row a configured ``NORDVPN_HOST`` names, or None for a host not in it."""
    if not host:
        return None
    return next((loc for loc in NORDVPN_LOCATIONS if loc.host == host.strip().lower()), None)


def nordvpn_host(location: str | None = None) -> str:
    """The NordVPN SOCKS host to dial: a named location, then ``NORDVPN_HOST``, then Amsterdam."""
    if location:
        found = find_location(location)
        if found:
            return found.host
        if location.strip().lower().endswith(NORDVPN_SUFFIX):
            return location.strip().lower()  # an official server this table does not list
        raise ProxyConfigError(
            f"unknown NordVPN location {location!r}. Run `websearch proxy locations` for "
            "the list; NordVPN runs SOCKS5 exits in the Netherlands, Sweden, and nine US "
            "cities only."
        )
    return os.environ.get("NORDVPN_HOST", NORDVPN_DEFAULT_HOST)


def _nordvpn_ready() -> bool:
    """Whether both NordVPN service credentials are in the environment."""
    return bool(os.environ.get("NORDVPN_USER") and os.environ.get("NORDVPN_PASS"))


def _nordvpn_url(location: str | None = None) -> str:
    user = os.environ.get("NORDVPN_USER", "")
    password = os.environ.get("NORDVPN_PASS", "")
    if not user or not password:
        raise ProxyConfigError(
            "proxy 'nordvpn' needs NORDVPN_USER and NORDVPN_PASS set to the NordVPN "
            "service credentials (Nord Account dashboard, 'Set up NordVPN manually'; "
            "these are not the account email/password). Run `websearch proxy setup` for "
            "the file to put them in."
        )
    host = nordvpn_host(location)
    return f"socks5h://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{NORDVPN_PORT}"


def resolve_proxy(cli_value: str | None = None, location: str | None = None) -> str | None:
    """The configured egress proxy URL, or None for a direct connection.

    This is the ``WEBSEARCH_PROXY`` hop on its own. It does not know about Tor: callers
    that want the address to actually connect through want ``egress_proxy``.

    ``location`` names the NordVPN city this call should come out of. Asking for a city
    is asking for NordVPN, so it turns the proxy on for the call even when the variable
    is off; what it cannot do is redirect some other proxy, which is an error rather than
    a silently ignored flag.
    """
    value = cli_value if cli_value is not None else os.environ.get("WEBSEARCH_PROXY")
    if location:
        if cli_value is not None and cli_value.strip().lower() != "nordvpn":
            raise ProxyConfigError(
                f"--location selects a NordVPN exit city and cannot be combined with "
                f"--proxy {cli_value!r}; drop one of the two."
            )
        return _nordvpn_url(location)
    if value is None:
        # Unset, not off: with both NordVPN service credentials in the environment the
        # proxy is on, because credentials someone placed there mean "use them" and the
        # off words remain the way to say otherwise.
        return _nordvpn_url() if _nordvpn_ready() else None
    if value.strip().lower() in _OFF:
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


def egress_proxy(
    cli_value: str | None = None,
    tor_value: str | None = None,
    location: str | None = None,
) -> str | None:
    """The address every network path should actually connect through, or None.

    Tor answers first when it is on. That is not the configured proxy being ignored: the
    proxy becomes Tor's upstream in torrc, so it is still in the path, one hop earlier.
    That is also why a per-call ``location`` is refused while Tor is up: the upstream is
    baked into the running Tor's torrc, so honoring the flag would take a restart and
    accepting it silently would name a city the traffic never visits.
    """
    if tor_enabled(tor_value):
        if location:
            raise ProxyConfigError(
                "--location cannot change the exit while the tor layer is on: the proxy is "
                "Tor's upstream, fixed when Tor started. Run `websearch proxy use "
                f"{location}` and then `websearch tor up` again."
            )
        return tor_socks_url()
    address = resolve_proxy(cli_value, location)
    if address is None and lock_enabled():
        # An explicit --proxy off overrides the lock a configured proxy implies; it never
        # overrides an explicit WEBSEARCH_EGRESS_LOCK=on.
        if cli_value is not None and cli_value.strip().lower() in _OFF and not lock_explicit():
            return None
        raise EgressLocked(
            f"egress is locked ({EGRESS_LOCK_VAR}) and this command has no proxy to leave "
            "through, so it would run from your own address. Set one up (`websearch proxy "
            "setup`, then `websearch proxy use <city>`), or unlock with `websearch proxy "
            "unlock`."
        )
    return address


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
