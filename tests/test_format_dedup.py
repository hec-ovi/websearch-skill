"""Unit tests for the dedup stage: byte-exact, MinHash near-dup, and clustering."""

from __future__ import annotations

from websearch.layer2_format.dedup import (
    DupItem,
    content_hash,
    dedup_items,
    estimated_jaccard,
    make_permutations,
    minhash_signature,
    normalize_body,
)

_BASE = (
    "Rust ownership manages memory deterministically and the borrow checker enforces the "
    "rules statically at compile time without a garbage collector running in the background."
)
_NEAR = _BASE + " In practice you rarely write lifetimes out by hand for everyday programs."
_FAR = (
    "Python uses reference counting plus a cyclic garbage collector to reclaim memory "
    "automatically at runtime, so the developer never frees an object explicitly at all."
)


def _items(*pairs):
    out = []
    for i, (url, body, score) in enumerate(pairs):
        out.append(
            DupItem(url=url, body=body, score=score, content_hash=content_hash(body), order=i)
        )
    return out


def test_normalize_collapses_whitespace_preserves_case():
    assert normalize_body("  A\t b\n\nC  ") == "A b C"


def test_content_hash_stable_and_whitespace_insensitive():
    assert content_hash("hello   world") == content_hash("hello world\n")
    assert content_hash("Hello") != content_hash("hello")  # case preserved


def test_minhash_near_vs_far():
    perms = make_permutations(128)
    s_base = minhash_signature(_BASE, num_perm=128, shingle_size=4, perms=perms)
    s_near = minhash_signature(_NEAR, num_perm=128, shingle_size=4, perms=perms)
    s_far = minhash_signature(_FAR, num_perm=128, shingle_size=4, perms=perms)
    assert estimated_jaccard(s_base, s_near) > 0.6
    assert estimated_jaccard(s_base, s_far) < 0.2
    assert estimated_jaccard(s_base, s_base) == 1.0


def test_minhash_signature_deterministic_across_calls():
    a = minhash_signature(_BASE, num_perm=64, shingle_size=4)
    b = minhash_signature(_BASE, num_perm=64, shingle_size=4)
    assert a == b  # fixed permutation seed


def test_byte_exact_folds_identical_bodies():
    items = _items(
        ("https://a.test/1", _BASE, 0.5),
        ("https://b.test/2", _BASE, 0.9),  # identical body, higher score -> canonical
    )
    clusters = dedup_items(items, method="exact")
    assert len(clusters) == 1
    assert clusters[0].canonical.url == "https://b.test/2"  # best score wins
    assert clusters[0].duplicates[0][1] == "exact"


def test_minhash_folds_near_duplicate_keeps_canonical():
    items = _items(
        ("https://a.test/base", _BASE, 0.9),
        ("https://a.test/near", _NEAR, 0.4),
        ("https://a.test/far", _FAR, 0.5),
    )
    clusters = dedup_items(items, method="minhash", jaccard_threshold=0.6)
    survivors = {c.canonical.url for c in clusters}
    assert "https://a.test/far" in survivors
    # base and near collapsed to one canonical (the higher-scored base)
    assert "https://a.test/base" in survivors
    assert "https://a.test/near" not in survivors
    base_cluster = next(c for c in clusters if c.canonical.url == "https://a.test/base")
    assert base_cluster.duplicates[0][0].url == "https://a.test/near"
    assert base_cluster.duplicates[0][1] == "minhash"
    assert base_cluster.duplicates[0][2] is not None  # similarity recorded


def test_high_threshold_keeps_near_duplicates_separate():
    items = _items(
        ("https://a.test/base", _BASE, 0.9),
        ("https://a.test/near", _NEAR, 0.4),
    )
    clusters = dedup_items(items, method="minhash", jaccard_threshold=0.99)
    assert len(clusters) == 2  # not similar enough at 0.99


def test_disabled_dedup_returns_every_item():
    items = _items(("https://a.test/1", _BASE, 0.5), ("https://b.test/2", _BASE, 0.9))
    clusters = dedup_items(items, enabled=False)
    assert len(clusters) == 2
    assert all(not c.duplicates for c in clusters)


def test_both_method_exact_then_minhash():
    items = _items(
        ("https://a.test/1", _BASE, 0.5),
        ("https://a.test/2", _BASE, 0.9),  # exact dup of 1
        ("https://a.test/3", _NEAR, 0.4),  # near dup of base
        ("https://a.test/4", _FAR, 0.6),  # distinct
    )
    clusters = dedup_items(items, method="both", jaccard_threshold=0.6)
    survivors = {c.canonical.url for c in clusters}
    assert "https://a.test/4" in survivors
    assert len(clusters) == 2  # {base cluster}, {far}
    base_cluster = next(c for c in clusters if c.canonical.url == "https://a.test/2")
    reasons = sorted(d[1] for d in base_cluster.duplicates)
    assert reasons == ["exact", "minhash"]


def test_empty_and_single_body_safe():
    assert dedup_items([]) == []
    one = _items(("https://a.test/1", "", None))
    clusters = dedup_items(one, method="both")
    assert len(clusters) == 1


def test_distinct_empty_bodies_are_not_folded():
    # Snippet-only or failed-extraction results all hash to the empty-string digest;
    # they must NOT collapse into one canonical (would silently drop distinct URLs).
    items = _items(
        ("https://a.test/1", "", 0.9),
        ("https://b.test/2", "   ", 0.5),
        ("https://c.test/3", "", 0.1),
    )
    for method in ("exact", "both"):
        clusters = dedup_items(items, method=method)
        assert len(clusters) == 3, method
        assert all(not c.duplicates for c in clusters)
