"""Adapter fidelity: request-param mapping, field coercion, and robustness.

SearXNG is exercised over real httpx (mocked at the network layer, so the assertions
inspect the actual outgoing request); ddgs is exercised with a recording fake that
captures the kwargs the adapter passes to the library.
"""

from __future__ import annotations

import pytest

from websearch.layer1_search.adapters.ddgs_engine import DdgsAdapter
from websearch.layer1_search.adapters.searxng import SearxngAdapter
from websearch.layer1_search.models import SearchRequest

SEARXNG_URL = "http://searxng.test"


def _sent_params(httpx_mock):
    return dict(httpx_mock.get_requests()[0].url.params)


@pytest.mark.parametrize("level,expected", [("off", "0"), ("moderate", "1"), ("strict", "2")])
def test_searxng_safesearch_mapping(httpx_mock, level, expected):
    httpx_mock.add_response(json={"results": []})
    SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q", safesearch=level))
    params = _sent_params(httpx_mock)
    assert params["safesearch"] == expected
    assert params["format"] == "json"


@pytest.mark.parametrize("fresh", ["day", "week", "month", "year"])
def test_searxng_freshness_maps_to_time_range(httpx_mock, fresh):
    httpx_mock.add_response(json={"results": []})
    SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q", freshness=fresh))
    assert _sent_params(httpx_mock)["time_range"] == fresh


def test_searxng_news_and_language_params(httpx_mock):
    httpx_mock.add_response(json={"results": []})
    SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q", result_type="news", language="en"))
    params = _sent_params(httpx_mock)
    assert params["categories"] == "news"
    assert params["language"] == "en"


def test_searxng_pageno_from_offset(httpx_mock):
    httpx_mock.add_response(json={"results": []})
    SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q", count=10, offset=20))
    assert _sent_params(httpx_mock)["pageno"] == "3"  # (20 // 10) + 1


def test_searxng_request_override_beats_static_engine_list(httpx_mock):
    httpx_mock.add_response(json={"results": []})
    SearxngAdapter(SEARXNG_URL, engines=["google"]).search(
        SearchRequest(query="q", engine_overrides={"searxng": {"engines": ["mojeek"]}})
    )
    assert _sent_params(httpx_mock)["engines"] == "mojeek"


def test_searxng_empty_thumbnail_becomes_none(httpx_mock):
    httpx_mock.add_response(
        json={"results": [{"url": "https://x.test/a", "thumbnail": "", "img_src": ""}]}
    )
    out = SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q"))
    assert out.results[0].thumbnail is None


def test_searxng_reuses_owned_client(httpx_mock, monkeypatch):
    import httpx

    httpx_mock.add_response(json={"results": []})
    httpx_mock.add_response(json={"results": []})
    constructed: list[int] = []
    real_client = httpx.Client

    class CountingClient(real_client):
        def __init__(self, *args, **kwargs):
            constructed.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("websearch.layer1_search.adapters.searxng.httpx.Client", CountingClient)
    adapter = SearxngAdapter(SEARXNG_URL)
    adapter.search(SearchRequest(query="a"))
    adapter.search(SearchRequest(query="b"))
    assert len(constructed) == 1  # one owned client, reused across searches


def test_searxng_non_object_json_is_error_not_crash(httpx_mock):
    httpx_mock.add_response(json=["not", "an", "object"])
    out = SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q"))
    assert out.error is not None
    assert out.results == []


def test_searxng_skips_non_dict_result_entries(httpx_mock):
    httpx_mock.add_response(
        json={"results": ["bogus", {"url": "https://ok/1", "title": "T", "content": "c"}]}
    )
    out = SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q"))
    assert out.error is None
    assert [r.url for r in out.results] == ["https://ok/1"]


def test_searxng_http_error_returns_error_output(httpx_mock):
    httpx_mock.add_response(status_code=502)
    out = SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q"))
    assert out.error is not None
    assert out.results == []


def test_searxng_coerces_published_date_and_score(httpx_mock):
    httpx_mock.add_response(
        json={
            "results": [
                {
                    "url": "https://x/1",
                    "title": "T",
                    "content": "c",
                    "score": 2,
                    "publishedDate": 1234,
                }
            ]
        }
    )
    out = SearxngAdapter(SEARXNG_URL).search(SearchRequest(query="q"))
    r = out.results[0]
    assert r.published_date == "1234"  # non-string upstream value coerced to str
    assert r.raw_score == 2.0  # int coerced to float


def test_searxng_disabled_without_url():
    out = SearxngAdapter(None).search(SearchRequest(query="q"))
    assert out.error is not None
    assert SearxngAdapter(None).enabled() is False


# --- ddgs ---------------------------------------------------------------------------


class _RecordingDDGS:
    def __init__(self, sink: dict):
        self._sink = sink

    def text(self, query, **kwargs):
        self._sink["query"] = query
        self._sink["kwargs"] = kwargs
        return []


def _run_ddgs(request: SearchRequest) -> dict:
    sink: dict = {}
    DdgsAdapter(ddgs_factory=lambda: _RecordingDDGS(sink)).search(request)
    return sink


def test_ddgs_region_uses_primary_language_subtag():
    sink = _run_ddgs(SearchRequest(query="q", country="US", language="en-GB"))
    assert sink["kwargs"]["region"] == "us-en"  # not the invalid "us-en-gb"


def test_ddgs_freshness_safesearch_and_count_kwargs():
    sink = _run_ddgs(SearchRequest(query="q", freshness="week", safesearch="strict", count=7))
    assert sink["kwargs"]["timelimit"] == "w"
    assert sink["kwargs"]["safesearch"] == "on"
    assert sink["kwargs"]["max_results"] == 7


def test_ddgs_without_country_omits_region():
    sink = _run_ddgs(SearchRequest(query="q"))
    assert "region" not in sink["kwargs"]


def test_ddgs_library_failure_becomes_error_output():
    class _Boom:
        def text(self, query, **kwargs):
            raise RuntimeError("ddgs down")

    out = DdgsAdapter(ddgs_factory=lambda: _Boom()).search(SearchRequest(query="q"))
    assert out.error is not None
    assert "ddgs down" in out.error


def test_ddgs_news_requests_the_news_index():
    sink: dict = {}

    class _NewsRecorder:
        def text(self, query, **kwargs):
            raise AssertionError("text index must not be queried for a news request")

        def news(self, query, **kwargs):
            sink["query"] = query
            return [{"url": "https://n.test/a", "title": "t", "body": "b"}]

    out = DdgsAdapter(ddgs_factory=lambda: _NewsRecorder()).search(
        SearchRequest(query="q", result_type="news")
    )
    assert sink["query"] == "q"
    assert out.results[0].result_type == "news"


def test_ddgs_timeout_reaches_client_and_client_is_reused(monkeypatch):
    captured: list[dict] = []

    class FakeDDGS:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def text(self, query, **kwargs):
            return []

    import ddgs

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    adapter = DdgsAdapter()
    adapter.search(SearchRequest(query="q", timeout_ms=30000))
    adapter.search(SearchRequest(query="q", timeout_ms=30000))
    assert captured == [{"timeout": 30}]  # request timeout honored; one client built
