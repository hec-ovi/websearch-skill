"""SSRF egress guard: scheme allowlist + private/reserved address rejection."""

from __future__ import annotations

import pytest

from websearch.layer2_extract.egress import BlockedEgress, guard_url


class _Resolver:
    """Records every hostname the guard asks the local resolver about."""

    def __init__(self, addrs: set[str] | None = None):
        self.asked: list[str] = []
        self._addrs = addrs or {"93.184.216.34"}

    def __call__(self, host: str) -> set[str]:
        self.asked.append(host)
        return self._addrs


def test_behind_a_proxy_no_hostname_is_resolved_locally():
    """The DNS-leak regression: with a proxy, the local resolver must never see the host.

    Resolving here would show the ISP every site the tool visits while the traffic itself
    is tunneled, which is precisely what the proxy was turned on to prevent.
    """
    resolver = _Resolver()
    guard_url("https://example.com/page", resolve=resolver, proxied=True)
    assert resolver.asked == []


def test_without_a_proxy_the_hostname_is_still_resolved():
    resolver = _Resolver()
    guard_url("https://example.com/page", resolve=resolver, proxied=False)
    assert resolver.asked == ["example.com"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
        "http://[::1]:9000/",
    ],
)
def test_literal_internal_addresses_stay_refused_even_behind_a_proxy(url):
    """Skipping DNS must not skip the guard: a literal IP needs no lookup to judge."""
    resolver = _Resolver()
    with pytest.raises(BlockedEgress):
        guard_url(url, resolve=resolver, proxied=True)
    assert resolver.asked == []


def test_non_http_schemes_stay_refused_behind_a_proxy():
    for url in ("file:///etc/passwd", "gopher://x/", "dict://x:11/"):
        with pytest.raises(BlockedEgress):
            guard_url(url, proxied=True)


def test_a_configured_proxy_locks_the_guard_without_an_explicit_lock(monkeypatch):
    """Defense in depth for the implied lock: with a proxy in the environment, a fetch
    that somehow arrives at a tier with no proxy is refused rather than sent direct."""
    monkeypatch.setenv("WEBSEARCH_PROXY", "socks5h://u:p@proxy.test:1080")
    with pytest.raises(BlockedEgress, match="no proxy"):
        guard_url("https://example.com/", resolve=_Resolver(), proxied=False)


PUBLIC = lambda host: {"93.184.216.34"}  # noqa: E731


def test_allows_public_host():
    guard_url("https://example.com/path", resolve=PUBLIC)  # no raise


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/1", "dict://x/", "ftp://x/y"])
def test_scheme_allowlist(url):
    with pytest.raises(BlockedEgress):
        guard_url(url, resolve=PUBLIC)


@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.1", "192.168.1.1", "172.16.0.1", "169.254.169.254", "::1", "0.0.0.0"],
)
def test_literal_internal_ip_blocked(ip):
    host = f"[{ip}]" if ":" in ip else ip
    with pytest.raises(BlockedEgress):
        guard_url(f"http://{host}/", resolve=PUBLIC)


def test_public_literal_ip_allowed():
    guard_url("http://93.184.216.34/", resolve=PUBLIC)


def test_hostname_resolving_to_private_is_blocked():
    with pytest.raises(BlockedEgress):
        guard_url("https://sneaky.test/", resolve=lambda h: {"127.0.0.1"})


def test_cloud_metadata_endpoint_blocked():
    # 169.254.169.254 is link-local; the classic SSRF target.
    with pytest.raises(BlockedEgress):
        guard_url("http://169.254.169.254/latest/meta-data/", resolve=PUBLIC)


def test_allow_private_bypasses_guard():
    guard_url("https://internal.test/", allow_private=True, resolve=lambda h: {"127.0.0.1"})


def test_resolution_failure_is_blocked():
    def boom(host):
        raise OSError("name resolution failed")

    with pytest.raises(BlockedEgress):
        guard_url("https://nope.test/", resolve=boom)


def test_mixed_resolution_blocks_if_any_address_is_internal():
    with pytest.raises(BlockedEgress):
        guard_url("https://rebind.test/", resolve=lambda h: {"93.184.216.34", "127.0.0.1"})


def test_all_unparseable_resolved_entries_fail_closed():
    # A resolver yielding only garbage must not fall through to "allowed": the guard
    # requires at least one successfully parsed, non-internal address.
    with pytest.raises(BlockedEgress):
        guard_url("https://weird.test/", resolve=lambda h: {"not-an-ip", "also-garbage"})


def test_garbage_entries_alongside_a_public_address_are_tolerated():
    guard_url("https://ok.test/", resolve=lambda h: {"garbage", "93.184.216.34"})  # no raise


def test_cgnat_addresses_are_refused():
    """RFC 6598 (100.64.0.0/10) is the range a private/loopback denylist silently misses."""
    with pytest.raises(BlockedEgress):
        guard_url("http://100.64.0.1/")
    with pytest.raises(BlockedEgress):
        guard_url("http://nat.test/", resolve=lambda host: {"100.100.0.5"})
