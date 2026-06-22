"""Router behavior: fan-out, fault tolerance, selection, site filtering, merge.

The router's real entry point is ``SearchRouter.search``; the external boundary is the
``EngineAdapter`` port, so these drive it with fake adapters (no network) and validate
the emitted Envelope against the frozen contract.
"""

from __future__ import annotations

import time

from tests.conftest import SEARCH_RESPONSE_REF
from websearch.layer1_search.capability import GENERAL_AGGREGATOR, NEURAL_INDEX
from websearch.layer1_search.models import Fusion, SearchRequest
from websearch.layer1_search.port import EngineAdapter, EngineOutput, RawResult
from websearch.layer1_search.router import SearchRouter


class FakeAdapter(EngineAdapter):
    def __init__(
        self,
        name: str,
        results: list[RawResult] | None = None,
        *,
        group: str = GENERAL_AGGREGATOR,
        error: str | None = None,
        enabled: bool = True,
        answers: list[str] | None = None,
    ):
        self.name = name
        self.correlation_group = group
        self._results = results or []
        self._error = error
        self._enabled = enabled
        self._answers = answers or []

    def enabled(self) -> bool:
        return self._enabled

    def search(self, request: SearchRequest) -> EngineOutput:
        if self._error:
            return EngineOutput(engine=self.name, error=self._error)
        return EngineOutput(engine=self.name, results=self._results, answers=self._answers)


def raw(url: str, rank: int, title: str = "t", snippet: str = "s") -> RawResult:
    return RawResult(url=url, title=title, snippet=snippet, rank=rank)


def test_all_engines_failed_is_an_error_envelope(assert_valid):
    router = SearchRouter([FakeAdapter("a", error="boom"), FakeAdapter("b", error="down")])
    env = router.search(SearchRequest(query="q"))
    payload = env.model_dump(mode="json")
    assert_valid(payload, SEARCH_RESPONSE_REF)
    assert env.ok is False
    assert payload["error"]["code"] == "all_engines_failed"
    assert payload["error"]["retriable"] is True
    assert payload["data"] is None
    assert payload["meta"]["layer"] == "search"


def test_no_engines_enabled(assert_valid):
    router = SearchRouter([FakeAdapter("a", enabled=False)])
    env = router.search(SearchRequest(query="q"))
    payload = env.model_dump(mode="json")
    assert_valid(payload, SEARCH_RESPONSE_REF)
    assert payload["error"]["code"] == "no_engines_enabled"
    assert payload["error"]["retriable"] is False


def test_unknown_requested_engine_yields_no_engines_enabled():
    router = SearchRouter([FakeAdapter("a", [raw("https://x/1", 1)])])
    env = router.search(SearchRequest(query="q", engines=["does-not-exist"]))
    assert env.ok is False
    assert env.error.code == "no_engines_enabled"


def test_partial_failure_records_unresponsive_and_keeps_results(assert_valid):
    router = SearchRouter(
        [FakeAdapter("a", error="timeout"), FakeAdapter("b", [raw("https://x.com/1", 1)])]
    )
    env = router.search(SearchRequest(query="q"))
    payload = env.model_dump(mode="json")
    assert_valid(payload, SEARCH_RESPONSE_REF)
    assert env.ok is True
    data = payload["data"]
    assert [u["url"] for u in data["results"]] == ["https://x.com/1"]
    assert data["engines_queried"] == ["a", "b"]
    assert {u["engine"]: u["reason"] for u in data["unresponsive_engines"]} == {"a": "timeout"}
    assert payload["meta"]["backend"] == "b"


def test_engine_selection_preserves_requested_order():
    router = SearchRouter(
        [FakeAdapter("a", [raw("https://a/1", 1)]), FakeAdapter("b", [raw("https://b/1", 1)])]
    )
    env = router.search(SearchRequest(query="q", engines=["b", "a"]))
    assert env.data["engines_queried"] == ["b", "a"]


