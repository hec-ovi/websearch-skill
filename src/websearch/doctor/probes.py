"""The individual diagnostics. One function per question, each answerable on its own.

Every probe returns an ``Outcome`` and never raises: a doctor that crashes on the first
broken thing cannot tell you what else is broken. Network access goes through the ``Net``
port so the whole set is testable without touching the internet.

Credential hygiene: anything derived from an exception goes through ``scrub_proxy`` before
it lands in a summary or a detail field. HTTP clients put the proxy URL, userinfo and all,
into their error text.
"""

from __future__ import annotations

import ipaddress
import platform
import socket
import sys
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..optional_layers import LayerState, redact_url, scrub_proxy
from ..proxy import EGRESS_LOCK_VAR, NORDVPN_INSIGHTS, lock_enabled, proxy_for
from .models import FAIL, OK, SKIPPED, WARN

# Keyless IP echoes, tried in order. Several because a doctor that reports "no internet"
# when one echo is down would be lying.
IP_ECHOES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

# Interface name prefixes that mean "traffic is tunneled". Covers OpenVPN (tun/tap),
# WireGuard (wg), macOS, which puts every tunnel on utun, and the provider-specific names
# NordVPN and Proton use.
TUNNEL_PREFIXES = ("tun", "tap", "wg", "nordlynx", "proton", "ipsec", "utun")

# The text engines ddgs can query, if the installed ddgs does not tell us. ddgs is the
# source of truth (it gains and loses engines between releases); this only keeps the
# doctor useful against an upstream layout change.
FALLBACK_DDGS_BACKENDS = (
    "brave",
    "duckduckgo",
    "google",
    "mojeek",
    "startpage",
    "wikipedia",
    "yahoo",
    "yandex",
)

# The base install's dependency closure, and what breaks without each one.
BASE_DEPENDENCIES = (
    ("httpx", "every HTTP path"),
    ("socksio", "socks5 proxy URLs"),
    ("ddgs", "the keyless search engines"),
    ("trafilatura", "HTML to Markdown extraction"),
    ("curl_cffi", "the browser-impersonation fetch tier"),
    ("pydantic", "every contract model"),
)


@dataclass
class Outcome:
    status: str
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)
    hint: str | None = None


class Net(Protocol):
    """The only network the probes get. Injectable, so the suite runs offline."""

    def text(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: float = 10.0,
        params: dict[str, Any] | None = None,
    ) -> str: ...

    def json(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: float = 10.0,
        params: dict[str, Any] | None = None,
    ) -> Any: ...


