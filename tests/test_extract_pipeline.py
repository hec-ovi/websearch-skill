"""Pipeline orchestration: how fetch + extract outcomes map to the Envelope."""

from __future__ import annotations

from tests.conftest import ARTICLE_HTML, EXTRACT_RESPONSE_REF
from websearch.layer2_extract import FetchExtractPipeline, FetchRouter, TrafilaturaExtractor
from websearch.layer2_extract.exceptions import DependencyMissing
from websearch.layer2_extract.models import ExtractResult, FetchRequest, FetchResult
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


def test_non_html_content_type_skips_extraction():
    fetcher = _FetcherReturning(
        status=200, ok=True, raw_html="%PDF-1.7 binary", content_type="application/pdf"
    )
    env = _pipeline(fetcher).run(FetchRequest(url="https://x.test/file.pdf"))
    assert env.ok is True
    assert env.data["result"]["extracted_via"] == "none"
    assert any("not HTML" in w for w in env.data["warnings"])


def test_request_id_present_in_meta_on_success_and_error():
    ok = _pipeline(_FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)).run(
        FetchRequest(url="https://x.test/")
    )
    err = _pipeline(_FetcherReturning(status=0, ok=False, error="boom")).run(
        FetchRequest(url="https://x.test/")
    )
    assert ok.meta.request_id  # type: ignore[attr-defined]
    assert err.meta.request_id  # type: ignore[attr-defined]


def test_override_collision_does_not_crash():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)
    env = _pipeline(fetcher).run(
        FetchRequest(url="https://x.test/"),
        extract_overrides={"html": "INJECTED", "base_url": "https://evil.test/"},
    )
    assert env.ok is True  # reserved keys are stripped, no TypeError


def test_unavailable_engine_falls_back_to_default_with_warning():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)
    env = _pipeline(fetcher).run(
        FetchRequest(url="https://x.test/"), extract_overrides={"engine": "resiliparse"}
    )
    assert env.ok is True
    assert any("resiliparse" in w and "opt-in" in w for w in env.data["warnings"])
    assert env.data["result"]["extracted_via"] == "trafilatura"


class _ExtractorNamed(ExtractAdapter):
    name = "jina_readerlm"

    def extract(self, request):
        return ExtractResult(
            content_markdown="neural output", quality_score=1.0, extracted_via=self.name
        )


def test_injected_extractor_matching_requested_engine_does_not_warn():
    # An injected custom extractor satisfies its own engine name: no "opt-in adapter
    # not installed" warning, and the request runs through that extractor.
    fetcher = _FetcherReturning(status=200, ok=True, raw_html="<p>x</p>")
    env = _pipeline(fetcher, _ExtractorNamed()).run(
        FetchRequest(url="https://x.test/"), extract_overrides={"engine": "jina_readerlm"}
    )
    assert env.ok is True
    assert not any("opt-in" in w for w in env.data["warnings"])
    assert env.data["result"]["extracted_via"] == "jina_readerlm"


def test_reserved_wait_for_and_politeness_warn():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)
    env = _pipeline(fetcher).run(
        FetchRequest(
            url="https://x.test/",
            wait_for="#main",
            politeness={"per_host_delay_ms": 500, "respect_robots": True},
        )
    )
    assert env.ok is True
    warnings = env.data["warnings"]
    assert any("wait_for is reserved" in w for w in warnings)
    assert any("politeness is reserved" in w for w in warnings)


def test_default_politeness_and_no_wait_for_do_not_warn():
    fetcher = _FetcherReturning(status=200, ok=True, raw_html=ARTICLE_HTML)
    env = _pipeline(fetcher).run(FetchRequest(url="https://x.test/"))
    assert not any("reserved" in w for w in env.data["warnings"])
