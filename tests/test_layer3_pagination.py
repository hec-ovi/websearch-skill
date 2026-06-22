"""Token-budget pagination: lossless splitting, within budget, edge cases.

The contract is that pagination is progressive disclosure, never a content cap: the pages
concatenate back to the exact original, so no content is ever dropped.
"""

from __future__ import annotations

import pytest

from websearch.layer3_agentio.pagination import paginate

_CASES = [
    "",
    "short",
    "a\nb\nc\n",
    "line one\nline two\nline three\n",
    "x" * 1000,  # one oversized line, no trailing newline
    "word " * 500,  # long single line with spaces
    "para1\n\n" + ("y" * 600) + "\n\npara3\n",
    "héllo wörld\n" * 200,  # unicode
]


@pytest.mark.parametrize("md", _CASES)
def test_pagination_is_lossless(md):
    pages = paginate(md, page_size_tokens=20, chars_per_token=4.0)
    assert "".join(pages) == md
    assert len(pages) >= 1


@pytest.mark.parametrize("md", _CASES)
def test_every_page_within_budget(md):
    budget = 20 * 4
    pages = paginate(md, page_size_tokens=20, chars_per_token=4.0)
    assert all(len(p) <= budget for p in pages)


def test_oversized_single_line_is_hard_split_losslessly():
    md = "x" * 1000
    pages = paginate(md, page_size_tokens=5, chars_per_token=4.0)  # budget 20
    assert "".join(pages) == md
    assert all(len(p) <= 20 for p in pages)
    assert len(pages) >= 50


def test_empty_document_is_a_single_empty_page():
    assert paginate("", page_size_tokens=100) == [""]


def test_small_document_is_a_single_page():
    assert paginate("short enough", page_size_tokens=100) == ["short enough"]
