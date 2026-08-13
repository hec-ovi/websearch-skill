"""NordVPN setup: the file the two credentials go in, and the city they come out of.

Turning the egress proxy on needs two things this tool cannot invent: the NordVPN service
credentials, and a decision about where the traffic should surface. Both are settings, so
this module is about one file and one variable each:

- ``NORDVPN_USER`` / ``NORDVPN_PASS`` go in the settings file. ``setup`` creates that file
  with both keys scaffolded and empty, then prints its absolute path. Nothing here ever
  reads a credential back out or prints one: filling them in is the user's own step, in
  their own editor, and the tool's job is to say exactly where.
- ``NORDVPN_HOST`` is the exit. ``use`` writes the host for a named city and turns the
  proxy on; ``locations`` lists what can be named. NordVPN runs SOCKS5 exits in three
  countries and nine US cities, which is the table in ``proxy.py``.

``status`` answers the whole thing in one screen: which file is in play, which keys are
still missing, which city is selected, and (unless verification is off) where NordVPN says
the traffic actually comes out, which is the only check that catches a selection that did
not take effect.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from . import errors
from .envelope import Envelope, layer_envelopes
from .optional_layers import (
    PROXY_ENV,
    defines_setting,
    env_file_candidates,
    redact_url,
    scrub_proxy,
    settings_file,
    write_env_setting,
)
from .proxy import (
    EGRESS_LOCK_VAR,
    NORDVPN_INSIGHTS,
    NORDVPN_LOCATIONS,
    ProxyConfigError,
    find_location,
    location_of_host,
    lock_enabled,
    lock_explicit,
    resolve_proxy,
    tor_on_or_off,
)

PROXY_CONTRACT_VERSION = "1.0.0"

USER_VAR = "NORDVPN_USER"
PASS_VAR = "NORDVPN_PASS"
HOST_VAR = "NORDVPN_HOST"
CREDENTIAL_VARS = (USER_VAR, PASS_VAR)

VERIFY_TIMEOUT_S = 15

# The scaffold `setup` writes. Two empty keys and the sentence that saves the one mistake
# everyone makes here: these are the service credentials from the dashboard, not the
# email and password the account logs in with.
SETTINGS_HEADER = """\
# websearch settings. Read by every websearch command. Keep it out of version control.
"""
CREDENTIAL_BLOCK = """
# NordVPN SOCKS5 egress. Fill both with your NordVPN SERVICE credentials:
# Nord Account -> NordVPN -> Manual setup -> Service credentials.
# They are NOT the email and password you log in with.
{lines}
"""


class ProxyRequest(BaseModel):
    """Mirrors contracts/proxy.schema.json#/$defs/ProxyRequest."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["setup", "status", "locations", "use", "off", "lock", "unlock"]
    location: str | None = None
    verify: bool = True
    timeout_s: int = Field(default=VERIFY_TIMEOUT_S, ge=1, le=120)


class ProxyLocation(BaseModel):
    """Mirrors contracts/proxy.schema.json#/$defs/ProxyLocation."""

    model_config = ConfigDict(extra="forbid")

    slug: str
    city: str | None = None
    country: str
    host: str
    selected: bool = False


class ProxyExit(BaseModel):
    """Mirrors contracts/proxy.schema.json#/$defs/ProxyExit."""

    model_config = ConfigDict(extra="forbid")

    ip: str | None = None
    city: str | None = None
    country: str | None = None
    protected: bool = False


