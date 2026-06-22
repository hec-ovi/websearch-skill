"""Regression tests for the robustness/edge-case polish pass.

Each test drives a real entry point (CLI, facade, tool client, store, fetcher, pipeline)
and asserts the gap is closed: no raw traceback, precise error codes/retriability, correct
clamping, thread safety, and the plug-and-play (no engine flags) agent surface.
"""

from __future__ import annotations

import json
import threading

import pytest

from tests.conftest import DDGS_ROWS, FakeDDGS, ddgs_factory
from websearch import errors
from websearch.cli import main
from websearch.layer2_extract.blocks import title_looks_like_error
from websearch.layer2_extract.egress import BlockedEgress, guard_url
from websearch.layer2_extract.models import FetchRequest, FetchResult
from websearch.layer2_extract.pipeline import FetchExtractPipeline
from websearch.layer2_format import StoreConfig
from websearch.layer2_format.chunk import chunk_markdown
from websearch.layer2_format.models import PageInput, ResultInput, SearchPageRequest
from websearch.layer2_format.store import build_page_index
from websearch.layer3_agentio import AgentSearchRequest, build_agent_io


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# --- facade-1 / cli-1: page <= 0 must never crash with a traceback -------------------


def test_web_fetch_page_zero_is_clean_invalid_request(monkeypatch, capsys):
    monkeypatch.setattr("ddgs.DDGS", lambda *a, **k: FakeDDGS([]))
    rc = main(["web-fetch", "https://example.com", "--page", "0", "--json"])
    out = _json(capsys)
    assert rc == 1
    assert out["ok"] is False
    assert out["error"]["code"] == errors.INVALID_REQUEST  # not a raw traceback


def test_web_fetch_negative_page_is_clean_invalid_request(capsys):
    rc = main(["web-fetch", "https://example.com", "--page", "-3", "--json"])
    out = _json(capsys)
    assert rc == 1
    assert out["error"]["code"] == errors.INVALID_REQUEST


def test_build_page_clamps_page_below_one_without_crashing():
    # web_fetch_many takes a raw page kwarg (no request model). page<=0 must clamp to the
    # first page and add a warning, never index pages[-1] or raise AgentPage's ge=1.
    aio = build_agent_io(enable_ddgs=False)
    page = aio._build_page(
        url="https://x.test/p",
        title="t",
        markdown="# A\nalpha\n\n# B\nbeta\n",
        page=0,
        page_size_tokens=4000,
        datamark=False,
        chars_per_token=4.0,
        source="live",
    )
    assert page.page == 1
    assert any("below 1" in w for w in page.warnings)


# --- F10: a whitespace-only web-search query is rejected ------------------------------


def test_web_search_whitespace_query_rejected(capsys):
    rc = main(["web-search", "   ", "--json"])
    out = _json(capsys)
    assert rc == 1
    assert out["error"]["code"] == errors.INVALID_REQUEST


# --- F5: engines is forgiving (never hard-fails) -------------------------------------


def test_web_search_unknown_engine_falls_back_to_default_with_warning(monkeypatch):
    # The model field still exists; passing a non-adapter name must run the default search
    # and warn, never return no_engines_enabled (which would yield zero results).
    aio = build_agent_io(ddgs_factory=ddgs_factory(DDGS_ROWS))
    env = aio.web_search(AgentSearchRequest(query="rust", engines=["google", "brave"]))
    assert env.ok
    assert env.data["results"], "fell back to default engines instead of failing"
    assert any("not available" in w for w in env.data["warnings"])


# --- SEED-1: facade envelopes carry real elapsed_ms ----------------------------------


def test_web_search_envelope_has_real_elapsed_ms():
    aio = build_agent_io(ddgs_factory=ddgs_factory(DDGS_ROWS))
    env = aio.web_search(AgentSearchRequest(query="rust"))
    assert env.ok
    assert env.meta.elapsed_ms > 0.0  # was hard-coded 0.0 before the fix


def test_propagate_error_preserves_trace_id_and_valid_code():
    # A search whose router fails must keep the upstream trace_id and use a real error code.
    aio = build_agent_io(enable_ddgs=False)  # no engines configured
    env = aio.web_search(AgentSearchRequest(query="rust"))
    assert env.ok is False
    assert env.error.code in {errors.NO_ENGINES_ENABLED, errors.ALL_ENGINES_FAILED}
    assert env.error.code != "unknown_error"
    assert env.meta.trace_id is not None  # upstream correlation id preserved


# --- cli backstop --------------------------------------------------------------------


def test_cli_backstop_converts_unexpected_exception_to_clean_error(monkeypatch, capsys):
    from websearch import cli as cli_mod

    def boom(args):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli_mod, "_cmd_github", boom)
    rc = main(["github", "x", "--json"])
    out = _json(capsys)
    assert rc == 1
    assert out["error"]["code"] == errors.INTERNAL_ERROR
    assert "kaboom" in out["error"]["message"]  # cause preserved, not a traceback


