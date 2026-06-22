"""Fetcher fidelity: httpx Tier 0 (mocked at the network layer) and curl_cffi
(faked at the libcurl boundary, the same way ddgs is faked)."""

from __future__ import annotations

import httpx

from tests.conftest import (
    ARTICLE_HTML,
    CLOUDFLARE_HTML,
    RecordingCurlGetter,
    fake_curl_getter,
)
from websearch.layer2_extract.fetchers.curl_cffi_fetcher import CurlCffiFetcher
from websearch.layer2_extract.fetchers.httpx_fetcher import HttpxFetcher
from websearch.layer2_extract.fetchers.util import DEFAULT_USER_AGENT
from websearch.layer2_extract.models import FetchRequest

URL = "https://page.test/article"


# --- httpx Tier 0 ------------------------------------------------------------------


def test_httpx_success(httpx_mock):
    httpx_mock.add_response(html=ARTICLE_HTML)
    res = HttpxFetcher().fetch(FetchRequest(url=URL))
    assert res.ok is True
    assert res.status == 200
    assert res.blocked is False
    assert res.fetched_via == "http"
    assert res.raw_html and "Rust Ownership" in res.raw_html


def test_httpx_sends_default_user_agent(httpx_mock):
    httpx_mock.add_response(html="<html></html>")
    HttpxFetcher().fetch(FetchRequest(url=URL))
    assert httpx_mock.get_requests()[0].headers["user-agent"] == DEFAULT_USER_AGENT


def test_httpx_sends_custom_user_agent(httpx_mock):
    httpx_mock.add_response(html="<html></html>")
    HttpxFetcher().fetch(FetchRequest(url=URL, user_agent="my-agent/1.0"))
    assert httpx_mock.get_requests()[0].headers["user-agent"] == "my-agent/1.0"


def test_httpx_detects_cloudflare_block(httpx_mock):
    httpx_mock.add_response(status_code=403, html=CLOUDFLARE_HTML)
    res = HttpxFetcher().fetch(FetchRequest(url=URL))
    assert res.blocked is True
    assert res.block_reason == "cloudflare_challenge"
    assert res.ok is False


def test_httpx_transport_error_yields_status_zero(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("connection refused"))
    res = HttpxFetcher().fetch(FetchRequest(url=URL))
    assert res.status == 0
    assert res.ok is False
    assert res.error and "ConnectError" in res.error


def test_httpx_max_bytes_is_a_transport_guard(httpx_mock):
    httpx_mock.add_response(html="<html>" + "x" * 5000 + "</html>")
    res = HttpxFetcher().fetch(FetchRequest(url=URL, max_bytes=64))
    assert res.raw_html is not None
    assert len(res.raw_html) == 64  # body capped to the guard, not the full 5KB


# --- curl_cffi escalation tier -----------------------------------------------------


def test_curl_cffi_success_via_fake_getter():
    fetcher = CurlCffiFetcher(getter=fake_curl_getter(ARTICLE_HTML))
    res = fetcher.fetch(FetchRequest(url=URL))
    assert res.ok is True
    assert res.fetched_via == "curl_cffi"
    assert res.raw_html and "Rust Ownership" in res.raw_html


def test_curl_cffi_passes_impersonate_and_proxy_kwargs():
    rec = RecordingCurlGetter()
    fetcher = CurlCffiFetcher(getter=rec, impersonate="chrome131")
    fetcher.fetch(
        FetchRequest(
            url=URL,
            timeout_ms=15000,
            proxy={"url": "socks5h://127.0.0.1:1080", "type": "socks5"},
        )
    )
    _url, kwargs = rec.calls[0]
    assert kwargs["impersonate"] == "chrome131"
    assert kwargs["timeout"] == 15.0
    assert kwargs["proxies"] == {
        "http": "socks5h://127.0.0.1:1080",
        "https": "socks5h://127.0.0.1:1080",
    }


def test_curl_cffi_detects_block_from_response():
    fetcher = CurlCffiFetcher(getter=fake_curl_getter(CLOUDFLARE_HTML, status_code=403))
    res = fetcher.fetch(FetchRequest(url=URL))
    assert res.blocked is True
    assert res.block_reason == "cloudflare_challenge"


def test_curl_cffi_library_exception_becomes_error_result():
    def boom(url, **kwargs):
        raise RuntimeError("curl down")

    res = CurlCffiFetcher(getter=boom).fetch(FetchRequest(url=URL))
    assert res.status == 0
    assert res.ok is False
    assert "curl down" in (res.error or "")


def test_curl_cffi_available_with_injected_getter():
    assert CurlCffiFetcher(getter=fake_curl_getter("x")).available() is True
