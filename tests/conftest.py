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
FETCH_ID = "https://github.com/hec-ovi/websearch-skill/contracts/fetch.schema.json"
EXTRACT_ID = "https://github.com/hec-ovi/websearch-skill/contracts/extract.schema.json"
FORMAT_ID = "https://github.com/hec-ovi/websearch-skill/contracts/format.schema.json"
STORE_ID = "https://github.com/hec-ovi/websearch-skill/contracts/store.schema.json"

SEARCH_RESPONSE_REF = f"{SEARCH_ID}#/$defs/SearchResponse"
SEARCH_REQUEST_REF = f"{SEARCH_ID}#/$defs/SearchRequest"
SEARCH_PAYLOAD_REF = f"{SEARCH_ID}#/$defs/SearchPayload"

FETCH_REQUEST_REF = f"{FETCH_ID}#/$defs/FetchRequest"
FETCH_RESULT_REF = f"{FETCH_ID}#/$defs/FetchResult"
EXTRACT_REQUEST_REF = f"{EXTRACT_ID}#/$defs/ExtractRequest"
EXTRACT_RESULT_REF = f"{EXTRACT_ID}#/$defs/ExtractResult"
EXTRACT_PAYLOAD_REF = f"{EXTRACT_ID}#/$defs/ExtractPayload"
EXTRACT_RESPONSE_REF = f"{EXTRACT_ID}#/$defs/ExtractResponse"

FORMAT_REQUEST_REF = f"{FORMAT_ID}#/$defs/FormatRequest"
FORMAT_RESULT_INPUT_REF = f"{FORMAT_ID}#/$defs/ResultInput"
FORMAT_PAYLOAD_REF = f"{FORMAT_ID}#/$defs/FormatPayload"
FORMAT_RESPONSE_REF = f"{FORMAT_ID}#/$defs/FormatResponse"
FORMAT_SIDECAR_REF = f"{FORMAT_ID}#/$defs/FormatSidecar"
ANTHROPIC_BLOCK_REF = f"{FORMAT_ID}#/$defs/AnthropicSearchResultBlock"

STORE_ADD_RESULT_REF = f"{STORE_ID}#/$defs/AddResult"
STORE_SEARCH_REQUEST_REF = f"{STORE_ID}#/$defs/SearchPageRequest"
STORE_SEARCH_RESULT_REF = f"{STORE_ID}#/$defs/SearchPageResult"
STORE_PAGE_DOC_REF = f"{STORE_ID}#/$defs/PageDocument"
STORE_RESOLVE_INDEX_REF = f"{STORE_ID}#/$defs/ResolveIndex"
STORE_PAGE_INPUT_REF = f"{STORE_ID}#/$defs/PageInput"

AGENTIO_ID = "https://github.com/hec-ovi/websearch-skill/contracts/agent-io.schema.json"
AGENTIO_SEARCH_REQUEST_REF = f"{AGENTIO_ID}#/$defs/AgentSearchRequest"
AGENTIO_SEARCH_PAYLOAD_REF = f"{AGENTIO_ID}#/$defs/AgentSearchPayload"
AGENTIO_SEARCH_RESPONSE_REF = f"{AGENTIO_ID}#/$defs/AgentSearchResponse"
AGENTIO_FETCH_REQUEST_REF = f"{AGENTIO_ID}#/$defs/AgentFetchRequest"
AGENTIO_OPEN_REQUEST_REF = f"{AGENTIO_ID}#/$defs/AgentOpenRequest"
AGENTIO_FETCH_PAYLOAD_REF = f"{AGENTIO_ID}#/$defs/AgentFetchPayload"
AGENTIO_FETCH_RESPONSE_REF = f"{AGENTIO_ID}#/$defs/AgentFetchResponse"
AGENTIO_PAGE_REF = f"{AGENTIO_ID}#/$defs/AgentPage"


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


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """In tests, hostnames resolve to a public IP so the SSRF egress guard does not
    block the fake test hosts. Tests that exercise the guard itself pass their own
    resolver to egress.guard_url, which bypasses this."""
    monkeypatch.setattr(
        "websearch.layer2_extract.egress._resolve",
        lambda host: {"93.184.216.34"},
    )


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


# --- Layer 2A fixtures: canned HTML and a curl_cffi library-boundary fake ----------

# A substantial article: clears the 0.80 quality gate, carries JSON-LD + og:type.
ARTICLE_HTML = """<!doctype html><html lang="en"><head>
<title>Understanding Rust Ownership</title>
<meta property="og:type" content="article">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Article","headline":"Understanding Rust Ownership",
 "author":{"@type":"Person","name":"Jane Dev"},"datePublished":"2026-05-01"}
</script></head><body>
<header><nav><a href="/">Home</a></nav></header>
<article><h1>Understanding Rust Ownership</h1>
<p>Ownership is the mechanism Rust uses to manage memory. Every value in Rust has a single
variable that owns it, and there can be only one owner at a time. When the owner goes out of
scope, the value is dropped and its memory is freed automatically.</p>
<p>This discipline eliminates whole classes of bugs. Use-after-free, double-free, and data
races are rejected by the compiler instead of crashing the program at runtime. The borrow
checker enforces the rules statically, so the costs are paid at compile time.</p>
<p>Borrowing lets a function reference a value without taking ownership of it. Shared borrows
are immutable and may overlap; a mutable borrow is exclusive. See
<a href="https://doc.rust-lang.org/book">the Rust book</a> for the full treatment.</p>
<p>Lifetimes annotate how long a reference stays valid so the compiler can reject dangling
pointers. Most lifetimes are inferred, and you rarely write them out by hand in practice.</p>
</article><footer>Copyright 2026</footer></body></html>"""

# A Cloudflare interstitial: returned with status 200 or 403, not real content.
CLOUDFLARE_HTML = """<!doctype html><html><head><title>Just a moment...</title></head>
<body><div id="cf-challenge-running"></div>
<p>Checking your browser before you access the site.</p>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
</body></html>"""


class FakeCurlResponse:
    """A curl_cffi.Response stand-in (status_code/text/content/headers/url/encoding)."""

    def __init__(
        self,
        text: str,
        status_code: int = 200,
        headers: dict | None = None,
        url: str | None = None,
        encoding: str = "utf-8",
        content: bytes | None = None,
    ):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/html"}
        self.url = url or "https://example.test/"
        self.encoding = encoding
        # content may diverge from a UTF-8 encoding of text (e.g. a latin-1 body),
        # which is how the real Response behaves and what the fetcher decodes from.
        self.content = content if content is not None else text.encode("utf-8")


def fake_curl_getter(
    text: str,
    status_code: int = 200,
    headers: dict | None = None,
    content: bytes | None = None,
):
    """A drop-in for curl_cffi.get(url, **kwargs) that returns a canned response."""

    def _get(url: str, **kwargs: Any) -> FakeCurlResponse:
        return FakeCurlResponse(
            text, status_code=status_code, headers=headers, url=url, content=content
        )

    return _get


class RecordingCurlGetter:
    """Captures the kwargs the curl_cffi fetcher passes to the library."""

    def __init__(self, text: str = "<html><body><p>ok</p></body></html>", status_code: int = 200):
        self.calls: list[tuple[str, dict]] = []
        self._text = text
        self._status = status_code

    def __call__(self, url: str, **kwargs: Any) -> FakeCurlResponse:
        self.calls.append((url, kwargs))
        return FakeCurlResponse(self._text, status_code=self._status, url=url)
