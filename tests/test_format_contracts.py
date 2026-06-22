"""Consumer-driven contract conformance for the Layer 2B schemas.

Real model output is validated against the frozen format/store JSON Schemas, and a few
deliberately-malformed instances confirm the schemas reject what they should.
"""

from __future__ import annotations

from tests.conftest import (
    ANTHROPIC_BLOCK_REF,
    FORMAT_REQUEST_REF,
    FORMAT_RESULT_INPUT_REF,
    STORE_PAGE_INPUT_REF,
    STORE_SEARCH_REQUEST_REF,
    schema_errors,
)
from websearch.layer2_format import (
    FormatRequest,
    PageInput,
    ResultInput,
    SearchPageRequest,
)
from websearch.layer2_format.models import (
    AnthropicCitations,
    AnthropicSearchResultBlock,
    AnthropicTextBlock,
)


def _json(model) -> dict:
    return model.model_dump(mode="json")


def test_result_input_valid(assert_valid):
    assert_valid(
        _json(ResultInput(url="https://x.test/", title="T", score=0.5, body_markdown="# B")),
        FORMAT_RESULT_INPUT_REF,
    )


def test_result_input_requires_url():
    assert schema_errors({"title": "no url"}, FORMAT_RESULT_INPUT_REF)


def test_result_input_rejects_unknown_field():
    inst = _json(ResultInput(url="https://x.test/"))
    inst["bogus"] = 1
    assert schema_errors(inst, FORMAT_RESULT_INPUT_REF)


def test_format_request_valid(assert_valid):
    req = FormatRequest(
        query="q",
        results=[ResultInput(url="https://x.test/", body_markdown="b")],
        page=0,
        page_size=5,
        mode="auto",
    )
    assert_valid(_json(req), FORMAT_REQUEST_REF)


def test_format_request_requires_results():
    assert schema_errors({"query": "q"}, FORMAT_REQUEST_REF)


def test_anthropic_block_valid(assert_valid):
    block = AnthropicSearchResultBlock(
        source="https://x.test/",
        title="T",
        content=[AnthropicTextBlock(text="hello")],
        citations=AnthropicCitations(enabled=True),
    )
    assert_valid(block.to_block(), ANTHROPIC_BLOCK_REF)


def test_anthropic_block_rejects_empty_content():
    block = AnthropicSearchResultBlock(
        source="https://x.test/", title="T", content=[AnthropicTextBlock(text="hi")]
    )
    inst = block.to_block()
    inst["content"] = []  # violates minItems
    assert schema_errors(inst, ANTHROPIC_BLOCK_REF)


def test_anthropic_block_source_must_be_string():
    block = AnthropicSearchResultBlock(
        source="https://x.test/", title="T", content=[AnthropicTextBlock(text="hi")]
    )
    inst = block.to_block()
    inst["source"] = {"url": "https://x.test/"}  # document-block shape, not search_result
    assert schema_errors(inst, ANTHROPIC_BLOCK_REF)


def test_anthropic_block_drops_unset_optionals():
    block = AnthropicSearchResultBlock(
        source="https://x.test/", title="T", content=[AnthropicTextBlock(text="hi")]
    )
    inst = block.to_block()
    assert "citations" not in inst  # omitted, never null
    assert "cache_control" not in inst


def test_page_input_valid(assert_valid):
    assert_valid(_json(PageInput(url="https://x.test/", markdown="# B")), STORE_PAGE_INPUT_REF)


def test_search_page_request_valid(assert_valid):
    assert_valid(_json(SearchPageRequest(query="rust", top_k=10)), STORE_SEARCH_REQUEST_REF)


def test_search_page_request_rejects_empty_query():
    assert schema_errors({"query": ""}, STORE_SEARCH_REQUEST_REF)
