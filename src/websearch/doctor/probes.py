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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..optional_layers import LayerState, redact_url, scrub_proxy
from ..proxy import proxy_for
from .models import FAIL, OK, SKIPPED, WARN

# Keyless IP echoes, tried in order. Several because a doctor that reports "no internet"
# when one echo is down would be lying.
IP_ECHOES = (
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://icanhazip.com",
)

# NordVPN's keyless connection check. It reports whether the CALLER's egress is inside
# NordVPN's network, which is the only way to confirm the tunnel from outside it.
NORDVPN_INSIGHTS = "https://api.nordvpn.com/v1/helpers/ips/insights"

# Interface name prefixes that mean "traffic is tunneled". Covers OpenVPN (tun/tap),
# WireGuard (wg), and the provider-specific names NordVPN and Proton use.
TUNNEL_PREFIXES = ("tun", "tap", "wg", "nordlynx", "proton", "ipsec", "utun")

_NET_DIR = Path("/sys/class/net")

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
    ("fastmcp", "the MCP server"),
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
    detail = {
        "python": py,
        "package_version": pkg,
        "platform": platform.platform(terse=True),
        "executable": sys.executable,
    }
    # No version floor check: requires-python >=3.11 means an interpreter that got this
    # far already satisfies it.
    return Outcome(OK, f"Python {py}, websearch-skill {pkg}", detail)


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


def probe_mcp() -> Outcome:
    """Can the MCP face start, and does it register the tools it advertises?"""
    import asyncio

    try:
        from ..layer3_agentio import mcp_server
    except Exception as exc:
        return Outcome(
            FAIL,
            f"the MCP server does not import: {_err(exc)}",
            {},
            hint="fastmcp ships in the base install; reinstall with uv sync.",
        )
    try:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = sorted(t.name for t in tools)
    except Exception as exc:
        return Outcome(
            WARN,
            f"the MCP server imports but its tools could not be listed: {_err(exc)}",
            {},
            hint="Start it directly to see the real error: websearch mcp",
        )
    return Outcome(OK, f"{len(names)} tools: {', '.join(names)}", {"tools": names})


# --- egress -------------------------------------------------------------------------


def probe_internet(net: Net, *, timeout_s: float) -> Outcome:
    """The baseline: a direct request, no proxy. Everything else is measured against it."""
    ip, echo, errors = exit_ip(net, proxy=None, timeout_s=timeout_s)
    if not ip:
        return Outcome(
            FAIL,
            "no direct internet: every IP echo failed",
            {"errors": errors},
            hint="Check the machine's connectivity and DNS; every other network check "
            "below will fail for the same reason.",
        )
    return Outcome(OK, f"direct exit {ip} (via {echo})", {"exit_ip": ip, "echo": echo})


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
    detail: dict[str, Any] = {"proxy": shown, "direct_ip": direct_ip, "echo": echo}
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
    return Outcome(OK, f"exit {ip} through {shown}", detail)


def _tunnel_interfaces() -> list[str] | None:
    """Tunnel-looking network interfaces, or None where we cannot enumerate them."""
    if not _NET_DIR.is_dir():
        return None
    try:
        names = sorted(p.name for p in _NET_DIR.iterdir())
    except OSError:
        return None
    return [n for n in names if n.lower().startswith(TUNNEL_PREFIXES)]


def probe_vpn(
    state: LayerState,
    net: Net,
    *,
    timeout_s: float,
    proxy_url: str | None,
    proxy_enabled: bool,
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
            hint="Start it with ./docker/searxng/searxng.sh up, or unset "
            "WEBSEARCH_SEARXNG_URL to fall back to the keyless engines.",
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
        return Outcome(
            WARN,
            out.error,
            detail,
            hint="Keyless engines rate-limit and serve CAPTCHAs; one failing engine only "
            "narrows the fanout.",
        )
    if not out.results:
        return Outcome(WARN, "answered with 0 results", detail)
    return Outcome(OK, f"{len(out.results)} results", detail)


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


def probe_fetch(pipeline, url: str, *, timeout_ms: int, tier: str = "http") -> Outcome:
    """One fetch+extract round trip through the real Layer-2A pipeline."""
    from ..layer2_extract import FetchRequest

    try:
        env = pipeline.run(FetchRequest(url=url, tier_hint=tier, timeout_ms=timeout_ms))
    except Exception as exc:
        return Outcome(FAIL, _err(exc), {"url": url})
    if not env.ok:
        err = env.error
        return Outcome(
            FAIL,
            f"{err.code}: {err.message}" if err else "fetch failed",
            {"url": url},
            hint=f"Try it directly for the full error: websearch fetch {url}",
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