# --- layer2-fetch-2: precise retriability from structured failure_kind ---------------


class _StubRouter:
    def __init__(self, result: FetchResult):
        self._result = result

    def fetch(self, request: FetchRequest) -> FetchResult:
        return self._result


def _retriable_for(kind: str) -> bool:
    fr = FetchResult(
        url="https://x.test/",
        status=0,
        ok=False,
        fetched_via="http",
        error="boom",
        failure_kind=kind,  # type: ignore[arg-type]
    )
    pipe = FetchExtractPipeline(_StubRouter(fr), extractor=None)  # extractor unused on status 0
    env = pipe.run(FetchRequest(url="https://x.test/"))
    assert env.ok is False
    return env.error.retriable


def test_permanent_fetch_failures_are_not_retriable():
    assert _retriable_for("egress_refused") is False
    assert _retriable_for("redirect_loop") is False
    assert _retriable_for("dependency_missing") is False


def test_transient_fetch_failures_are_retriable():
    assert _retriable_for("transport_error") is True
    assert _retriable_for("timeout") is True


# --- layer2-fetch-4: SSRF guard closes CGNAT (100.64.0.0/10) --------------------------


def test_egress_guard_blocks_cgnat_literal():
    with pytest.raises(BlockedEgress):
        guard_url("http://100.64.0.1/")


def test_egress_guard_blocks_host_resolving_to_cgnat():
    with pytest.raises(BlockedEgress):
        guard_url("http://nat.test/", resolve=lambda host: {"100.100.0.5"})


def test_egress_guard_allows_public():
    guard_url("http://ok.test/", resolve=lambda host: {"93.184.216.34"})  # no raise


# --- layer2-extract-2: error-title veto is tightened ---------------------------------


def test_title_error_detector_ignores_legit_long_titles():
    assert title_looks_like_error("Forbidden City: a complete travel guide") is False
    assert title_looks_like_error("The Top 500 Companies of 2026") is False


def test_title_error_detector_still_flags_real_errors():
    assert title_looks_like_error("404 Not Found") is True
    assert title_looks_like_error("Just a moment...") is True
    assert title_looks_like_error("Access Denied") is True


# --- layer2-format-2 / -4: NaN score + chunk hang ------------------------------------


def test_nan_score_sanitized_to_none_at_model_boundary():
    r = ResultInput(url="https://x.test/", score=float("nan"))
    assert r.score is None
    r2 = ResultInput(url="https://x.test/", score=float("inf"), quality_score=float("-inf"))
    assert r2.score is None and r2.quality_score is None


def test_chunk_markdown_zero_max_chars_terminates():
    # max_chars=0 used to hang the heading path in an infinite loop.
    out = chunk_markdown("# Heading\nsome body text here\n", max_chars=0)
    assert isinstance(out, list)  # returns, does not hang


# --- layer2-store: thread-safety, transaction, honest total --------------------------


def _docs(n: int) -> list[PageInput]:
    return [
        PageInput(url=f"https://x.test/{i}", markdown=f"# Doc {i}\nalpha shared term body {i}\n")
        for i in range(n)
    ]


def test_store_concurrent_add_and_search_is_safe():
    store = build_page_index(StoreConfig())
    errors_seen: list[Exception] = []

    def worker(i: int) -> None:
        try:
            store.add(_docs(3))
            store.search(SearchPageRequest(query="alpha", top_k=10))
            store.resolve_index()
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors_seen, f"store raced under concurrency: {errors_seen}"


def test_store_add_leaves_no_open_transaction():
    store = build_page_index(StoreConfig())
    store.add(_docs(2))
    con = getattr(store, "_con", None)
    if con is not None:  # sqlite backend only
        assert con.in_transaction is False  # the per-doc transaction committed cleanly


def test_store_total_reflects_true_match_count_beyond_top_k():
    store = build_page_index(StoreConfig())
    store.add(_docs(6))  # 6 docs, each a passage containing "alpha"
    res = store.search(SearchPageRequest(query="alpha", top_k=2, page=1, page_size=2))
    assert len(res.passages) == 2  # only the top_k pool is returned
    assert res.total >= 6  # but total is the honest match count, not the capped pool


# --- mcp-2: a tool whose body raises returns a clean internal_error -------------------


def test_mcp_tool_internal_error_is_clean(monkeypatch):
    pytest.importorskip("fastmcp")
    from websearch.layer3_agentio import mcp_server

    class _Boom:
        def web_open(self, req):
            raise RuntimeError("store exploded")

    monkeypatch.setattr(mcp_server, "_AGENT", _Boom())
    out = mcp_server.web_open(handle="x.test~deadbeefdead", page=1)
    assert out["ok"] is False
    assert out["error"]["code"] == errors.INTERNAL_ERROR
    assert "store exploded" in out["error"]["message"]
