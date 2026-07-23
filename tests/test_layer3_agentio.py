"""Layer 3 facade + MCP server: web_search / web_fetch / web_open.

Drives the real AgentIO with the engine boundary faked (ddgs) and the fetch boundary
stubbed (pytest-httpx). Validates the agent-io response contract, the fence, lossless
pagination, handle resolution (in-process and from a persisted store), and the FastMCP
tool registration + dispatch.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime

import pytest

from tests.conftest import (
    AGENTIO_FETCH_RESPONSE_REF,
    AGENTIO_SEARCH_RESPONSE_REF,
    ARTICLE_HTML,
    DDGS_ROWS,
    FakeDDGS,
    ddgs_factory,
)
from websearch.envelope import ok_envelope
from websearch.layer2_format import StoreConfig, fts5_available
from websearch.layer3_agentio import build_agent_io
from websearch.layer3_agentio.facade import AgentIO, make_handle
from websearch.layer3_agentio.models import (
    AgentFetchRequest,
    AgentOpenRequest,
    AgentSearchRequest,
)

FETCH_URL = "https://page.test/article"


@pytest.fixture
def agent():
    # ddgs faked for search; curl disabled so a fetch only ever hits the httpx_mock tier.
    return build_agent_io(
        enable_ddgs=True,
        ddgs_factory=lambda *a, **k: FakeDDGS(DDGS_ROWS),
        enable_curl_cffi=False,
    )


class _FakePipeline:
    """Returns a canned extract Envelope, so a redirect (final_url != url) is testable."""

    def __init__(self, envelope):
        self._env = envelope

    def run(self, request):
        return self._env


def _extract_env(*, requested, final, markdown="# Title\n\nbody text here and more", title="T"):
    return ok_envelope(
        "1.0.0",
        {
            "source": {
                "url": requested,
                "final_url": final,
                "status": 200,
                "ok": True,
                "fetched_via": "http",
            },
            "result": {"content_markdown": markdown, "title": title, "warnings": []},
            "warnings": [],
        },
        layer="extract",
    )


# --- web_search --------------------------------------------------------------------


def test_web_search_returns_ranked_handles_and_validates(agent, assert_valid):
    env = agent.web_search(AgentSearchRequest(query="rust", max_results=5))
    payload = env.model_dump(mode="json")
    assert_valid(payload, AGENTIO_SEARCH_RESPONSE_REF)
    assert env.ok and env.meta.layer == "agentio"
    results = payload["data"]["results"]
    assert payload["data"]["total_returned"] == len(results) >= 1
    assert all(h["handle"] for h in results)
    assert [h["rank"] for h in results] == list(range(1, len(results) + 1))


def test_concise_omits_engines_and_score_detailed_includes_them(agent):
    concise = agent.web_search(AgentSearchRequest(query="rust", detail="concise")).data
    detailed = agent.web_search(AgentSearchRequest(query="rust", detail="detailed")).data
    assert all(h["engines"] == [] and h["score"] is None for h in concise["results"])
    assert any(h["engines"] for h in detailed["results"])
    assert any(h["score"] is not None for h in detailed["results"])


def test_next_offset_is_null_honest(agent):
    # The keyless backends do not page reliably, so we do not advertise an offset cursor.
    env = agent.web_search(AgentSearchRequest(query="rust", max_results=2))
    assert env.data["next_offset"] is None


def test_web_search_zero_max_results_returns_every_fused_hit():
    rows = [{"title": f"T{i}", "href": f"https://example.com/p{i}", "body": "b"} for i in range(30)]
    agent = build_agent_io(
        enable_ddgs=True, ddgs_factory=ddgs_factory(rows), enable_curl_cffi=False
    )
    capped = agent.web_search(AgentSearchRequest(query="rust", max_results=5))
    uncapped = agent.web_search(AgentSearchRequest(query="rust", max_results=0))
    assert capped.data["total_returned"] == 5
    assert uncapped.data["total_returned"] == 30


def test_search_handle_equals_fetch_handle(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    fenv = agent.web_fetch(AgentFetchRequest(url=FETCH_URL))
    assert fenv.data["pages"][0]["handle"] == make_handle(FETCH_URL)


# --- web_fetch ---------------------------------------------------------------------


def test_web_fetch_fences_and_validates(agent, httpx_mock, assert_valid):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    env = agent.web_fetch(AgentFetchRequest(url=FETCH_URL, page_size_tokens=4000))
    payload = env.model_dump(mode="json")
    assert_valid(payload, AGENTIO_FETCH_RESPONSE_REF)
    assert env.ok
    page = payload["data"]["pages"][0]
    assert page["untrusted"] is True
    assert page["source"] == "live"
    assert page["fence"]["open"] in page["content"]
    assert page["fence"]["close"] in page["content"]
    assert "Ownership is the mechanism" in page["content"]  # full body, inside the fence


def test_web_fetch_paginates_a_large_body(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    env = agent.web_fetch(AgentFetchRequest(url=FETCH_URL, page_size_tokens=20))
    page = env.data["pages"][0]
    assert page["total_pages"] > 1
    assert page["has_more"] is True
    assert page["page"] == 1


def test_web_fetch_zero_page_size_returns_the_whole_body_as_one_page(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    env = agent.web_fetch(AgentFetchRequest(url=FETCH_URL, page_size_tokens=0))
    page = env.data["pages"][0]
    assert page["total_pages"] == 1
    assert page["has_more"] is False
    assert "Ownership is the mechanism" in page["content"]


def test_web_fetch_invalid_url_is_invalid_request_not_retriable(agent):
    # A caller error (non-http scheme) propagates as invalid_request, retriable false;
    # it must not collapse into a retriable fetch_failed.
    env = agent.web_fetch(AgentFetchRequest(url="ftp://nope"))
    assert not env.ok
    assert env.error.code == "invalid_request"
    assert env.error.retriable is False


def test_web_fetch_ssrf_refusal_is_permanent(agent):
    # An egress-refused address is a permanent failure: the code stays fetch_failed but
    # retriable must propagate as false from Layer 2, not be rewritten to true.
    env = agent.web_fetch(AgentFetchRequest(url="http://100.64.0.1/x"))
    assert not env.ok
    assert env.error.code == "fetch_failed"
    assert env.error.retriable is False


def test_web_fetch_transient_failure_stays_retriable(agent, httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"), url="https://down.test/x")
    env = agent.web_fetch(AgentFetchRequest(url="https://down.test/x"))
    assert not env.ok
    assert env.error.code == "fetch_failed"
    assert env.error.retriable is True


def test_fetch_many_propagates_shared_code_and_retriability(agent):
    env = agent.web_fetch_many(["ftp://a", "ftp://b"])
    assert not env.ok
    assert env.error.code == "invalid_request"
    assert env.error.retriable is False


def test_fetch_many_permanent_failures_not_retriable(agent):
    env = agent.web_fetch_many(["http://100.64.0.1/x"])
    assert not env.ok
    assert env.error.code == "fetch_failed"
    assert env.error.retriable is False


def test_fetch_many_zero_chars_per_token_is_clamped(agent, httpx_mock):
    # chars_per_token=0 used to divide by zero in the token estimate; it clamps to the
    # default instead.
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    env = agent.web_fetch_many([FETCH_URL], chars_per_token=0)
    assert env.ok
    assert env.data["pages"][0]["page_tokens"] > 0


def test_fetched_at_is_stamped_and_survives_web_open(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    fenv = agent.web_fetch(AgentFetchRequest(url=FETCH_URL))
    fetched_at = fenv.data["pages"][0]["fetched_at"]
    assert fetched_at is not None
    datetime.fromisoformat(fetched_at)  # parseable ISO 8601
    oenv = agent.web_open(AgentOpenRequest(handle=fenv.data["pages"][0]["handle"]))
    assert oenv.data["pages"][0]["fetched_at"] == fetched_at


def test_lazy_store_build_is_race_free(monkeypatch):
    import websearch.layer3_agentio.facade as fac

    aio = build_agent_io(enable_ddgs=False, enable_curl_cffi=False)
    real_build = fac.build_page_index
    calls: list[int] = []
    barrier = threading.Barrier(2)

    def slow_build(config):
        calls.append(1)
        import time

        time.sleep(0.05)  # widen the race window
        return real_build(config)

    monkeypatch.setattr(fac, "build_page_index", slow_build)
    stores: list[object] = []

    def grab():
        barrier.wait()
        stores.append(aio.store)

    threads = [threading.Thread(target=grab) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1  # built exactly once
    assert stores[0] is stores[1]


def test_partial_engine_match_warns_about_dropped_names(agent):
    env = agent.web_search(AgentSearchRequest(query="rust", engines=["ddgs", "goggle"]))
    assert env.ok
    assert env.data["results"]  # the valid subset still searched
    assert any("goggle" in w for w in env.data["warnings"])


# --- web_open ----------------------------------------------------------------------


def test_fetch_then_open_by_handle_uses_cache(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    fenv = agent.web_fetch(AgentFetchRequest(url=FETCH_URL, page_size_tokens=20))
    handle = fenv.data["pages"][0]["handle"]
    # Page 2 from cache: no second httpx response is registered, so this must not refetch.
    oenv = agent.web_open(AgentOpenRequest(handle=handle, page=2, page_size_tokens=20))
    assert oenv.ok
    page = oenv.data["pages"][0]
    assert page["source"] == "cache"
    assert page["page"] == 2


def test_open_with_zero_page_size_returns_the_whole_cached_body(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    fenv = agent.web_fetch(AgentFetchRequest(url=FETCH_URL, page_size_tokens=20))
    assert fenv.data["pages"][0]["total_pages"] > 1
    oenv = agent.web_open(AgentOpenRequest(handle=FETCH_URL, page_size_tokens=0))
    page = oenv.data["pages"][0]
    assert page["source"] == "cache"
    assert page["total_pages"] == 1
    assert page["has_more"] is False


def test_open_by_url_also_resolves(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    agent.web_fetch(AgentFetchRequest(url=FETCH_URL))
    oenv = agent.web_open(AgentOpenRequest(handle=FETCH_URL))
    assert oenv.ok and oenv.data["pages"][0]["source"] == "cache"


def test_open_by_handle_uses_the_index_fast_path(agent, httpx_mock, monkeypatch):
    # A handle fetched by this instance resolves from the handle index without rescanning
    # (and re-hashing) every stored URL; the full scan is only the persisted-store fallback.
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    handle = agent.web_fetch(AgentFetchRequest(url=FETCH_URL)).data["pages"][0]["handle"]

    def boom():
        raise AssertionError("resolve_index scanned despite an indexed handle")

    monkeypatch.setattr(agent.store, "resolve_index", boom)
    env = agent.web_open(AgentOpenRequest(handle=handle))
    assert env.ok and env.data["pages"][0]["source"] == "cache"


def test_open_unknown_handle_is_not_opened(agent):
    env = agent.web_open(AgentOpenRequest(handle="nope~deadbeef"))
    assert not env.ok
    assert env.error.code == "not_opened"
    assert env.error.retriable is False


def test_open_past_end_page_clamps_with_warning(agent, httpx_mock):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    agent.web_fetch(AgentFetchRequest(url=FETCH_URL))
    env = agent.web_open(AgentOpenRequest(handle=FETCH_URL, page=999))
    page = env.data["pages"][0]
    assert page["page"] == page["total_pages"]
    assert any("page 999 requested" in w for w in page["warnings"])


def test_handle_resolves_from_a_persisted_store_across_instances(httpx_mock, tmp_path):
    if not fts5_available():
        pytest.skip("FTS5 not available; cross-process resolution needs the sqlite-fts5 adapter")
    db = str(tmp_path / "idx.sqlite")
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    first = build_agent_io(
        enable_ddgs=False, enable_curl_cffi=False, store_config=StoreConfig(persist_path=db)
    )
    handle = first.web_fetch(AgentFetchRequest(url=FETCH_URL)).data["pages"][0]["handle"]
    second = build_agent_io(
        enable_ddgs=False, enable_curl_cffi=False, store_config=StoreConfig(persist_path=db)
    )
    env = second.web_open(AgentOpenRequest(handle=handle))
    assert env.ok and env.data["pages"][0]["source"] == "cache"


def test_redirect_keeps_handle_stable_to_requested_url():
    requested, final = "https://a.test/x", "https://a.test/x/"
    aio = AgentIO(
        router=None, pipeline=_FakePipeline(_extract_env(requested=requested, final=final))
    )
    page = aio.web_fetch(AgentFetchRequest(url=requested)).data["pages"][0]
    # The returned handle is stable to the REQUESTED url (what web_search keyed on), not
    # the post-redirect final_url, so a searched handle stays openable.
    assert page["handle"] == make_handle(requested)
    assert page["url"] == requested
    assert any("redirected to" in w for w in page["warnings"])
    # Openable by the search-time handle AND by the redirect target's handle.
    assert aio.web_open(AgentOpenRequest(handle=make_handle(requested))).ok
    assert aio.web_open(AgentOpenRequest(handle=make_handle(final))).ok


def test_resolve_fails_closed_on_a_handle_collision(monkeypatch, httpx_mock):
    import websearch.layer3_agentio.facade as fac

    # Force two distinct URLs onto the same handle and confirm web_open refuses to guess.
    monkeypatch.setattr(fac, "make_handle", lambda url: "same~collision")
    aio = build_agent_io(enable_ddgs=False, enable_curl_cffi=False)
    httpx_mock.add_response(url="https://a.test/1", html=ARTICLE_HTML)
    httpx_mock.add_response(url="https://a.test/2", html=ARTICLE_HTML)
    aio.web_fetch(AgentFetchRequest(url="https://a.test/1"))
    aio.web_fetch(AgentFetchRequest(url="https://a.test/2"))
    env = aio.web_open(AgentOpenRequest(handle="same~collision"))
    assert not env.ok and env.error.code == "not_opened"


def test_fetch_many_all_fail_preserves_the_cause(agent, httpx_mock):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"), url="https://dead.test/x")
    env = agent.web_fetch_many(["https://dead.test/x"])
    assert not env.ok and env.error.code == "fetch_failed"
    # The specific per-URL reason survives, not a generic "all 1 url(s) failed".
    assert "dead.test" in env.error.message


# --- MCP server --------------------------------------------------------------------


@pytest.fixture
def mcp_with_agent(agent):
    """Injects the faked AgentIO into the MCP module and restores the previous singleton,
    so no test leaks a module-global _AGENT into the rest of the session."""
    pytest.importorskip("fastmcp")
    from websearch.layer3_agentio import mcp_server

    prev = mcp_server._AGENT
    mcp_server.set_agent(agent)
    yield mcp_server
    mcp_server._AGENT = prev


def test_mcp_server_registers_and_dispatches(httpx_mock, mcp_with_agent):
    mcp_server = mcp_with_agent
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)

    async def check():
        for name in ("web_search", "web_fetch", "web_open"):
            tool = await mcp_server.mcp.get_tool(name)
            assert tool is not None and tool.name == name
        result = await mcp_server.mcp.call_tool("web_fetch", {"url": FETCH_URL})
        structured = getattr(result, "structured_content", None) or getattr(result, "data", None)
        assert structured and structured["ok"]
        assert structured["meta"]["layer"] == "agentio"
        assert structured["data"]["pages"][0]["untrusted"] is True

    asyncio.run(check())


def test_mcp_tool_invalid_argument_returns_error_envelope(mcp_with_agent):
    out = mcp_with_agent.web_search(query="x", detail="not-a-mode")
    assert out["ok"] is False
    assert out["error"]["code"] == "invalid_request"
