"""Dedup + provenance merge behavior."""

from __future__ import annotations

from websearch.layer1_search.capability import GENERAL_AGGREGATOR, NEURAL_INDEX
from websearch.layer1_search.dedup import dedupe
from websearch.layer1_search.port import RawResult


def tagged(*items):
    # items: (engine, group, url, rank, title, snippet)
    return [(e, g, RawResult(url=u, title=t, snippet=s, rank=r)) for (e, g, u, r, t, s) in items]


def test_empty_best_ranked_title_filled_from_another_source():
    docs = dedupe(
        tagged(
            ("a", GENERAL_AGGREGATOR, "https://x.com/1", 1, "", "snippet a"),  # best rank, no title
            ("b", NEURAL_INDEX, "https://x.com/1", 2, "Real Title", "snippet b"),
        )
    )
    assert len(docs) == 1
    assert docs[0].title == "Real Title"


def test_better_ranked_real_title_wins():
    docs = dedupe(
        tagged(
            ("a", GENERAL_AGGREGATOR, "https://x.com/1", 2, "Worse", "s"),
            ("b", NEURAL_INDEX, "https://x.com/1", 1, "Better", "s"),
        )
    )
    assert docs[0].title == "Better"


def test_merge_accumulates_sources_and_distinct_snippets():
    docs = dedupe(
        tagged(
            ("a", GENERAL_AGGREGATOR, "https://x.com/1", 1, "T", "snip one"),
            ("b", NEURAL_INDEX, "https://x.com/1", 1, "T", "snip two"),
            ("b", NEURAL_INDEX, "https://x.com/1", 2, "T", "snip two"),  # duplicate snippet
        )
    )
    assert len(docs) == 1
    assert {s.engine for s in docs[0].sources} == {"a", "b"}
    assert docs[0].snippets == ["snip one", "snip two"]


def test_blank_url_is_dropped():
    docs = dedupe(tagged(("a", GENERAL_AGGREGATOR, "", 1, "T", "s")))
    assert docs == []
