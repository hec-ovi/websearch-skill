"""URL canonicalization rules (the dedup key)."""

from __future__ import annotations

import pytest

from websearch.layer1_search.canonical import canonicalize_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        # lowercase scheme+host, strip www, drop fragment, strip utm, sort params
        (
            "https://www.Example.com/Path/?utm_source=x&b=2&a=1#frag",
            "https://example.com/Path?a=1&b=2",
        ),
        # trailing slash dropped on non-root paths
        ("http://example.com/a/", "http://example.com/a"),
        # bare domain normalizes to root "/"
        ("https://python.org", "https://python.org/"),
        ("https://www.python.org/?utm_source=foo", "https://python.org/"),
        # default ports stripped, non-default kept
        ("https://EXAMPLE.com:443/p", "https://example.com/p"),
        ("https://example.com:8080/p", "https://example.com:8080/p"),
        # multiple tracking params removed
        ("https://example.com/p?gclid=1&fbclid=2&keep=ok", "https://example.com/p?keep=ok"),
        # path case is preserved (paths are case-sensitive)
        ("https://example.com/CaseSensitive", "https://example.com/CaseSensitive"),
        ("", ""),
    ],
)
def test_canonicalize(raw, expected):
    assert canonicalize_url(raw) == expected


def test_www_and_bare_domain_collapse_to_same_key():
    a = canonicalize_url("https://www.python.org/?utm_source=foo")
    b = canonicalize_url("https://python.org")
    assert a == b
