"""Onion search and onion fetch: what reaches .onion, and what must not.

The rule under test everywhere here is the same one: an onion address is only meaningful
behind Tor. Without the layer, a .onion fetch is refused before anything resolves (the
lookup would leak to the local resolver on its way to failing), and onion search reports
which command turns the layer on rather than returning an empty list.

The Ahmia parsing is exercised against markup captured from the live site, so a rewrite of
the adapter keeps answering the same shape.
"""

from __future__ import annotations

import httpx
import pytest

from websearch import cli
from websearch.layer1_search import SearchRequest, build_router
from websearch.layer1_search.adapters.ahmia import AhmiaAdapter, unwrap
from websearch.layer1_search.capability import ONION_INDEX
from websearch.layer2_extract.egress import BlockedEgress, guard_url, is_onion
from websearch.layer2_extract.models import FetchRequest
from websearch.proxy import TOR_ENV

ONION = "http://2gzyxa5ihm7nsggfxnu52rck2vv4rvmdlkiu3zzui5du4xyclen53wid.onion/"
TOR_SOCKS = "socks5h://127.0.0.1:9050"

# Trimmed from a real ahmia.fi result page: the redirect wrapper, the cite, and the
# lastSeen timestamp are all shapes the adapter reads.
AHMIA_HOME = """<html><body>
<form id="searchForm" class="autocomplete" action="/search/" method="get">
  <input id="id_q" type="search" name="q" title="q">
  <input type="hidden" name="76b36d" value="f91283">
</form></body></html>"""

AHMIA_RESULTS = """<html><body><ol class="searchResults">
  <li class="result">
    <h4><a href="/search/redirect?search_term=tor project&amp;redirect_url=http://deeprecyrsonacndoosu3udqp7ziofjddoiq6grsfizp3m3mvbiinpad.onion/link/the-tor-project">
      The Tor Project</a></h4>
    <p>Find the latest verified .onion link for The Tor Project.</p>
    <cite>deeprecyrsonacndoosu3udqp7ziofjddoiq6grsfizp3m3mvbiinpad.onion</cite>
    <span class="lastSeen" data-timestamp="July 4, 2026, 8:34 a.m.">3 weeks</span>
  </li>
  <li class="result">
    <h4><a href="/search/redirect?search_term=x&amp;redirect_url=http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/">
      Ahmia</a></h4>
    <p>Ahmia searches hidden services.</p>
    <cite>juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion</cite>
  </li>
  <li class="navigation"><a href="/search/?q=x&amp;page=2">next</a></li>
</ol></body></html>"""


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(TOR_ENV, raising=False)


# --- the egress guard -----------------------------------------------------------------


def test_an_onion_url_is_recognized():
    assert is_onion(ONION)
    assert is_onion("https://sub.example.onion/path")
    assert not is_onion("https://onion.example.com/")  # not the TLD


def test_an_onion_url_is_refused_without_the_tor_layer():
    with pytest.raises(BlockedEgress) as exc:
        guard_url(ONION, proxied=True)
    assert "tor layer is off" in exc.value.reason
    assert "websearch tor up" in exc.value.reason


def test_the_refusal_happens_before_anything_resolves():
    """Resolving an onion name locally is the leak, not just a failure."""

    def explode(host):
        raise AssertionError(f"the guard resolved {host}")

    with pytest.raises(BlockedEgress):
        guard_url(ONION, resolve=explode)


def test_allow_private_hosts_does_not_open_the_onion_path():
    with pytest.raises(BlockedEgress):
        guard_url(ONION, allow_private=True, proxied=True)


def test_an_onion_url_passes_with_a_tor_proxy():
    guard_url(ONION, proxied=True, tor=True)  # no raise


# --- the fetch path, end to end through the real pipeline -----------------------------


def test_fetching_an_onion_without_tor_fails_closed_with_the_fix(monkeypatch):
    from websearch.layer2_extract import build_pipeline

    env = build_pipeline().run(FetchRequest(url=ONION, tier_hint="http", timeout_ms=2000))

    assert not env.ok
    assert "tor layer is off" in env.error.message
    assert "websearch tor up" in env.error.message


def test_the_pipeline_marks_its_default_proxy_as_tor_when_the_layer_is_on(monkeypatch):
    """The flag has to reach Layer 2A, or a fetch behind Tor would still be refused."""
    monkeypatch.setenv(TOR_ENV, "on")
    args = cli.argparse.Namespace(proxy=None, tor=None)

    assert cli._egress_fetch_proxy(args) == {"url": TOR_SOCKS, "type": "socks5", "tor": True}


# --- the Ahmia adapter ----------------------------------------------------------------


def _ahmia(handler) -> AhmiaAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://ahmia.fi")
    return AhmiaAdapter(client=client, base_url_override="https://ahmia.fi")


