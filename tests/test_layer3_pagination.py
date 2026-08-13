"""Token-budget pagination: lossless splitting, within budget, edge cases.

The contract is that pagination is progressive disclosure, never a content cap: the pages
concatenate back to the exact original, so no content is ever dropped.
"""

from __future__ import annotations

import pytest

from websearch.layer3_agentio.pagination import paginate

_CASES = [
    "",
    "héllo wörld\n" * 200,  # multiline unicode
    "x" * 1000,  # one oversized line that must hard-split, no trailing newline
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


@pytest.mark.parametrize("md", _CASES)
def test_zero_budget_disables_pagination(md):
    # 0 = no budget: the whole body as one page, still lossless by construction.
    assert paginate(md, page_size_tokens=0) == [md]
