"""Provenance-aware weighted Reciprocal Rank Fusion (RRF).

The L1 finding's load-bearing objection: naively fusing engines that share an
underlying crawler (SearXNG and ddgs both scrape Google/Bing) lets a consensus bonus
amplify the same crawler agreeing with itself, so the fused ranking can be *worse*
than a single well-tuned engine. The fix, implemented here, is mandatory
de-correlation:

1. group a doc's sources by ``correlation_group``;
2. each group contributes a single RRF term using its best (lowest) rank, so
   correlated engines count as one independent vote, not several;
3. the optional consensus bonus scales only with the number of *distinct* groups.

So SearXNG + ddgs agreeing gives one vote; SearXNG + Exa agreeing gives two.
"""

from __future__ import annotations

from .dedup import DedupedDoc
from .models import Fusion

# Per distinct extra correlation group, multiply the score by this much.
_CONSENSUS_STEP = 0.10


def _weight_for(engine: str, method: str, weights: dict[str, float] | None) -> float:
    if method == "rrf":
        return 1.0
    if weights and engine in weights:
        return weights[engine]
    return 1.0


def fused_score(doc: DedupedDoc, fusion: Fusion) -> float:
    """Compute one doc's fused score under the de-correlation rule."""
    k = fusion.k
    # score_convex is not implemented yet; treat it as weighted_rrf (the router warns).
    method = "weighted_rrf" if fusion.method == "score_convex" else fusion.method

    # Best rank and best weight per correlation group.
    best_rank: dict[str, int] = {}
    best_weight: dict[str, float] = {}
    for s in doc.sources:
        w = _weight_for(s.engine, method, fusion.weights)
        if s.group not in best_rank or s.rank < best_rank[s.group]:
            best_rank[s.group] = s.rank
        if s.group not in best_weight or w > best_weight[s.group]:
            best_weight[s.group] = w

    base = sum(best_weight[g] / (k + best_rank[g]) for g in best_rank)

    num_groups = len(best_rank)
    if fusion.consensus_bonus and num_groups > 1:
        base *= 1.0 + _CONSENSUS_STEP * (num_groups - 1)
    return base


def fuse(docs: list[DedupedDoc], fusion: Fusion) -> list[tuple[DedupedDoc, float]]:
    """Score and sort docs by fused score (desc).

    Ties break by best overall rank (asc) then canonical URL (asc) for determinism.
    """
    scored = [(doc, fused_score(doc, fusion)) for doc in docs]

    def sort_key(item: tuple[DedupedDoc, float]):
        doc, score = item
        best_overall = min((s.rank for s in doc.sources), default=10**9)
        return (-score, best_overall, doc.url)

    scored.sort(key=sort_key)
    return scored
