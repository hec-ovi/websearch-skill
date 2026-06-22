"""Shared test fixtures: the contract registry, a schema-validation helper, and fakes.

The registry loads every contract file by ``$id`` so cross-file ``$ref``s (the
SearchResponse referencing the Envelope) resolve exactly as in production. This is the
consumer-driven contract check: tests validate real CLI/router output against the
frozen JSON Schemas, so a producer change that breaks the shape fails CI.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"

ENVELOPE_ID = "https://github.com/hec-ovi/websearch-skill/contracts/envelope.schema.json"
SEARCH_ID = "https://github.com/hec-ovi/websearch-skill/contracts/search.schema.json"

SEARCH_RESPONSE_REF = f"{SEARCH_ID}#/$defs/SearchResponse"
SEARCH_REQUEST_REF = f"{SEARCH_ID}#/$defs/SearchRequest"
SEARCH_PAYLOAD_REF = f"{SEARCH_ID}#/$defs/SearchPayload"


def _build_registry() -> Registry:
    pairs = []
    for path in sorted(CONTRACTS.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        pairs.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(pairs)


REGISTRY = _build_registry()


def schema_errors(instance: Any, ref: str) -> list[str]:
    validator = Draft202012Validator({"$ref": ref}, registry=REGISTRY)
    return [
        f"{e.message} (at {list(e.absolute_path)})"
        for e in sorted(validator.iter_errors(instance), key=lambda e: str(list(e.absolute_path)))
    ]


@pytest.fixture
def assert_valid():
    def _assert(instance: Any, ref: str) -> None:
        errors = schema_errors(instance, ref)
        assert not errors, "contract violations:\n" + "\n".join(f"- {e}" for e in errors)

    return _assert


# --- Fakes for the external engine boundaries -------------------------------------


class FakeDDGS:
    """Stands in for ddgs.DDGS (the external network boundary)."""

    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []

    def text(self, query: str, **kwargs: Any) -> list[dict]:
        return list(self._rows)


def ddgs_factory(rows: list[dict]):
    return lambda *a, **k: FakeDDGS(rows)


# --- Canned engine payloads used by the e2e ---------------------------------------

SEARXNG_JSON = {
    "query": "rust",
    "number_of_results": 3,
    "results": [
        {
            "url": "https://example.com/rust-guide",
            "title": "Rust Guide",
            "content": "A guide to the Rust programming language.",
            "engine": "google",
            "score": 1.0,
            "category": "general",
        },
        {
            "url": "https://www.python.org/?utm_source=newsletter",
            "title": "Python",
            "content": "The official Python website.",
            "engine": "google",
            "score": 0.8,
        },
        {
            "url": "https://blog.dev/post",
            "title": "A Blog Post",
            "content": "Some blog content.",
            "engine": "bing",
            "score": 0.5,
        },
    ],
    "answers": ["Rust is a systems programming language."],
    "suggestions": ["rust book"],
    "corrections": [],
}

DDGS_ROWS = [
    {
        "title": "Rust Lang",
        "href": "https://example.com/rust-guide",
        "body": "Official-ish overview of the Rust language and its tooling.",
    },
    {"title": "The Rust Book", "href": "https://doc.rust-lang.org/book", "body": "Learn Rust."},
    {"title": "Python.org", "href": "https://python.org", "body": "Python home page."},
]
