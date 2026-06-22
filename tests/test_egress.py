"""SSRF egress guard: scheme allowlist + private/reserved address rejection."""

from __future__ import annotations

import pytest

from websearch.layer2_extract.egress import BlockedEgress, guard_url

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
