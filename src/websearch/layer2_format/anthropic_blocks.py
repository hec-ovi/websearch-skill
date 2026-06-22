"""Derived view: map FormattedResults onto Anthropic ``search_result`` content blocks.

This is an OPTIONAL, vendor-specific projection off the vendor-neutral results, never
the canonical shape. Each block matches the verified Anthropic messages-API schema:
``source`` is a bare URL string (not a nested object), ``title`` is a plain string, and
``content`` is a list of at least one non-empty text block (the minimal citable unit).

Citations are all-or-nothing across a request, so ``citations.enabled`` is set uniformly
on every block (or omitted entirely). Layer 3 owns the toggle: a caller that also uses
structured outputs must disable citations, because citations plus ``output_config.format``
is an Anthropic API error.
"""

from __future__ import annotations

from .models import (
    AnthropicCitations,
    AnthropicSearchResultBlock,
    AnthropicTextBlock,
    FormattedResult,
)


def _content_texts(r: FormattedResult) -> list[str]:
    """At least one non-empty text, drawn from the richest source available."""
    blocks = [b for b in r.body_blocks if b and b.strip()]
    if blocks:
        return blocks
    highlights = [h.text for h in r.highlights if h.text and h.text.strip()]
    if highlights:
        return highlights
    if r.summary and r.summary.strip():
        return [r.summary]
    if r.body_markdown and r.body_markdown.strip():
        return [r.body_markdown]
    # Guarantee a non-empty block so the contract's minItems/minLength always holds.
    return [r.title or r.url]


def to_anthropic_blocks(
    results: list[FormattedResult], *, citations: bool = True
) -> list[dict]:
    """Build the ``anthropic_search_result_blocks`` array (serialized, optionals dropped)."""
    blocks: list[dict] = []
    for r in results:
        block = AnthropicSearchResultBlock(
            source=r.url,
            title=r.title or r.url,
            content=[AnthropicTextBlock(text=t) for t in _content_texts(r)],
            citations=AnthropicCitations(enabled=True) if citations else None,
        )
        blocks.append(block.to_block())
    return blocks
