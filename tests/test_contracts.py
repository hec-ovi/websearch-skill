"""Schema-level guarantees of the frozen contracts."""

from __future__ import annotations

from tests.conftest import (
    ENVELOPE_ID,
    SEARCH_PAYLOAD_REF,
    SEARCH_REQUEST_REF,
    SEARCH_RESPONSE_REF,
    schema_errors,
)


def test_minimal_and_rich_search_requests_validate(assert_valid):
    assert_valid({"query": "hello"}, SEARCH_REQUEST_REF)
    assert_valid(
        {
            "query": "hello",
            "count": 5,
            "freshness": "week",
            "fusion": {"method": "rrf", "k": 30},
            "egress": {"enabled": True, "profile": "wireguard-de"},
        },
        SEARCH_REQUEST_REF,
    )


def test_search_request_requires_query():
    assert schema_errors({}, SEARCH_REQUEST_REF)


def test_search_request_rejects_unknown_field():
    assert schema_errors({"query": "x", "bogus": 1}, SEARCH_REQUEST_REF)


def test_freshness_custom_range():
    assert not schema_errors(
        {"query": "q", "freshness": {"start": "2026-01-01", "end": "2026-02-01"}},
        SEARCH_REQUEST_REF,
    )
    # a partial range matches neither branch of the oneOf
    assert schema_errors({"query": "q", "freshness": {"start": "2026-01-01"}}, SEARCH_REQUEST_REF)


def test_envelope_rejects_extra_top_level_key():
    env = {
        "contract_version": "1.0.0",
        "ok": True,
        "data": None,
        "error": None,
        "meta": {"layer": "search", "backend": None, "elapsed_ms": 1},
        "surprise": 1,
    }
    assert schema_errors(env, ENVELOPE_ID)


def test_search_response_pins_layer_to_search():
    wrong_layer = {
        "contract_version": "1.0.0",
        "ok": True,
        "data": None,
        "error": None,
        "meta": {"layer": "extract", "backend": None, "elapsed_ms": 1},
    }
    assert schema_errors(wrong_layer, SEARCH_RESPONSE_REF)


def test_minimal_search_payload_validates(assert_valid):
    assert_valid(
        {"query": "q", "request_id": "id-1", "results": [], "engines_queried": ["ddgs"]},
        SEARCH_PAYLOAD_REF,
    )


def test_result_item_requires_provenance():
    # a ResultItem with no sources violates minItems:1 on sources
    payload = {
        "query": "q",
        "request_id": "id-1",
        "engines_queried": ["ddgs"],
        "results": [
            {
                "url": "https://x/1",
                "display_url": "https://x/1",
                "title": "t",
                "snippet": "s",
                "fused_score": 0.1,
                "sources": [],
            }
        ],
    }
    assert schema_errors(payload, SEARCH_PAYLOAD_REF)
