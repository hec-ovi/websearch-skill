"""Unit tests for Markdown chunking: heading-aware and fixed, with verbatim offsets."""

from __future__ import annotations

from websearch.layer2_format.chunk import chunk_markdown
from websearch.layer2_format.tokens import estimate_tokens

DOC = (
    "# Title\n\nIntro paragraph about the subject at hand.\n\n"
    "## Section A\n\nBody of section A with some detail.\n\n"
    "## Section B\n\nBody of section B with more detail."
)


def test_empty_input_yields_no_passages():
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  ") == []


def test_heading_aware_splits_at_headings():
    chunks = chunk_markdown(DOC, strategy="heading", max_chars=10_000)
    assert len(chunks) == 3  # intro + 2 sections
    texts = [c[0] for c in chunks]
    assert texts[0].startswith("# Title")
    assert texts[1].startswith("## Section A")
    assert texts[2].startswith("## Section B")


def test_offsets_slice_back_verbatim():
    for text, start, end in chunk_markdown(DOC, strategy="heading"):
        assert DOC[start:end] == text


def test_fixed_strategy_windows_cover_document():
    chunks = chunk_markdown(DOC, strategy="fixed", max_chars=20, overlap=0)
    assert len(chunks) > 1
    for text, start, end in chunks:
        assert DOC[start:end] == text
        assert end - start <= 20


def test_oversized_section_is_split_but_offsets_hold():
    big = "# H\n\n" + ("word " * 500)
    chunks = chunk_markdown(big, strategy="heading", max_chars=200)
    assert len(chunks) > 1
    for text, start, end in chunks:
        assert big[start:end] == text
        assert end - start <= 200


def test_fixed_overlap_repeats_boundary_text():
    text = "abcdefghijklmnopqrstuvwxyz"
    chunks = chunk_markdown(text, strategy="fixed", max_chars=10, overlap=3)
    # step = 7, so windows start at 0, 7, 14, 21
    starts = [s for _t, s, _e in chunks]
    assert starts[0] == 0 and starts[1] == 7


def test_token_estimate_default_and_pluggable():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 8) == 2  # ceil(8/4)
    assert estimate_tokens("a" * 8, chars_per_token=2) == 4
    assert estimate_tokens("whatever", estimator=lambda t: 99) == 99
