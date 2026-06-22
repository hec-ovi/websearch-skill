"""Near-duplicate detection: byte-exact first, then pure-Python MinHash.

A two-stage pipeline (the standard used by RefinedWeb / SlimPajama / GPT-3 dedup):

1. **Byte-exact** - hash the lightly-normalized body (strip + collapse whitespace,
   case preserved) with SHA-256 and fold identical hashes together.
2. **MinHash near-duplicate** - on the survivors, estimate Jaccard similarity over
   word 4-gram shingles (char 5-grams for very short bodies) using a fixed family of
   ``num_perm`` min-wise-independent hashes, cluster pairs at or above the threshold
   with union-find, and keep one canonical per cluster.

This is pure stdlib (``hashlib`` + ``random``); ``datasketch`` is deliberately avoided
(it pulls in numpy and its LSH only earns its keep at far larger corpora than a single
search-to-format cycle produces), and SimHash is avoided (its only edge is a compact
fingerprint for billion-page scale; MinHash is more accurate here).

The canonical of a cluster is the highest-scored member, then the longest body, then
the first seen; the rest are recorded as dropped duplicates on the canonical.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field

# Universal-hashing constants for the (a*h + b) mod prime min-wise hash family.
_PRIME = (1 << 61) - 1
_MAXH = (1 << 32) - 1
# Fixed seed so every document is hashed with the SAME permutation family. Changing
# it changes signatures but not correctness; it must never vary between documents.
_SEED = 0x5EED

_CHAR_SHINGLE_SIZE = 5  # fallback when a body has fewer than shingle_size words


def normalize_body(text: str | None) -> str:
    """Light normalization for hashing/shingling: strip and collapse whitespace."""
    return " ".join((text or "").split())


def content_hash(text: str | None) -> str:
    """Stable SHA-256 of the normalized body (case preserved). The byte-exact key."""
    return hashlib.sha256(normalize_body(text).encode("utf-8")).hexdigest()


def _shingles(text: str, k: int) -> set[str]:
    """Word k-gram shingles (lowercased); char 5-grams for bodies shorter than k words."""
    tokens = normalize_body(text).lower().split()
    if len(tokens) >= k:
        return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}
    compact = "".join(tokens)
    ck = _CHAR_SHINGLE_SIZE
    if len(compact) >= ck:
        return {compact[i : i + ck] for i in range(len(compact) - ck + 1)}
    return {compact} if compact else set()


def _shingle_hash(shingle: str) -> int:
    return int.from_bytes(hashlib.sha1(shingle.encode("utf-8")).digest()[:4], "big")


def make_permutations(num_perm: int, seed: int = _SEED) -> tuple[list[int], list[int]]:
    """The fixed (a, b) coefficient lists for ``num_perm`` min-wise hashes."""
    rng = random.Random(seed)
    a = [rng.randint(1, _PRIME - 1) for _ in range(num_perm)]
    b = [rng.randint(0, _PRIME - 1) for _ in range(num_perm)]
    return a, b


def minhash_signature(
    text: str,
    *,
    num_perm: int,
    shingle_size: int,
    perms: tuple[list[int], list[int]] | None = None,
) -> list[int] | None:
    """MinHash signature of ``text``; None when the body yields no shingles."""
    a, b = perms or make_permutations(num_perm)
    shingles = _shingles(text, shingle_size)
    if not shingles:
        return None
    sig = [_MAXH] * num_perm
    for sh in shingles:
        h = _shingle_hash(sh)
        for i in range(num_perm):
            v = ((a[i] * h + b[i]) % _PRIME) & _MAXH
            if v < sig[i]:
                sig[i] = v
    return sig


def estimated_jaccard(sig1: list[int] | None, sig2: list[int] | None) -> float:
    """Estimated Jaccard = fraction of equal signature slots."""
    if not sig1 or not sig2 or len(sig1) != len(sig2):
        return 0.0
    equal = sum(1 for x, y in zip(sig1, sig2, strict=True) if x == y)
    return equal / len(sig1)


class _DSU:
    """Minimal union-find for clustering near-duplicate pairs."""

    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, x: int, y: int) -> None:
        self.parent[self.find(x)] = self.find(y)


@dataclass
class DupItem:
    """A dedup input record. ``order`` preserves first-seen for stable canonical choice."""

    url: str
    body: str
    score: float | None
    content_hash: str
    order: int
    payload: object = None  # the caller's object, carried through untouched


@dataclass
class DupCluster:
    canonical: DupItem
    duplicates: list[tuple[DupItem, str, float | None]] = field(default_factory=list)
    # each duplicate: (item, reason "exact"|"minhash", similarity-or-None)


def _canonical_key(item: DupItem) -> tuple[float, int, int]:
    """Higher score, then longer body, then earlier order wins (sorted descending)."""
    return (item.score if item.score is not None else float("-inf"), len(item.body), -item.order)


def dedup_items(
    items: list[DupItem],
    *,
    enabled: bool = True,
    method: str = "both",
    jaccard_threshold: float = 0.9,
    num_perm: int = 128,
    shingle_size: int = 4,
) -> list[DupCluster]:
    """Cluster ``items`` into one canonical per near/exact-duplicate group.

    Returns one DupCluster per surviving canonical, in the input order of its canonical.
    """
    if not items:
        return []
    if not enabled:
        return [DupCluster(canonical=it) for it in items]

    do_exact = method in ("exact", "both")
    do_minhash = method in ("minhash", "both")

    # Stage 1: byte-exact grouping by content hash.
    exact_groups: dict[str, list[DupItem]] = {}
    exact_order: list[str] = []
    for it in items:
        key = it.content_hash if do_exact else f"__unique__{it.order}"
        if key not in exact_groups:
            exact_groups[key] = []
            exact_order.append(key)
        exact_groups[key].append(it)

    # Each exact group collapses to a canonical; the rest are exact duplicates.
    reps: list[DupItem] = []
    exact_dupes: dict[int, list[tuple[DupItem, str, float | None]]] = {}
    for key in exact_order:
        group = sorted(exact_groups[key], key=_canonical_key, reverse=True)
        canon = group[0]
        reps.append(canon)
        exact_dupes[canon.order] = [(d, "exact", None) for d in group[1:]]

    # Stage 2: MinHash near-duplicate clustering over the exact-survivors.
    if do_minhash and len(reps) > 1:
        perms = make_permutations(num_perm)
        sigs = [
            minhash_signature(r.body, num_perm=num_perm, shingle_size=shingle_size, perms=perms)
            for r in reps
        ]
        dsu = _DSU(len(reps))
        sims: dict[tuple[int, int], float] = {}
        for i in range(len(reps)):
            if sigs[i] is None:
                continue
            for j in range(i + 1, len(reps)):
                if sigs[j] is None:
                    continue
                sim = estimated_jaccard(sigs[i], sigs[j])
                if sim >= jaccard_threshold:
                    dsu.union(i, j)
                    sims[(i, j)] = sim
    else:
        dsu = None
        sims = {}

    if dsu is None:
        return [DupCluster(canonical=r, duplicates=exact_dupes.get(r.order, [])) for r in reps]

    # Group exact-survivors by their union-find root, choose a canonical per root.
    by_root: dict[int, list[int]] = {}
    for idx in range(len(reps)):
        by_root.setdefault(dsu.find(idx), []).append(idx)

    clusters: list[DupCluster] = []
    for members in by_root.values():
        members_sorted = sorted(members, key=lambda m: _canonical_key(reps[m]), reverse=True)
        canon_idx = members_sorted[0]
        canon = reps[canon_idx]
        dup_records = list(exact_dupes.get(canon.order, []))
        for m in members_sorted[1:]:
            sim = sims.get((min(m, canon_idx), max(m, canon_idx)))
            dup_records.append((reps[m], "minhash", sim))
            dup_records.extend(exact_dupes.get(reps[m].order, []))
        clusters.append(DupCluster(canonical=canon, duplicates=dup_records))

    # Stable output: ascending by the canonical's first-seen order.
    clusters.sort(key=lambda c: c.canonical.order)
    return clusters
