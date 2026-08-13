"""Optional egress proxy: WEBSEARCH_PROXY/--proxy resolution and per-path wiring.

Every network path is exercised at its composition surface with the transport faked,
so no test opens a socket: searxng (httpx.Client), ddgs (DDGS kwarg), the Layer 2A
pipeline default, arxiv/github (httpx.get), the CLI entry point, and the MCP _safe
error mapping.
"""

from __future__ import annotations

import httpx
import pytest

from websearch import cli
from websearch.layer1_search import SearchRequest
from websearch.layer1_search.adapters.ddgs_engine import DdgsAdapter
from websearch.layer1_search.adapters.searxng import SearxngAdapter
from websearch.layer2_extract import FetchRequest, build_pipeline
from websearch.layer2_extract.models import FetchResult
from websearch.layer2_extract.ports import FetchAdapter
from websearch.proxy import (
    EGRESS_LOCK_VAR,
    NORDVPN_DEFAULT_HOST,
    EgressLocked,
    ProxyConfigError,
    as_fetch_proxy,
    bypasses_proxy,
    egress_proxy,
    lock_enabled,
    proxy_for,
    proxy_type,
    resolve_proxy,
)

SOCKS = "socks5h://u:p@proxy.test:1080"
HTTP = "http://proxy.test:3128"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("WEBSEARCH_PROXY", "NORDVPN_USER", "NORDVPN_PASS", "NORDVPN_HOST"):
        monkeypatch.delenv(var, raising=False)


# --- resolution ---------------------------------------------------------------------


def test_unset_means_direct():
    assert resolve_proxy() is None


@pytest.mark.parametrize("value", ["", "off", "OFF", "none", "direct", "  off  "])
def test_off_values_mean_direct(monkeypatch, value):
    monkeypatch.setenv("WEBSEARCH_PROXY", value)
    assert resolve_proxy() is None


def test_env_url_passthrough(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", f"  {SOCKS}  ")
    assert resolve_proxy() == SOCKS


def test_cli_value_beats_env(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", HTTP)
    assert resolve_proxy(SOCKS) == SOCKS


# --- the credentials alone are the whole setup ---------------------------------------


def test_credentials_alone_turn_the_proxy_on(monkeypatch):
    """The install promise: put the two NordVPN variables in place and everything is
    proxied, with no third switch to remember."""
    monkeypatch.setenv("NORDVPN_USER", "svc_user")
    monkeypatch.setenv("NORDVPN_PASS", "svc_pass")
    assert resolve_proxy() == f"socks5h://svc_user:svc_pass@{NORDVPN_DEFAULT_HOST}:1080"
    assert lock_enabled() is True


def test_an_explicit_off_beats_the_credentials(monkeypatch):
    monkeypatch.setenv("NORDVPN_USER", "svc_user")
    monkeypatch.setenv("NORDVPN_PASS", "svc_pass")
    monkeypatch.setenv("WEBSEARCH_PROXY", "off")
    assert resolve_proxy() is None
    assert lock_enabled() is False


def test_one_credential_is_not_enough(monkeypatch):
    monkeypatch.setenv("NORDVPN_USER", "svc_user")
    assert resolve_proxy() is None


# --- a configured proxy is its own lock ----------------------------------------------


def test_a_configured_proxy_implies_the_lock(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", SOCKS)
    assert lock_enabled() is True


def test_no_proxy_means_no_lock():
    assert lock_enabled() is False


def test_a_broken_proxy_config_locks_rather_than_opening_direct(monkeypatch):
    """nordvpn with a credential missing must fail closed: the day the configuration
    breaks is exactly the day a silent direct fallback would leak the address."""
    monkeypatch.setenv("WEBSEARCH_PROXY", "nordvpn")
    assert lock_enabled() is True


def test_cli_off_overrides_the_implied_lock_but_not_an_explicit_one(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", SOCKS)
    assert egress_proxy("off") is None  # --proxy off: the human's explicit choice wins
    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    with pytest.raises(EgressLocked):
        egress_proxy("off")  # an explicit lock outranks the flag


# --- local targets skip the proxy ---------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8888",
        "http://localhost:8888/",
        "http://LOCALHOST:8888",
        "http://[::1]:8888",
        "http://192.168.1.20:8888",
        "http://10.0.0.5",
        "http://172.16.4.4:8080",
        "http://169.254.10.1",
        "http://searxng.local:8888",
        "http://box.home.arpa:8888",
    ],
)
def test_local_targets_bypass_the_proxy(url):
    assert bypasses_proxy(url) is True
    assert proxy_for(url, SOCKS) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://searx.be",
        "http://8.8.8.8",
        "https://searxng.example.com:8888",
        "http://mylocalhost.example.com",
    ],
)
def test_remote_targets_keep_the_proxy(url):
    assert bypasses_proxy(url) is False
    assert proxy_for(url, SOCKS) == SOCKS