class HttpxNet:
    """The real ``Net``: one httpx client per proxy, reused across probes."""

    def __init__(self, user_agent: str = "websearch-skill-doctor/1.0"):
        self._user_agent = user_agent
        self._clients: dict[str | None, Any] = {}

    def _client(self, proxy: str | None):
        import httpx

        if proxy not in self._clients:
            self._clients[proxy] = httpx.Client(
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
                proxy=proxy,
            )
        return self._clients[proxy]

    def _get(self, url: str, proxy: str | None, timeout_s: float, params: dict | None):
        resp = self._client(proxy).get(url, timeout=timeout_s, params=params)
        resp.raise_for_status()
        return resp

    def text(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: float = 10.0,
        params: dict[str, Any] | None = None,
    ) -> str:
        return self._get(url, proxy, timeout_s, params).text

    def json(
        self,
        url: str,
        *,
        proxy: str | None = None,
        timeout_s: float = 10.0,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return self._get(url, proxy, timeout_s, params).json()

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()


def _err(exc: BaseException, proxy: str | None = None) -> str:
    """An exception rendered for a summary line, with any proxy credentials removed."""
    return scrub_proxy(f"{type(exc).__name__}: {exc}", proxy)


def _valid_ip(text: str) -> str | None:
    candidate = (text or "").strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def exit_ip(net: Net, *, proxy: str | None, timeout_s: float) -> tuple[str | None, str, list[str]]:
    """The public IP requests leave from: (ip, echo used, errors seen)."""
    errors: list[str] = []
    for echo in IP_ECHOES:
        try:
            ip = _valid_ip(net.text(echo, proxy=proxy, timeout_s=timeout_s))
        except Exception as exc:
            errors.append(f"{echo}: {_err(exc, proxy)}")
            continue
        if ip:
            return ip, echo, errors
        errors.append(f"{echo}: response was not an IP address")
    return None, "", errors


# --- runtime ------------------------------------------------------------------------


def probe_runtime() -> Outcome:
    from importlib.metadata import PackageNotFoundError, version

    try:
        pkg = version("websearch-skill")
    except PackageNotFoundError:
        pkg = "(not installed; running from a source tree)"
    py = platform.python_version()
    # Machine goes in the summary, not just the detail: when a dependency has no wheel it
    # is almost always because of the arch (arm64 vs amd64), and that is the first thing
    # you want to read next to the dependency check below.
    machine = platform.machine() or "unknown"
    detail = {
        "python": py,
        "package_version": pkg,
        "system": platform.system(),
        "machine": machine,
        "platform": platform.platform(terse=True),
        "executable": sys.executable,
    }
    # No version floor check: requires-python >=3.11 means an interpreter that got this
    # far already satisfies it.
    return Outcome(
        OK, f"Python {py} on {platform.system()} {machine}, websearch-skill {pkg}", detail
    )


def probe_dependencies() -> Outcome:
    from importlib.metadata import PackageNotFoundError, version

    found: dict[str, str] = {}
    missing: list[str] = []
    for name, what in BASE_DEPENDENCIES:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            missing.append(f"{name} ({what})")
    detail: dict[str, Any] = {"installed": found, "missing": missing}
    if missing:
        return Outcome(
            FAIL,
            f"{len(missing)} base dependency/dependencies missing: {', '.join(missing)}",
            detail,
            hint="Reinstall the package: uv sync, or pip install 'websearch-skill'.",
        )
    listed = ", ".join(f"{k} {v}" for k, v in found.items())
    return Outcome(OK, listed, detail)


# --- egress -------------------------------------------------------------------------


def probe_internet(net: Net, *, timeout_s: float, proxy: str | None = None) -> Outcome:
    """Can this installation reach the internet by the path it will actually use?

    With an egress proxy configured, that path IS the proxy, so this request goes through
    it. Probing directly would put a packet outside the tunnel purely to satisfy a
    diagnostic, which is the opposite of what turning the proxy on asked for.
    """
    ip, echo, errors = exit_ip(net, proxy=proxy, timeout_s=timeout_s)
    via = "through the egress proxy" if proxy else "direct"
    if not ip:
        return Outcome(
            FAIL,
            f"no internet {via}: every IP echo failed",
            {"errors": errors, "proxied": bool(proxy)},
            hint="Check connectivity and DNS; every other network check below will fail "
            "for the same reason."
            if not proxy
            else "Check the proxy first: with it unreachable, nothing else can work.",
        )
    return Outcome(
        OK,
        f"exit {ip} {via} (via {echo})",
        {"exit_ip": ip, "echo": echo, "proxied": bool(proxy)},
    )


def probe_baseline(net: Net, *, timeout_s: float, requested: bool) -> Outcome:
    """The direct exit IP, for comparing against the proxied one.

    Opt-in, because it is the only request in the whole tool that deliberately leaves the
    tunnel. Skipped by default when a proxy is configured: knowing your own ISP-assigned
    IP is not worth handing your real address to a third-party echo service while you are
    paying for a VPN to hide it.
    """
    if not requested:
        return Outcome(
            SKIPPED,
            "not measured: a direct request would leave the tunnel (--baseline to allow it)",
        )
    if lock_enabled():
        # The one probe that deliberately leaves the proxy, refused by the one setting that
        # says nothing may. Asking for it does not outrank the lock: that is what a lock is.
        return Outcome(
            SKIPPED,
            f"refused: {EGRESS_LOCK_VAR} is on, and this is the one request that would "
            "leave outside the proxy",
        )
    ip, echo, errors = exit_ip(net, proxy=None, timeout_s=timeout_s)
    if not ip:
        return Outcome(WARN, "no direct internet outside the proxy", {"errors": errors})
    return Outcome(OK, f"direct exit {ip} (via {echo})", {"exit_ip": ip, "echo": echo})


def _searxng_egress() -> str | None:
    """Where a local SearXNG's own engine requests leave from, redacted, or None.

    Reported next to the proxy check because it is a different process with its own
    setting: the CLI going through the proxy says nothing about where the instance that
    queries Google and Brave leaves from.
    """
    from .. import searxng_local

    try:
        paths = searxng_local.Paths(searxng_local.home_dir())
        if searxng_local.running_pid(paths) is None:
            return None
        return redact_url(searxng_local.settings_egress(paths))
    except (OSError, ValueError):
        return None


def probe_proxy(
    state: LayerState,
    proxy_url: str | None,
    net: Net,
    *,
    timeout_s: float,
    direct_ip: str | None,
) -> Outcome:
    """Does the egress-proxy layer actually move traffic somewhere else?"""
    if not state.enabled:
        return Outcome(SKIPPED, state.detail)
    if state.error:
        return Outcome(
            FAIL,
            state.error,
            {"source": state.source},
            hint="Fix the proxy configuration, or unset it to go direct.",
        )
    shown = state.value or redact_url(proxy_url) or "(unknown)"
    ip, echo, errors = exit_ip(net, proxy=proxy_url, timeout_s=timeout_s)
    detail: dict[str, Any] = {
        "proxy": shown,
        "direct_ip": direct_ip,
        "echo": echo,
        # Whether a path that cannot use the proxy is refused or quietly goes direct. The
        # difference only shows up on the day the proxy is unavailable.
        "locked": lock_enabled(),
        "searxng_egress": _searxng_egress(),
    }
    if not ip:
        return Outcome(
            FAIL,
            f"no traffic gets through {shown}",
            {**detail, "errors": errors},
            hint="Check the proxy host, port, and credentials. Prefer socks5h:// over "
            "socks5:// so DNS resolves proxy-side.",
        )
    detail["exit_ip"] = ip
    if direct_ip and ip == direct_ip:
        return Outcome(
            WARN,
            f"reachable but the exit IP is unchanged ({ip}); traffic is not being rerouted",
            detail,
            hint="A transparent or same-network proxy hides nothing. Confirm the proxy "
            "URL points at a remote exit.",
        )
    if direct_ip:
        return Outcome(OK, f"exit {ip} through {shown} (direct was {direct_ip})", detail)
    # No baseline was taken, on purpose. Say so rather than implying a comparison that
    # did not happen; for NordVPN the vpn check below is the stronger statement anyway.
    return Outcome(OK, f"exit {ip} through {shown} (not compared to a direct exit)", detail)


def probe_tor(state: LayerState, *, timeout_s: float, direct_ip: str | None) -> Outcome:
    """Is Tor actually carrying the traffic, or is something merely listening on the port?

    Reachability alone is not the answer worth reporting: any process can accept a
    connection on 9050, and a tool that said "tor: ok" for one would be telling you your
    requests are anonymous when they are not. The verdict comes from Tor's own check
    service, which is the same question SearXNG asks before it will use an onion engine.
    """
    if not state.enabled:
        return Outcome(SKIPPED, state.detail)
    if state.error:
        return Outcome(
            FAIL,
            state.error,
            {"source": state.source},
            hint=f"Fix {state.source}, or set WEBSEARCH_TOR=off to leave the layer alone.",
        )
    from ..tor_local import CHECK_URL, is_reachable, verify

    socks = state.value or "(unknown)"
    detail: dict[str, Any] = {"socks_url": socks, "direct_ip": direct_ip, "check": CHECK_URL}
    if not is_reachable(state.value):
        return Outcome(
            FAIL,
            f"nothing is listening on {socks}",
            detail,
            hint="Start it with `websearch tor up`. Until then .onion is unreachable and "
            "every request the layer was meant to route is going nowhere.",
        )
    result = verify(state.value, timeout_s=timeout_s)
    detail.update({k: v for k, v in result.items() if k != "error"})
    if result["is_tor"] is None:
        return Outcome(
            WARN,
            f"{socks} answers but the exit could not be confirmed: {result['error']}",
            detail,
            hint="Tor may still be bootstrapping, or the check service is unreachable from "
            "this exit. Re-run `websearch tor status`.",
        )
    if not result["is_tor"]:
        return Outcome(
            FAIL,
            f"{socks} answers but the traffic is NOT leaving through Tor",
            detail,
            hint="Something else is bound to that port. Stop it, or move Tor with "
            "WEBSEARCH_TOR_PORT.",
        )
    exit_ip = result.get("exit_ip") or "unknown"
    changed = f" (direct was {direct_ip})" if direct_ip and direct_ip != exit_ip else ""
    return Outcome(OK, f"Tor exit {exit_ip} through {socks}{changed}", detail)


def _tunnel_interfaces() -> list[str] | None:
    """Tunnel-looking network interfaces, or None where the names carry no signal.

    ``socket.if_nameindex`` is stdlib on Linux, macOS, and Windows (3.8+), so this needs
    no ``/sys/class/net`` walk and no platform branch beyond one: Windows reports names
    like ``ethernet_32770``, which say nothing about tunneling, so it returns None and
    the caller says "unconfirmed" rather than inventing a verdict from a name it cannot
    read. POSIX names are meaningful (Linux ``tun``/``tap``/``wg``/``nordlynx``, macOS
    puts every tunnel on ``utun``).
    """
    if sys.platform.startswith("win"):
        return None
    try:
        names = sorted(name for _, name in socket.if_nameindex())
    except (OSError, AttributeError):
        return None
    return [n for n in names if n.lower().startswith(TUNNEL_PREFIXES)]


def probe_vpn(
    state: LayerState,
    net: Net,
    *,
    timeout_s: float,
    proxy_url: str | None,
    proxy_enabled: bool,
    tor_enabled: bool = False,
) -> Outcome:
    """Is the tool's traffic really behind the VPN the operator declared?

    The path checked is the path the tool uses: through the egress proxy when one is
    configured, direct otherwise. Checking the host's own egress while the tool sends
    everything through a proxy would answer a question nobody asked.
    """
    if not state.enabled:
        return Outcome(SKIPPED, state.detail)
    if state.error:
        return Outcome(FAIL, state.error, {"source": state.source})
    if tor_enabled:
        # Everything now exits a Tor node, so every external check sees that node. A VPN
        # hop in front of Tor is real and still doing its job (your ISP sees the VPN, not
        # an entry guard), but nothing outside can confirm it, and reporting FAIL here
        # would be calling a working configuration broken.
        return Outcome(
            SKIPPED,
            "not checkable behind Tor: the tool's traffic exits a Tor node, so no external "
            "service can see the VPN hop in front of it",
            {"vpn": state.value, "tor": True},
        )

    path_proxy = proxy_url if proxy_enabled else None
    via = "the egress proxy" if path_proxy else "the host connection"

    if state.value == "nordvpn":
        try:
            info = net.json(NORDVPN_INSIGHTS, proxy=path_proxy, timeout_s=timeout_s)
        except Exception as exc:
            return Outcome(
                WARN,
                f"NordVPN's status endpoint is unreachable: {_err(exc, path_proxy)}",
                {"endpoint": NORDVPN_INSIGHTS, "path": via},
                hint="Without it the tunnel cannot be confirmed from outside. Retry, or "
                "check nordvpn's own client.",
            )
        detail = {
            "protected": bool(info.get("protected")),
            "exit_ip": info.get("ip"),
            "country": info.get("country"),
            "isp": info.get("isp"),
            "path": via,
        }
        if detail["protected"]:
            where = detail["country"] or "unknown location"
            return Outcome(
                OK, f"NordVPN protected via {via}: {detail['exit_ip']} ({where})", detail
            )
        return Outcome(
            FAIL,
            f"NOT behind NordVPN via {via}: {detail['exit_ip']} ({detail.get('isp') or '?'})",
            detail,
            hint="Connect the NordVPN app, or set WEBSEARCH_PROXY=nordvpn to send the "
            "tool's traffic through NordVPN's SOCKS5 exit.",
        )

    # WEBSEARCH_VPN=any: no third party can name the provider, so fall back to whether
    # the host has a tunnel interface at all.
    interfaces = _tunnel_interfaces()
    if interfaces is None:
        return Outcome(
            WARN,
            "cannot enumerate network interfaces on this platform; tunnel unconfirmed",
            {"path": via},
            hint="Use WEBSEARCH_VPN=nordvpn for a verifiable check, or verify the tunnel yourself.",
        )
    if interfaces:
        return Outcome(
            OK, f"tunnel interface(s) up: {', '.join(interfaces)}", {"interfaces": interfaces}
        )
    return Outcome(
        FAIL,
        "a VPN was declared but no tunnel interface is up",
        {"interfaces": []},
        hint="Start the VPN, or unset WEBSEARCH_VPN if you did not want one.",
    )


# --- searxng ------------------------------------------------------------------------


def probe_searxng(
    state: LayerState,
    net: Net,
    *,
    timeout_s: float,
    proxy_url: str | None,
    query: str,
) -> Outcome:
    """Health, engine counts, and one real JSON query against the configured instance."""
    if not state.enabled:
        return Outcome(SKIPPED, state.detail)
    if state.error:
        return Outcome(FAIL, state.error, {"source": state.source})

    base = (state.value or "").rstrip("/")
    # A loopback or LAN instance can never be reached through a remote exit node.
    proxy = proxy_for(base, proxy_url)
    detail: dict[str, Any] = {"url": base, "proxied": bool(proxy)}

    try:
        net.text(f"{base}/healthz", proxy=proxy, timeout_s=timeout_s)
    except Exception as exc:
        return Outcome(
            FAIL,
            f"not responding at {base}: {_err(exc, proxy)}",
            detail,
            hint="Start it with `websearch searxng up` (or ./docker/searxng/searxng.sh up on "
            "a Docker host), or unset WEBSEARCH_SEARXNG_URL to fall back to the "
            "keyless engines.",
        )

    try:
        config = net.json(f"{base}/config", proxy=proxy, timeout_s=timeout_s)
        engines = config.get("engines") or []
        detail["version"] = config.get("version")
        detail["engines_active"] = sum(1 for e in engines if e.get("enabled"))
        detail["engines_available"] = len(engines)
    except Exception as exc:
        detail["config_error"] = _err(exc, proxy)

    try:
        payload = net.json(
            f"{base}/search",
            proxy=proxy,
            timeout_s=timeout_s,
            params={"q": query, "format": "json"},
        )
    except Exception as exc:
        return _searxng_query_failed(exc, detail, proxy)

    results = payload.get("results") or []
    answered = sorted({e for r in results for e in (r.get("engines") or [])})
    detail["results"] = len(results)
    detail["engines_answered"] = answered
    if not results:
        return Outcome(
            WARN,
            f"reachable but returned 0 results for {query!r}",
            detail,
            hint="Re-probe the engine set: ./docker/searxng/searxng.sh engines",
        )
    counted = f"{len(results)} results from {len(answered)} engines" if answered else "results"
    version = f"SearXNG {detail['version']}, " if detail.get("version") else ""
    active = detail.get("engines_active")
    engines_note = f"{active} engines active, " if active else ""
    return Outcome(OK, f"{version}{engines_note}{counted}", detail)


def _searxng_query_failed(exc: Exception, detail: dict, proxy: str | None) -> Outcome:
    return Outcome(
        FAIL,
        f"healthy but the JSON search API failed: {_err(exc, proxy)}",
        detail,
        hint="SearXNG disables the JSON format by default. This repo's config enables it "
        "(search.formats: [html, json]); a hand-rolled instance needs it set.",
    )


# --- engines, tools, fetch ----------------------------------------------------------


def ddgs_backends() -> list[str]:
    """The text engines the installed ddgs can query, asked of ddgs itself."""
    try:
        from ddgs.engines import ENGINES

        names = sorted(ENGINES["text"])
    except Exception:
        return list(FALLBACK_DDGS_BACKENDS)
    return names or list(FALLBACK_DDGS_BACKENDS)


def probe_engine(adapter, request) -> Outcome:
    """One engine, through the real Layer-1 adapter, isolated from the others."""
    if not adapter.enabled():
        return Outcome(SKIPPED, "not configured")
    try:
        out = adapter.search(request)
    except Exception as exc:
        return Outcome(WARN, f"raised {_err(exc)}", {"engine": adapter.name})
    detail = {"engine": adapter.name, "results": len(out.results)}
    if out.error:
        # Deliberately not called a block. ddgs reports "No results found" for a CAPTCHA,
        # a rate limit, AND for a 200 whose HTML its parser no longer understands, and
        # from here those are indistinguishable. The SearXNG cross-check below is what
        # tells them apart, because it parses the same provider independently.
        return Outcome(WARN, out.error, detail)
    if not out.results:
        return Outcome(WARN, "answered with 0 results", detail)
    return Outcome(OK, f"{len(out.results)} results", detail)


def probe_searxng_engine(
    net: Net, base: str, engine: str, *, query: str, timeout_s: float, proxy: str | None
) -> tuple[bool, int, str | None]:
    """Ask a SearXNG instance for ONE named engine: (reached, result count, error).

    The second opinion on a silent provider. SearXNG's scraper for an engine is written
    and updated independently of ddgs's, so "ddgs got nothing, SearXNG got 200 results"
    is a parser problem, and "neither got anything" is the provider refusing this IP.
    """
    try:
        payload = net.json(
            f"{base.rstrip('/')}/search",
            proxy=proxy,
            timeout_s=timeout_s,
            params={"q": query, "format": "json", "engines": engine},
        )
    except Exception as exc:
        return False, 0, _err(exc, proxy)
    count = len(payload.get("results") or [])
    return count > 0, count, None


def probe_arxiv(tool, query: str) -> Outcome:
    from ..tool_arxiv import ArxivSearchRequest

    try:
        env = tool.search(ArxivSearchRequest(query=query, max_results=3))
    except Exception as exc:
        return Outcome(FAIL, _err(exc), {})
    if not env.ok:
        err = env.error
        status = WARN if err and err.code == "rate_limited" else FAIL
        return Outcome(status, f"{err.code}: {err.message}" if err else "failed", {})
    papers = (env.data or {}).get("papers") or []
    return Outcome(OK if papers else WARN, f"{len(papers)} papers", {"papers": len(papers)})


def probe_github(tool, query: str) -> Outcome:
    from ..tool_github import GithubSearchRequest

    try:
        env = tool.search(GithubSearchRequest(query=query, per_page=3))
    except Exception as exc:
        return Outcome(FAIL, _err(exc), {})
    if not env.ok:
        err = env.error
        if err and err.code == "rate_limited":
            return Outcome(
                WARN,
                f"rate limited: {err.message}",
                {},
                hint="Unauthenticated GitHub search allows about 10 requests a minute.",
            )
        return Outcome(FAIL, f"{err.code}: {err.message}" if err else "failed", {})
    repos = (env.data or {}).get("repos") or []
    return Outcome(OK if repos else WARN, f"{len(repos)} repos", {"repos": len(repos)})


def probe_fetch(
    pipeline, url: str, *, timeout_ms: int, tier: str = "http", what: str = "http"
) -> Outcome:
    """One fetch+extract round trip through the real Layer-2A pipeline."""
    from ..layer2_extract import FetchRequest

    try:
        env = pipeline.run(FetchRequest(url=url, tier_hint=tier, timeout_ms=timeout_ms))
    except Exception as exc:
        return Outcome(FAIL, _err(exc), {"url": url})
    if not env.ok:
        err = env.error
        # An onion failure is about the circuit, not the site: a healthy Tor reaches this
        # address, so pointing at the site would send the reader to the wrong place.
        hint = (
            "The Tor circuit could not reach this onion service. Check `websearch tor "
            "status`; a fresh Tor sometimes needs a second attempt."
            if what == "onion"
            else f"Try it directly for the full error: websearch fetch {url}"
        )
        return Outcome(
            FAIL,
            f"{err.code}: {err.message}" if err else "fetch failed",
            {"url": url},
            hint=hint,
        )
    data = env.data or {}
    src = data.get("source") or {}
    res = data.get("result") or {}
    detail = {
        "url": url,
        "status": src.get("status"),
        "via": src.get("fetched_via"),
        "words": res.get("word_count"),
        "blocked": bool(src.get("blocked")),
    }
    if src.get("blocked"):
        return Outcome(WARN, f"blocked: {src.get('block_reason')}", detail)
    return Outcome(
        OK,
        f"HTTP {src.get('status')} via {src.get('fetched_via')}, {res.get('word_count')} words",
        detail,
    )


def probe_impersonation(getter=None, *, impersonate: str = "chrome") -> Outcome:
    """Is the browser-impersonation tier (curl_cffi) present and usable?

    Availability only. Whether it defeats a given site's anti-bot is a property of that
    site, not of this installation, and a doctor should not pretend to measure it.
    """
    from ..layer2_extract.fetchers.curl_cffi_fetcher import CurlCffiFetcher

    fetcher = CurlCffiFetcher(getter=getter, impersonate=impersonate)
    if not fetcher.available():
        return Outcome(
            WARN,
            "curl_cffi is not importable, so a blocked page cannot escalate a tier",
            {"impersonate": impersonate},
            hint="curl_cffi ships in the base install; reinstall with uv sync.",
        )
    return Outcome(
        OK,
        f"curl_cffi available ({impersonate} impersonation)",
        {"impersonate": impersonate},
    )