def _routes(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/":
        return httpx.Response(200, text=AHMIA_HOME)
    if request.url.path == "/search/":
        assert request.url.params.get("76b36d") == "f91283", "the form field must be replayed"
        return httpx.Response(200, text=AHMIA_RESULTS)
    return httpx.Response(404)


def test_ahmia_unwraps_the_redirect_link():
    href = "/search/redirect?search_term=a+b&redirect_url=http://abc.onion/x?y=1"
    assert unwrap(href) == "http://abc.onion/x?y=1"
    assert unwrap("http://abc.onion/direct") == "http://abc.onion/direct"


def test_ahmia_parses_results_into_the_port_shape():
    out = _ahmia(_routes).search(SearchRequest(query="tor project", onion=True))

    assert out.error is None
    assert [r.url for r in out.results] == [
        "http://deeprecyrsonacndoosu3udqp7ziofjddoiq6grsfizp3m3mvbiinpad.onion/link/the-tor-project",
        "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/",
    ]
    first = out.results[0]
    assert first.title == "The Tor Project"
    assert "verified .onion link" in first.snippet
    assert first.published_date == "July 4, 2026, 8:34 a.m."
    assert first.rank == 1


def test_ahmia_maps_freshness_to_the_day_filter():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/":
            seen.update(dict(request.url.params))
            return httpx.Response(200, text=AHMIA_RESULTS)
        return httpx.Response(200, text=AHMIA_HOME)

    _ahmia(handler).search(SearchRequest(query="x", freshness="week", onion=True))
    assert seen["d"] == "7"


def test_ahmia_reports_an_engine_error_instead_of_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    out = _ahmia(handler).search(SearchRequest(query="x", onion=True))
    assert out.results == [] and out.error


def test_ahmia_is_disabled_without_a_tor_proxy():
    adapter = AhmiaAdapter(proxy=None)
    assert adapter.enabled() is False
    out = adapter.search(SearchRequest(query="x", onion=True))
    assert "websearch tor up" in out.error


# --- engine selection -----------------------------------------------------------------


def test_the_onion_router_holds_only_onion_engines():
    router = build_router(searxng_url="http://127.0.0.1:8888", proxy=TOR_SOCKS, onion=True)
    names = [a.name for a in router.adapters]

    assert names == ["ahmia", "searxng-onions"]
    assert all(a.correlation_group == ONION_INDEX for a in router.adapters)


def test_the_clearnet_router_has_no_onion_engines():
    router = build_router(searxng_url="http://127.0.0.1:8888")
    assert [a.name for a in router.adapters] == ["searxng", "ddgs"]


def test_a_clearnet_request_never_reaches_an_onion_engine():
    """Both sides can live in one router; the request picks which one runs."""
    router = build_router(searxng_url="http://127.0.0.1:8888", proxy=TOR_SOCKS, onion=True)

    env = router.search(SearchRequest(query="x", onion=False))

    assert not env.ok
    assert env.error.code == "no_engines_enabled"


def test_an_onion_request_with_the_layer_off_says_how_to_turn_it_on():
    router = build_router(onion=True)  # no proxy: Ahmia stays disabled

    env = router.search(SearchRequest(query="x", onion=True))

    assert not env.ok
    assert env.error.code == "no_engines_enabled"
    assert "websearch tor up" in env.error.message


def test_the_searxng_onion_adapter_asks_for_the_onions_category():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"results": []})

    from websearch.layer1_search.adapters.searxng import ONION_ENGINE_NAME, SearxngAdapter

    adapter = SearxngAdapter(
        "http://127.0.0.1:8888",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        category="onions",
        name=ONION_ENGINE_NAME,
    )
    adapter.search(SearchRequest(query="x", onion=True))

    assert seen["categories"] == "onions"
    assert adapter.name == "searxng-onions"


# --- the CLI face ---------------------------------------------------------------------


def test_the_cli_onion_flag_reaches_the_request_and_the_router(monkeypatch, capsys):
    captured: dict = {}

    class FakeRouter:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def search(self, request):
            captured["request"] = request
            from websearch.envelope import ok_envelope
            from websearch.layer1_search import SEARCH_CONTRACT_VERSION

            return ok_envelope(
                SEARCH_CONTRACT_VERSION,
                {
                    "query": request.query,
                    "request_id": "r",
                    "results": [],
                    "engines_queried": ["ahmia"],
                },
                layer="search",
                backend="ahmia",
            )

    monkeypatch.setattr(cli, "build_router", lambda **kwargs: FakeRouter(**kwargs))
    monkeypatch.setenv(TOR_ENV, "on")

    assert cli.main(["search", "hidden wiki", "--onion"]) == 0

    assert captured["onion"] is True
    assert captured["proxy"] == TOR_SOCKS  # onion search goes through Tor, not direct
    assert captured["request"].onion is True