def test_bypass_tolerates_a_missing_or_hostless_url():
    assert bypasses_proxy(None) is False
    assert bypasses_proxy("") is False
    assert bypasses_proxy("not a url") is False
    assert proxy_for(None, SOCKS) == SOCKS


def test_proxy_for_stays_none_when_no_proxy_is_configured():
    assert proxy_for("https://searx.be", None) is None


def test_searxng_on_loopback_is_not_proxied_even_when_one_is_set(httpx_mock, monkeypatch):
    """The bug this guards: WEBSEARCH_PROXY sent the loopback SearXNG hop to a remote
    SOCKS exit, which resolved localhost to itself, so every search failed."""
    seen: dict = {}
    real_client = httpx.Client

    def recording_client(*args, **kwargs):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("websearch.layer1_search.adapters.searxng.httpx.Client", recording_client)
    httpx_mock.add_response(json={"results": []})

    SearxngAdapter("http://127.0.0.1:8888", proxy=SOCKS).search(SearchRequest(query="q"))
    assert seen["proxy"] is None


def test_searxng_on_a_public_host_still_uses_the_proxy(httpx_mock, monkeypatch):
    seen: dict = {}
    real_client = httpx.Client

    def recording_client(*args, **kwargs):
        seen.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr("websearch.layer1_search.adapters.searxng.httpx.Client", recording_client)
    httpx_mock.add_response(json={"results": []})

    SearxngAdapter("https://searx.example.com", proxy=SOCKS).search(SearchRequest(query="q"))
    assert seen["proxy"] == SOCKS


def test_nordvpn_host_override_and_credential_encoding(monkeypatch):
    monkeypatch.setenv("NORDVPN_USER", "user@x")
    monkeypatch.setenv("NORDVPN_PASS", "p@ss:w/1")
    monkeypatch.setenv("NORDVPN_HOST", "amsterdam.nl.socks.nordhold.net")
    assert (
        resolve_proxy("nordvpn")
        == "socks5h://user%40x:p%40ss%3Aw%2F1@amsterdam.nl.socks.nordhold.net:1080"
    )


def test_nordvpn_without_credentials_raises(monkeypatch):
    monkeypatch.setenv("NORDVPN_USER", "svc-user")  # password still missing
    with pytest.raises(ProxyConfigError, match="NORDVPN_PASS"):
        resolve_proxy("nordvpn")


def test_fetch_proxy_shapes():
    assert proxy_type(SOCKS) == "socks5"
    assert proxy_type(HTTP) == "http"
    assert as_fetch_proxy(None) is None
    assert as_fetch_proxy(SOCKS) == {"url": SOCKS, "type": "socks5", "tor": False}


# --- layer 1: search engines --------------------------------------------------------


def test_ddgs_client_gets_proxy(monkeypatch):
    captured = {}

    class FakeDDGS:
        def __init__(self, proxy=None, timeout=None):
            captured["proxy"] = proxy

        def text(self, query, **kwargs):
            return []

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    DdgsAdapter(proxy=SOCKS).search(SearchRequest(query="q"))
    assert captured["proxy"] == SOCKS
    DdgsAdapter().search(SearchRequest(query="q"))
    assert captured["proxy"] is None


# --- layer 2A: fetch pipeline default -----------------------------------------------


class RecordingFetcher(FetchAdapter):
    name = "recorder"
    fetched_via = "http"
    tier_class = "http"
    escalation_order = -1  # ahead of the real HttpxFetcher; no socket is opened

    def __init__(self):
        self.last_request: FetchRequest | None = None

    def fetch(self, request: FetchRequest) -> FetchResult:
        self.last_request = request
        return FetchResult(
            url=request.url, fetched_via="http", status=200, ok=True, raw_html="<p>ok</p>"
        )


