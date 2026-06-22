"""Router behavior: fan-out, fault tolerance, selection, site filtering, merge.

The router's real entry point is ``SearchRouter.search``; the external boundary is the
``EngineAdapter`` port, so these drive it with fake adapters (no network) and validate
the emitted Envelope against the frozen contract.
"""

from __future__ import annotations

from tests.conftest import SEARCH_RESPONSE_REF
from websearch.layer1_search.capability import GENERAL_AGGREGATOR, NEURAL_INDEX
from websearch.layer1_search.models import SearchRequest
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
