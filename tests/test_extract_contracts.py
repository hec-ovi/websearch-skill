"""Consumer-driven contract conformance for the Layer 2A schemas.

Real model output is validated against the frozen fetch/extract JSON Schemas, and a
few deliberately-malformed instances confirm the schemas reject what they should.
"""

from __future__ import annotations

from tests.conftest import (
    EXTRACT_PAYLOAD_REF,
    EXTRACT_REQUEST_REF,
    EXTRACT_RESULT_REF,
    FETCH_REQUEST_REF,
    FETCH_RESULT_REF,
    schema_errors,
)
from websearch.layer2_extract.models import (
    ExtractPayload,
    ExtractRequest,
    ExtractResult,
    ExtractSource,
    FetchRequest,
    FetchResult,
)


def _json(model) -> dict:
    return model.model_dump(mode="json")


def test_fetch_request_valid(assert_valid):
    assert_valid(_json(FetchRequest(url="https://x.test/", tier_hint="auto")), FETCH_REQUEST_REF)


def test_fetch_request_rejects_unknown_field():
    instance = _json(FetchRequest(url="https://x.test/"))
    instance["bogus"] = 1
    assert schema_errors(instance, FETCH_REQUEST_REF)


def test_fetch_request_requires_url():
    assert schema_errors({"tier_hint": "auto"}, FETCH_REQUEST_REF)


def test_fetch_result_valid(assert_valid):
    r = FetchResult(url="https://x.test/", status=200, ok=True, fetched_via="curl_cffi")
    assert_valid(_json(r), FETCH_RESULT_REF)


def test_fetch_result_rejects_bad_fetched_via():
    instance = _json(FetchResult(url="https://x.test/", status=200, ok=True, fetched_via="http"))
    instance["fetched_via"] = "telepathy"
    assert schema_errors(instance, FETCH_RESULT_REF)


def test_extract_request_valid(assert_valid):
    assert_valid(_json(ExtractRequest(html="<p>x</p>", engine="trafilatura")), EXTRACT_REQUEST_REF)


def test_extract_result_valid(assert_valid):
    r = ExtractResult(content_markdown="# Hi", quality_score=0.9, extracted_via="trafilatura")
    assert_valid(_json(r), EXTRACT_RESULT_REF)


def test_extract_result_quality_out_of_range_rejected():
    instance = _json(
        ExtractResult(content_markdown="x", quality_score=0.5, extracted_via="trafilatura")
    )
    instance["quality_score"] = 1.5
    assert schema_errors(instance, EXTRACT_RESULT_REF)


def test_extract_result_bad_page_type_rejected():
    instance = _json(
        ExtractResult(content_markdown="x", quality_score=0.5, extracted_via="trafilatura")
    )
    instance["page_type"] = "memo"
    assert schema_errors(instance, EXTRACT_RESULT_REF)


def test_extract_payload_valid(assert_valid):
    payload = ExtractPayload(
        request_id="abc",
        source=ExtractSource(url="https://x.test/", status=200, ok=True, fetched_via="http"),
        result=ExtractResult(
            content_markdown="# Hi", quality_score=0.9, extracted_via="trafilatura"
        ),
    )
    assert_valid(_json(payload), EXTRACT_PAYLOAD_REF)