def test_pipeline_applies_default_proxy():
    rec = RecordingFetcher()
    pipeline = build_pipeline(enable_curl_cffi=False, extra_fetchers=[rec], proxy=HTTP)
    env = pipeline.run(FetchRequest(url="https://example.com/"))
    assert env.ok
    assert rec.last_request.proxy is not None
    assert rec.last_request.proxy.url == HTTP
    assert rec.last_request.proxy.type == "http"


def test_per_request_proxy_wins_over_default():
    rec = RecordingFetcher()
    pipeline = build_pipeline(enable_curl_cffi=False, extra_fetchers=[rec], proxy=HTTP)
    pipeline.run(FetchRequest(url="https://example.com/", proxy={"url": SOCKS, "type": "socks5"}))
    assert rec.last_request.proxy.url == SOCKS


def test_pipeline_without_proxy_leaves_requests_direct():
    rec = RecordingFetcher()
    pipeline = build_pipeline(enable_curl_cffi=False, extra_fetchers=[rec])
    pipeline.run(FetchRequest(url="https://example.com/"))
    assert rec.last_request.proxy is None


# --- keyless tools: arxiv and github ------------------------------------------------


_EMPTY_FEED = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'


class _Resp:
    def __init__(self, text):
        self.status_code = 200
        self.headers = {}
        self.text = text


class _FakeClientBase:
    """Stands in for httpx.Client: records constructor kwargs, serves a canned body."""

    body = ""
    sink: dict = {}

    def __init__(self, **kwargs):
        type(self).sink.update(kwargs)

    def get(self, url, *, params=None, headers=None, timeout=None):
        return _Resp(type(self).body)


def test_arxiv_default_transport_uses_proxy(monkeypatch):
    from websearch.tool_arxiv import ArxivSearchRequest, build_arxiv_tool

    captured: dict = {}

    class FakeClient(_FakeClientBase):
        body = _EMPTY_FEED
        sink = captured

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    env = build_arxiv_tool(proxy=SOCKS).search(ArxivSearchRequest(query="q"))
    assert env.ok
    assert captured["proxy"] == SOCKS


def test_github_default_transport_uses_proxy(monkeypatch):
    from websearch.tool_github import GithubSearchRequest, build_github_tool

    captured: dict = {}

    class FakeClient(_FakeClientBase):
        body = '{"total_count": 0, "incomplete_results": false, "items": []}'
        sink = captured

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeClient)
    build_github_tool(proxy=SOCKS).search(GithubSearchRequest(query="q"))
    assert captured["proxy"] == SOCKS


def test_injected_http_get_is_not_wrapped(monkeypatch):
    from websearch.tool_arxiv import ArxivSearchRequest, build_arxiv_tool

    calls = []

    def my_get(url, *, params, headers, timeout_s):
        calls.append(url)
        return _Resp(_EMPTY_FEED)

    env = build_arxiv_tool(http_get=my_get, proxy=SOCKS).search(ArxivSearchRequest(query="q"))
    assert env.ok and calls  # the injected transport ran untouched


# --- entry point: the CLI -----------------------------------------------------------


def test_cli_web_search_passes_env_proxy_to_builder(monkeypatch, capsys):
    seen = {}

    def fake_build_agent_io(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before any network")

    monkeypatch.setattr("websearch.cli.build_agent_io", fake_build_agent_io)
    monkeypatch.setenv("WEBSEARCH_PROXY", HTTP)
    rc = cli.main(["web-search", "q"])
    assert rc == 1
    assert seen["proxy"] == HTTP


def test_cli_proxy_off_overrides_env(monkeypatch):
    seen = {}

    def fake_build_agent_io(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop before any network")

    monkeypatch.setattr("websearch.cli.build_agent_io", fake_build_agent_io)
    monkeypatch.setenv("WEBSEARCH_PROXY", HTTP)
    cli.main(["web-search", "q", "--proxy", "off"])
    assert seen["proxy"] is None


def test_cli_nordvpn_without_creds_is_clean_invalid_request(capsys):
    rc = cli.main(["web-search", "q", "--proxy", "nordvpn"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid_request" in err
    assert "NORDVPN_USER" in err
    assert "Traceback" not in err