class ProxyPayload(BaseModel):
    """Mirrors contracts/proxy.schema.json#/$defs/ProxyPayload."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["setup", "status", "locations", "use", "off", "lock", "unlock"]
    settings_file: str
    settings_exists: bool
    env_files: list[str] = []
    missing_keys: list[str] = []
    enabled: bool
    locked: bool = False
    proxy: str | None = None
    location: ProxyLocation | None = None
    locations: list[ProxyLocation] = []
    exit: ProxyExit | None = None
    exit_error: str | None = None
    wired: list[str] = []
    searxng: str | None = None
    next_actions: list[str] = []


def existing_env_files() -> list[Path]:
    """The settings files that exist right now, in the order they are read."""
    return [p for p in (c.expanduser() for c in env_file_candidates()) if p.is_file()]


def missing_credentials() -> list[str]:
    """Which of the two credential variables still has no value."""
    return [name for name in CREDENTIAL_VARS if not os.environ.get(name, "").strip()]


def _defined_anywhere(key: str) -> bool:
    return any(defines_setting(path, key) for path in existing_env_files())


def scaffold(target: Path) -> tuple[bool, list[str]]:
    """Make sure the settings file exists and names both credential keys.

    Returns whether the file had to be created and which keys were added. A key that
    already has a value, whether from a file in the chain or an exported variable, is left
    alone: writing an empty one underneath it would put a second, contradictory answer in
    front of the person reading the file.
    """
    created = not target.exists()
    added = [
        key
        for key in CREDENTIAL_VARS
        if not _defined_anywhere(key) and not os.environ.get(key, "").strip()
    ]
    if not created and not added:
        return False, []
    target.parent.mkdir(parents=True, exist_ok=True)
    text = "" if created else target.read_text(encoding="utf-8")
    if created:
        text = SETTINGS_HEADER
    elif not text.endswith("\n"):
        text += "\n"
    if added:
        text += CREDENTIAL_BLOCK.format(lines="\n".join(f"{key}=" for key in added))
    target.write_text(text, encoding="utf-8")
    target.chmod(0o600)  # it is about to hold a password
    return created, added


def verify_exit(proxy_url: str, timeout_s: int) -> tuple[ProxyExit | None, str | None]:
    """Ask NordVPN where this proxy comes out. Returns the exit, or why it could not.

    This is the only check that distinguishes "the setting is written" from "the traffic
    leaves from Dallas": a proxy URL is a claim until something on the other side of it
    answers.
    """
    try:
        import httpx

        with httpx.Client(proxy=proxy_url, timeout=timeout_s, follow_redirects=True) as client:
            info = client.get(NORDVPN_INSIGHTS).json()
    except Exception as exc:  # noqa: BLE001 - any transport failure is one answer here
        return None, scrub_proxy(f"{type(exc).__name__}: {exc}", proxy_url)
    return (
        ProxyExit(
            ip=info.get("ip"),
            city=info.get("city"),
            country=info.get("country"),
            protected=bool(info.get("protected")),
        ),
        None,
    )


def _locations(selected_host: str | None) -> list[ProxyLocation]:
    return [
        ProxyLocation(
            slug=loc.slug,
            city=loc.city,
            country=loc.country,
            host=loc.host,
            selected=loc.host == selected_host,
        )
        for loc in NORDVPN_LOCATIONS
    ]


def _selected() -> ProxyLocation | None:
    """The location the current ``NORDVPN_HOST`` names, listed or not."""
    host = os.environ.get(HOST_VAR, "").strip().lower()
    if not host:
        return None
    known = location_of_host(host)
    if known:
        return ProxyLocation(
            slug=known.slug,
            city=known.city,
            country=known.country,
            host=known.host,
            selected=True,
        )
    return ProxyLocation(slug="custom", city=None, country="unknown", host=host, selected=True)


def _searxng_gap() -> str | None:
    """Whether a running local SearXNG is still on a different egress than the one set.

    That instance fetches from every engine it queries, so an instance left behind on the
    old setting keeps searching from the previous exit while everything else has moved.
    Settings are read once at startup, which is why this is a restart and not a reload.
    """
    from . import searxng_local as sx

    try:
        paths = sx.Paths(sx.home_dir())
        if sx.running_pid(paths) is None:
            return None
        if sx.settings_egress(paths) == sx.outgoing_proxy():
            return None
    except (OSError, sx.SearxngError, ProxyConfigError):
        return None
    return "the running SearXNG still uses the previous egress; restart it: websearch searxng up"


def _lock_is_on() -> bool:
    """Whether egress is locked, treating a malformed switch as off (the layer reports it)."""
    try:
        return lock_enabled()
    except ProxyConfigError:
        return False


def _next_actions(payload: ProxyPayload) -> list[str]:
    actions: list[str] = []
    if payload.missing_keys:
        keys = " and ".join(payload.missing_keys)
        actions.append(f"fill {keys} in {payload.settings_file}")
    elif not payload.enabled:
        actions.append("pick where to come out: websearch proxy use dallas")
    if payload.enabled and not payload.locked:
        actions.append(
            "nothing stops a path that has no proxy from going direct; lock it with "
            "websearch proxy lock"
        )
    if payload.exit_error:
        actions.append(f"the proxy did not answer: {payload.exit_error}")
    elif payload.exit and not payload.exit.protected:
        actions.append(
            "the exit is not NordVPN: check the credentials and the host, then run "
            "websearch proxy status again"
        )
    if payload.enabled and tor_on_or_off():
        actions.append(
            "the tor layer is on, so this proxy is Tor's upstream: restart it with "
            "websearch tor up for a location change to take effect"
        )
    gap = _searxng_gap()
    if gap:
        actions.append(gap)
    return actions


def _state(action: str, request: ProxyRequest, **extra: Any) -> ProxyPayload:
    """The whole picture after an action: one shape for every one of them."""
    target = settings_file()
    try:
        proxy_url = resolve_proxy()
    except ProxyConfigError:
        proxy_url = None  # unusable configuration; missing_keys already says why
    payload = ProxyPayload(
        action=action,  # type: ignore[arg-type]
        settings_file=str(target),
        settings_exists=target.is_file(),
        env_files=[str(p) for p in existing_env_files()],
        missing_keys=missing_credentials(),
        enabled=proxy_url is not None,
        locked=_lock_is_on(),
        proxy=redact_url(proxy_url),
        location=_selected(),
        **extra,
    )
    if request.verify and proxy_url and action != "off":
        payload.exit, payload.exit_error = verify_exit(proxy_url, request.timeout_s)
    payload.next_actions = _next_actions(payload)
    return payload


_ok, _error = layer_envelopes(PROXY_CONTRACT_VERSION, layer="proxy", backend="nordvpn")


def _wire(key: str, value: str) -> list[str]:
    """Record a setting and make it true for this process too."""
    written = write_env_setting(key, value)
    os.environ[key] = value
    return [str(written)] if written else []


def _restart_searxng() -> str | None:
    """Move a running local SearXNG onto the new egress, since it only reads settings once.

    Everything that instance fetches leaves the machine, so an instance left on the old
    exit would keep searching from the city you just moved away from.
    """
    from . import searxng_local

    try:
        return searxng_local.refresh_if_running()
    except (OSError, searxng_local.SearxngError, ProxyConfigError):
        # Its own commands report their own failures; a proxy change that cannot restart
        # it is reported as an action left to take, not as a failure to select a city.
        return None


def control(request: ProxyRequest) -> Envelope:
    """Run one setup action and describe the resulting configuration.

    The single entry point behind the ``proxy`` CLI command. An unusable configuration
    comes back as a payload that names what is missing, not as an exception: "you have not
    filled the password yet" is an answer, not a crash.
    """
    started = time.monotonic()

    if request.action == "locations":
        payload = _state("locations", request, locations=_locations(os.environ.get(HOST_VAR)))
        return _ok(payload, started)

    if request.action == "status":
        return _ok(_state("status", request), started)

    if request.action == "setup":
        target = settings_file()
        try:
            created, added = scaffold(target)
        except OSError as exc:
            return _error(errors.INTERNAL_ERROR, f"cannot write {target}: {exc}")
        payload = _state("setup", request)
        if created:
            payload.next_actions.insert(0, f"created {target}")
        elif added:
            payload.next_actions.insert(0, f"added {', '.join(added)} to {target}")
        return _ok(payload, started)

    if request.action == "lock":
        try:
            configured = resolve_proxy() is not None
        except ProxyConfigError:
            configured = True  # a proxy is chosen, it just cannot be built yet
        if not configured:
            return _error(
                errors.INVALID_REQUEST,
                "there is no proxy to lock egress to. Run `websearch proxy use <city>` "
                "first; locking without one would refuse every command.",
            )
        wired = _wire(EGRESS_LOCK_VAR, "on")
        return _ok(_state("lock", request, wired=wired, searxng=_restart_searxng()), started)

    if request.action == "unlock":
        wired = _wire(EGRESS_LOCK_VAR, "off")
        return _ok(_state("unlock", request, wired=wired), started)

    if request.action == "off":
        if lock_explicit():
            # Turning the proxy off under an explicit lock is the one move that would put
            # traffic back on the real address, which is what the lock exists to stop. The
            # implied lock does not apply here: `proxy off` IS the explicit choice.
            return _error(
                errors.INVALID_REQUEST,
                f"{EGRESS_LOCK_VAR} is on: turning the proxy off would send traffic from "
                "your own address. Run `websearch proxy unlock` first if that is what you "
                "want.",
            )
        wired = _wire(PROXY_ENV, "off")
        return _ok(_state("off", request, wired=wired, searxng=_restart_searxng()), started)

    # --- use ------------------------------------------------------------------------
    if not request.location:
        return _error(errors.INVALID_REQUEST, "proxy use needs a location, e.g. 'dallas'")
    found = find_location(request.location)
    if found is None:
        listed = ", ".join(loc.slug for loc in NORDVPN_LOCATIONS)
        return _error(
            errors.INVALID_REQUEST,
            f"unknown location {request.location!r}. NordVPN runs SOCKS5 exits in: {listed}",
        )
    wired = _wire(HOST_VAR, found.host) + _wire(PROXY_ENV, "nordvpn")
    payload = _state("use", request, wired=sorted(set(wired)), searxng=_restart_searxng())
    return _ok(payload, started)
