"""Pipeline orchestration: how fetch + extract outcomes map to the Envelope."""

from __future__ import annotations

from tests.conftest import ARTICLE_HTML, EXTRACT_RESPONSE_REF
from websearch.layer2_extract import FetchExtractPipeline, FetchRouter, TrafilaturaExtractor
from websearch.layer2_extract.exceptions import DependencyMissing
from websearch.layer2_extract.models import FetchRequest, FetchResult
from websearch.layer2_extract.ports import ExtractAdapter, FetchAdapter


class _FetcherReturning(FetchAdapter):
    name = "fake"
    fetched_via = "http"
    tier_class = "http"
    escalation_order = 0

    def __init__(self, **kw):
        self._kw = kw

    def fetch(self, request: FetchRequest) -> FetchResult:
        return FetchResult(url=request.url, fetched_via="http", **self._kw)


class _FetcherRaising(FetchAdapter):
    name = "fake"
    fetched_via = "http"
    tier_class = "http"
    escalation_order = 0

    def __init__(self, exc: Exception):
        self._exc = exc

    def fetch(self, request: FetchRequest) -> FetchResult:
        raise self._exc


class _ExtractorRaising(ExtractAdapter):
    name = "trafilatura"

    def __init__(self, exc: Exception):
        self._exc = exc

    def extract(self, request):
        raise self._exc


def _pipeline(fetcher: FetchAdapter, extractor=None) -> FetchExtractPipeline:
    return FetchExtractPipeline(FetchRouter([fetcher]), extractor or TrafilaturaExtractor())


def test_success_envelope_matches_contract(assert_valid):
    fetcher = _FetcherReturning(
        status=200, ok=True, raw_html=ARTICLE_HTML, content_type="text/html"
    )
    env = _pipeline(fetcher).run(FetchRequest(url="https://example.com/blog/rust"))
    payload = env.model_dump(mode="json")
    assert_valid(payload, EXTRACT_RESPONSE_REF)
    assert payload["ok"] is True
    assert payload["meta"]["layer"] == "extract"
    assert payload["data"]["result"]["page_type"] == "article"


def test_transport_failure_is_fetch_failed():
    env = _pipeline(_FetcherReturning(status=0, ok=False, error="ConnectError: refused")).run(
        FetchRequest(url="https://x.test/")
    )
    assert env.ok is False
    assert env.error.code == "fetch_failed"
    assert env.error.retriable is True


def test_blocked_page_still_returns_content_with_warning():
    fetcher = _FetcherReturning(
        status=403,
        ok=False,
        blocked=True,
        block_reason="cloudflare_challenge",
        raw_html="<title>Just a moment...</title><p>checking your browser</p>",
    )
    env = _pipeline(fetcher).run(FetchRequest(url="https://x.test/"))
    assert env.ok is True  # we returned what we got
    data = env.data
    assert data["source"]["blocked"] is True
    assert any("blocked" in w for w in data["warnings"])


def test_http_404_returns_ok_envelope_with_warning():
    fetcher = _FetcherReturning(status=404, ok=False, raw_html="<html><body>nope</body></html>")
    env = _pipeline(fetcher).run(FetchRequest(url="https://x.test/"))
    assert env.ok is True
    assert env.data["source"]["status"] == 404
    assert any("HTTP 404" in w for w in env.data["warnings"])


def test_extractor_exception_is_extract_failed():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html="<p>x</p>")
    env = _pipeline(fetcher, _ExtractorRaising(RuntimeError("boom"))).run(
        FetchRequest(url="https://x.test/")
    )
    assert env.ok is False
    assert env.error.code == "extract_failed"


def test_dependency_missing_on_fetch_is_clean_error():
    env = _pipeline(_FetcherRaising(DependencyMissing("curl_cffi"))).run(
        FetchRequest(url="https://x.test/")
    )
    assert env.ok is False
    assert env.error.code == "dependency_missing"


def test_dependency_missing_on_extract_is_clean_error():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html="<p>x</p>")
    env = _pipeline(fetcher, _ExtractorRaising(DependencyMissing("trafilatura"))).run(
        FetchRequest(url="https://x.test/")
    )
    assert env.ok is False
    assert env.error.code == "dependency_missing"


def test_unavailable_engine_falls_back_to_default_with_warning():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)
    env = _pipeline(fetcher).run(
        FetchRequest(url="https://x.test/"), extract_overrides={"engine": "resiliparse"}
    )
    assert env.ok is True
    assert any("resiliparse" in w and "opt-in" in w for w in env.data["warnings"])
    assert env.data["result"]["extracted_via"] == "trafilatura"