def test_web_search_defaults_to_the_clearnet(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli, "build_agent_io", lambda **kwargs: captured.update(kwargs) or _Stub())

    cli.main(["web-search", "rust"])

    assert captured["onion"] is False
    assert captured["proxy"] is None


class _Stub:
    def web_search(self, req):
        from websearch.envelope import ok_envelope
        from websearch.layer3_agentio import AGENTIO_CONTRACT_VERSION

        return ok_envelope(
            AGENTIO_CONTRACT_VERSION,
            {"query": req.query, "results": [], "total_returned": 0},
            layer="agentio",
            backend="none",
        )


# --- keeping onion content out of the clearnet store ----------------------------------


def _agent_io(tmp_path, markdown="# Onion page\n\n" + "words " * 200):
    """A facade whose fetch boundary is canned, so this exercises storage, not network."""
    from websearch.envelope import ok_envelope
    from websearch.layer2_extract import EXTRACT_CONTRACT_VERSION
    from websearch.layer2_format import StoreConfig
    from websearch.layer3_agentio import build_agent_io

    class FakePipeline:
        def run(self, request, **kwargs):
            return ok_envelope(
                EXTRACT_CONTRACT_VERSION,
                {
                    "source": {"final_url": request.url, "status": 200, "fetched_via": "http"},
                    "result": {"title": "Onion page", "markdown": markdown, "word_count": 200},
                },
                layer="extract",
                backend="fake",
            )

    return build_agent_io(
        pipeline=FakePipeline(),
        router=object(),
        store_config=StoreConfig(persist_path=str(tmp_path / "pages.json")),
    )


def test_an_onion_page_is_indexed_in_its_own_file(tmp_path):
    from websearch.layer3_agentio import AgentFetchRequest

    aio = _agent_io(tmp_path)
    aio.web_fetch(AgentFetchRequest(url=ONION, page_size_tokens=50))
    aio.web_fetch(AgentFetchRequest(url="https://example.com/", page_size_tokens=50))

    onion_index = tmp_path / "pages-onion.json"
    assert onion_index.exists()
    clearnet = {e.url for e in aio.store.resolve_index().docs}
    onion = {e.url for e in aio.onion_store.resolve_index().docs}
    assert clearnet == {"https://example.com/"}
    assert onion == {ONION}


def test_an_onion_handle_still_opens_from_its_own_store(tmp_path):
    """Isolation must not cost the paging that makes a long onion page usable."""
    from websearch.layer3_agentio import AgentFetchRequest, AgentOpenRequest

    aio = _agent_io(tmp_path)
    fetched = aio.web_fetch(AgentFetchRequest(url=ONION, page_size_tokens=50))
    handle = fetched.data["pages"][0]["handle"]

    opened = aio.web_open(AgentOpenRequest(handle=handle, page=2, page_size_tokens=50))

    assert opened.ok, opened.error
    assert opened.data["pages"][0]["source"] == "cache"


def test_an_in_memory_run_writes_no_onion_file(tmp_path):
    """`--persist-path off` means off for both indexes, not off for one of them."""
    from websearch.envelope import ok_envelope
    from websearch.layer2_extract import EXTRACT_CONTRACT_VERSION
    from websearch.layer2_format import StoreConfig
    from websearch.layer3_agentio import AgentFetchRequest, build_agent_io

    class FakePipeline:
        def run(self, request, **kwargs):
            return ok_envelope(
                EXTRACT_CONTRACT_VERSION,
                {
                    "source": {"final_url": request.url, "status": 200, "fetched_via": "http"},
                    "result": {"title": "t", "markdown": "# t\n\nbody", "word_count": 2},
                },
                layer="extract",
                backend="fake",
            )

    aio = build_agent_io(pipeline=FakePipeline(), router=object(), store_config=StoreConfig())
    aio.web_fetch(AgentFetchRequest(url=ONION))

    assert list(tmp_path.iterdir()) == []


# --- the fence names an onion source --------------------------------------------------


def test_the_fence_marks_an_onion_page_as_one():
    from websearch.layer3_agentio import fence_untrusted

    text, _ = fence_untrusted("body", source_url=ONION)

    assert "ONION SERVICE" in text
    assert "accountable to nobody" in text
    assert "hostile by default" in text


def test_the_fence_does_not_say_that_about_a_clearnet_page():
    from websearch.layer3_agentio import fence_untrusted

    text, _ = fence_untrusted("body", source_url="https://example.com/")

    assert "ONION SERVICE" not in text
    assert "UNTRUSTED DATA" in text  # the ordinary fence is unchanged


def test_the_onion_note_reaches_a_fetched_page(tmp_path):
    from websearch.layer3_agentio import AgentFetchRequest

    aio = _agent_io(tmp_path)
    page = aio.web_fetch(AgentFetchRequest(url=ONION, page_size_tokens=50)).data["pages"][0]

    assert "ONION SERVICE" in page["content"]
