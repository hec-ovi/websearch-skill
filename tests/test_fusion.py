"""Provenance-aware de-correlated weighted RRF.

These pin the L1 finding's load-bearing rule: engines that share a correlation group
count as one independent vote (best rank wins, no double-count), and the consensus
bonus applies only across distinct groups. So two correlated engines agreeing must
not outrank two decorrelated engines agreeing at the same ranks.
"""

from __future__ import annotations

import math

from websearch.layer1_search.capability import GENERAL_AGGREGATOR, NEURAL_INDEX
from websearch.layer1_search.dedup import DedupedDoc, DocSource
from websearch.layer1_search.fusion import _CONSENSUS_STEP, fuse, fused_score
from websearch.layer1_search.models import Fusion


def doc(url: str, sources: list[tuple[str, str, int]]) -> DedupedDoc:
    return DedupedDoc(
        url=url,
        display_url=url,
        title="t",
        snippet="s",
        snippets=["s"],
        sources=[DocSource(engine=e, group=g, rank=r) for (e, g, r) in sources],
    )


K = 60


def test_same_group_collapses_to_one_vote():
    f = Fusion(k=K)
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 1), ("ddgs", GENERAL_AGGREGATOR, 1)])
    # one group -> single RRF term at the best rank, no consensus bonus
    assert math.isclose(fused_score(d, f), 1 / (K + 1), rel_tol=1e-12)


def test_best_rank_within_group_wins():
    f = Fusion(k=K)
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 5), ("ddgs", GENERAL_AGGREGATOR, 2)])
    assert math.isclose(fused_score(d, f), 1 / (K + 2), rel_tol=1e-12)


def test_distinct_groups_sum_and_get_consensus_bonus():
    f = Fusion(k=K, consensus_bonus=True)
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 1), ("exa", NEURAL_INDEX, 1)])
    base = 1 / (K + 1) + 1 / (K + 1)
    expected = base * (1 + _CONSENSUS_STEP * 1)
    assert math.isclose(fused_score(d, f), expected, rel_tol=1e-12)


def test_consensus_bonus_can_be_disabled():
    f = Fusion(k=K, consensus_bonus=False)
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 1), ("exa", NEURAL_INDEX, 1)])
    assert math.isclose(fused_score(d, f), 2 / (K + 1), rel_tol=1e-12)


def test_decorrelated_agreement_beats_correlated_agreement():
    f = Fusion(k=K)
    correlated = doc("corr", [("searxng", GENERAL_AGGREGATOR, 1), ("ddgs", GENERAL_AGGREGATOR, 1)])
    decorrelated = doc("deco", [("searxng", GENERAL_AGGREGATOR, 1), ("exa", NEURAL_INDEX, 1)])
    ranked = fuse([correlated, decorrelated], f)
    assert [d.url for d, _ in ranked] == ["deco", "corr"]


def test_weighted_rrf_applies_weights_rrf_ignores_them():
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 1)])
    weighted = Fusion(method="weighted_rrf", k=K, weights={"searxng": 2.0})
    plain = Fusion(method="rrf", k=K, weights={"searxng": 2.0})
    assert math.isclose(fused_score(d, weighted), 2.0 / (K + 1), rel_tol=1e-12)
    assert math.isclose(fused_score(d, plain), 1.0 / (K + 1), rel_tol=1e-12)


def test_group_weight_is_max_over_engines_in_group():
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 3), ("ddgs", GENERAL_AGGREGATOR, 1)])
    f = Fusion(method="weighted_rrf", k=K, weights={"searxng": 2.0, "ddgs": 1.0})
    # best rank in the group is 1 (ddgs); group weight is max(2.0, 1.0) = 2.0
    assert math.isclose(fused_score(d, f), 2.0 / (K + 1), rel_tol=1e-12)


def test_score_convex_falls_back_to_weighted_rrf():
    d = doc("u", [("searxng", GENERAL_AGGREGATOR, 1)])
    convex = Fusion(method="score_convex", k=K)
    weighted = Fusion(method="weighted_rrf", k=K)
    assert math.isclose(fused_score(d, convex), fused_score(d, weighted), rel_tol=1e-12)