def test_include_and_exclude_sites():
    results = [raw("https://example.com/1", 1), raw("https://other.com/2", 2)]
    router = SearchRouter([FakeAdapter("a", results)])

    inc = router.search(SearchRequest(query="q", include_sites=["example.com"]))
    assert [r["url"] for r in inc.data["results"]] == ["https://example.com/1"]

    exc = router.search(SearchRequest(query="q", exclude_sites=["other.com"]))
    assert [r["url"] for r in exc.data["results"]] == ["https://example.com/1"]


def test_dedup_merges_provenance_across_decorrelated_engines():
    a = FakeAdapter("a", [raw("https://u.com/", 1)], group=GENERAL_AGGREGATOR)
    b = FakeAdapter(
        "b",
        [raw("https://u.com", 1), raw("https://v.com/2", 2)],
        group=NEURAL_INDEX,
    )
    router = SearchRouter([a, b])
    env = router.search(SearchRequest(query="q"))
    results = {r["url"]: r for r in env.data["results"]}
    # https://u.com/ and https://u.com canonicalize to the same key and merge.
    assert set(results) == {"https://u.com/", "https://v.com/2"}
    merged = results["https://u.com/"]
    assert {s["engine"] for s in merged["sources"]} == {"a", "b"}
    # decorrelated agreement ranks the merged doc first
    assert env.data["results"][0]["url"] == "https://u.com/"


def test_correlated_engines_emit_a_decorrelation_warning():
    a = FakeAdapter("a", [raw("https://a/1", 1)], group=GENERAL_AGGREGATOR)
    b = FakeAdapter("b", [raw("https://b/1", 1)], group=GENERAL_AGGREGATOR)
    router = SearchRouter([a, b])
    env = router.search(SearchRequest(query="q"))
    assert any("correlation group" in w for w in env.data["warnings"])


def test_unknown_engine_in_partial_set_warns_with_available():
    router = SearchRouter([FakeAdapter("ddgs", [raw("https://x/1", 1)])])
    env = router.search(SearchRequest(query="q", engines=["ddgs", "nope"]))
    assert env.ok is True
    assert any("nope" in w and "ddgs" in w for w in env.data["warnings"])


def test_all_unknown_engines_error_names_the_available_set():
    router = SearchRouter([FakeAdapter("ddgs", [raw("https://x/1", 1)])])
    env = router.search(SearchRequest(query="q", engines=["nope"]))
    assert env.ok is False
    assert env.error.code == "no_engines_enabled"
    assert "ddgs" in env.error.message and "nope" in env.error.message


def test_score_convex_emits_fallback_warning():
    router = SearchRouter([FakeAdapter("a", [raw("https://x/1", 1)])])
    env = router.search(SearchRequest(query="q", fusion=Fusion(method="score_convex")))
    assert any("score_convex" in w for w in env.data["warnings"])


def test_max_total_results_caps_output():
    results = [raw(f"https://x/{i}", i) for i in range(1, 11)]
    router = SearchRouter([FakeAdapter("a", results)])
    env = router.search(SearchRequest(query="q", max_total_results=3))
    assert len(env.data["results"]) == 3


def test_responded_with_no_results_is_ok_and_empty():
    router = SearchRouter([FakeAdapter("a", [])])
    env = router.search(SearchRequest(query="q"))
    assert env.ok is True
    assert env.data["results"] == []


def test_slow_engine_times_out_without_blocking_the_request():
    class SlowAdapter(FakeAdapter):
        def search(self, request):
            time.sleep(0.4)
            return EngineOutput(engine=self.name, results=[raw("https://slow/1", 1)])

    router = SearchRouter([FakeAdapter("fast", [raw("https://fast/1", 1)]), SlowAdapter("slow")])
    router._timeout_grace_s = 0.05
    start = time.perf_counter()
    env = router.search(SearchRequest(query="q", timeout_ms=1))
    elapsed = time.perf_counter() - start
    assert elapsed < 0.35  # returned before the slow engine finished
    assert env.ok is True
    assert [r["url"] for r in env.data["results"]] == ["https://fast/1"]
    assert any(u["engine"] == "slow" for u in env.data["unresponsive_engines"])
