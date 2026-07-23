"""Fetcher fidelity: httpx Tier 0 (mocked at the network layer) and curl_cffi
(faked at the libcurl boundary, the same way ddgs is faked)."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import (
    ARTICLE_HTML,
    CLOUDFLARE_HTML,
    FakeCurlResponse,
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


def test_curl_cffi_decodes_declared_latin1_content():
    getter = fake_curl_getter(
        "",
        content="Café résumé".encode("latin-1"),
        headers={"content-type": "text/html; charset=latin-1"},
    )
    res = CurlCffiFetcher(getter=getter).fetch(FetchRequest(url=URL))
    assert "Café résumé" in res.raw_html


class _StreamingCurlResponse:
    """A streamed curl_cffi Response stand-in: body only via iter_content()."""

    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.chunks_read = 0
        self.closed = False
        self.status_code = status_code
        self.headers = {"content-type": "text/html"}
        self.url = URL

    def iter_content(self):
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True


def test_curl_cffi_max_bytes_stops_the_streaming_download():
    # 100 KiB on offer; the guard must stop the transfer at 2 chunks, not buffer it all.
    resp = _StreamingCurlResponse([b"x" * 1024] * 100)
    res = CurlCffiFetcher(getter=lambda url, **kw: resp).fetch(
        FetchRequest(url=URL, max_bytes=2048)
    )
    assert res.status == 200
    assert res.raw_html is not None and len(res.raw_html) == 2048
    assert resp.chunks_read == 2  # download stopped mid-body, not after full buffering
    assert resp.closed is True


class _ContentOnlyResponse:
    """A response whose .text must never be touched (content is authoritative)."""

    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    url = URL
    content = "<html><body><p>Café body</p></body></html>".encode()

    @property
    def text(self):
        raise AssertionError("resp.text must not be decoded when content is present")


def test_curl_cffi_uses_content_without_decoding_text():
    res = CurlCffiFetcher(getter=lambda url, **kw: _ContentOnlyResponse()).fetch(
        FetchRequest(url=URL)
    )
    assert res.ok is True
    assert "Café body" in res.raw_html


def test_curl_cffi_reuses_one_session_across_requests():
    pytest.importorskip("curl_cffi")
    fetcher = CurlCffiFetcher()
    fetcher._resolve_getter()
    session = fetcher._session
    assert session is not None
    fetcher._resolve_getter()
    assert fetcher._session is session  # one Session, reused, never rebuilt per request


# --- redirect credential hygiene ----------------------------------------------------


class _RedirectThenOkGetter:
    """First call: 30x to ``location``; then 200 OK. Records kwargs per call."""

    def __init__(self, location: str):
        self._location = location
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if len(self.calls) == 1:
            return FakeCurlResponse(
                "",
                status_code=302,
                headers={"location": self._location, "content-type": "text/html"},
                url=url,
            )
        return FakeCurlResponse("<html><body><p>ok</p></body></html>", url=url)


AUTH_HEADERS = {"Authorization": "Bearer tok", "X-Custom": "1"}
SID_COOKIE = [{"name": "sid", "value": "s1"}]


def test_curl_cffi_cross_origin_redirect_drops_auth_and_cookies():
    rec = _RedirectThenOkGetter("https://other.test/next")
    res = CurlCffiFetcher(getter=rec).fetch(
        FetchRequest(url="https://page.test/a", headers=AUTH_HEADERS, cookies=SID_COOKIE)
    )
    assert res.status == 200
    first, second = rec.calls[0][1], rec.calls[1][1]
    assert first["headers"]["Authorization"] == "Bearer tok"
    assert first["cookies"] == {"sid": "s1"}
    assert "Authorization" not in second["headers"]
    assert "cookies" not in second
    assert second["headers"]["X-Custom"] == "1"  # non-credential headers still sent


def test_curl_cffi_same_host_redirect_keeps_auth_and_cookies():
    rec = _RedirectThenOkGetter("/next")
    res = CurlCffiFetcher(getter=rec).fetch(
        FetchRequest(url="https://page.test/a", headers=AUTH_HEADERS, cookies=SID_COOKIE)
    )
    assert res.status == 200
    second = rec.calls[1][1]
    assert second["headers"]["Authorization"] == "Bearer tok"
    assert second["cookies"] == {"sid": "s1"}


def test_curl_cffi_domain_scoped_cookie_goes_only_to_matching_host():
    rec = _RedirectThenOkGetter("https://other.test/next")
    CurlCffiFetcher(getter=rec).fetch(
        FetchRequest(
            url="https://page.test/a",
            cookies=[{"name": "sid", "value": "s1", "domain": "other.test"}],
        )
    )
    assert "cookies" not in rec.calls[0][1]  # not for the origin host
    assert rec.calls[1][1]["cookies"] == {"sid": "s1"}  # sent to the matching host


def test_httpx_cross_origin_redirect_drops_auth_and_cookies(httpx_mock):
    httpx_mock.add_response(
        url="https://start.test/", status_code=302, headers={"location": "https://other.test/next"}
    )
    httpx_mock.add_response(url="https://other.test/next", html="<p>ok</p>")
    res = HttpxFetcher().fetch(
        FetchRequest(url="https://start.test/", headers=AUTH_HEADERS, cookies=SID_COOKIE)
    )
    assert res.status == 200
    first, second = httpx_mock.get_requests()
    assert first.headers["Authorization"] == "Bearer tok"
    assert first.headers["Cookie"] == "sid=s1"
    assert "Authorization" not in second.headers
    assert "Cookie" not in second.headers
    assert second.headers["X-Custom"] == "1"


def test_httpx_same_host_redirect_keeps_auth_and_cookies(httpx_mock):
    httpx_mock.add_response(
        url="https://start.test/", status_code=302, headers={"location": "/next"}
    )
    httpx_mock.add_response(url="https://start.test/next", html="<p>ok</p>")
    res = HttpxFetcher().fetch(
        FetchRequest(url="https://start.test/", headers=AUTH_HEADERS, cookies=SID_COOKIE)
    )
    assert res.status == 200
    second = httpx_mock.get_requests()[1]
    assert second.headers["Authorization"] == "Bearer tok"
    assert second.headers["Cookie"] == "sid=s1"


def test_httpx_domain_scoped_cookie_goes_only_to_matching_host(httpx_mock):
    httpx_mock.add_response(
        url="https://start.test/", status_code=302, headers={"location": "https://other.test/next"}
    )
    httpx_mock.add_response(url="https://other.test/next", html="<p>ok</p>")
    HttpxFetcher().fetch(
        FetchRequest(
            url="https://start.test/",
            cookies=[{"name": "sid", "value": "s1", "domain": "other.test"}],
        )
    )
    first, second = httpx_mock.get_requests()
    assert "Cookie" not in first.headers
    assert second.headers["Cookie"] == "sid=s1"


# --- SSRF guard + redirect validation + charset (httpx tier) -----------------------


def test_httpx_refuses_private_host(httpx_mock, monkeypatch):
    monkeypatch.setattr("websearch.layer2_extract.egress._resolve", lambda h: {"127.0.0.1"})
    res = HttpxFetcher().fetch(FetchRequest(url="https://internal.test/"))
    assert res.status == 0 and res.ok is False
    assert "refused" in (res.error or "")
    assert httpx_mock.get_requests() == []  # no request was issued


def test_httpx_allow_private_hosts_bypasses_guard(httpx_mock, monkeypatch):
    monkeypatch.setattr("websearch.layer2_extract.egress._resolve", lambda h: {"127.0.0.1"})
    httpx_mock.add_response(html="<html><body><p>internal</p></body></html>")
    res = HttpxFetcher().fetch(FetchRequest(url="https://internal.test/", allow_private_hosts=True))
    assert res.status == 200


def test_httpx_follows_redirect_with_guard(httpx_mock):
    httpx_mock.add_response(status_code=301, headers={"location": "https://final.test/page"})
    httpx_mock.add_response(html=ARTICLE_HTML)
    res = HttpxFetcher().fetch(FetchRequest(url="https://start.test/"))
    assert res.status == 200
    assert res.redirects == ["https://start.test/"]
    assert "Rust Ownership" in res.raw_html


def test_httpx_redirect_to_metadata_ip_is_blocked(httpx_mock):
    httpx_mock.add_response(
        status_code=302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
    )
    res = HttpxFetcher().fetch(FetchRequest(url="https://start.test/"))
    assert res.status == 0 and res.ok is False
    assert "refused" in (res.error or "")
    assert res.redirects == ["https://start.test/"]


def test_httpx_decodes_declared_latin1(httpx_mock):
    httpx_mock.add_response(
        content="Café résumé".encode("latin-1"),
        headers={"content-type": "text/html; charset=latin-1"},
    )
    res = HttpxFetcher().fetch(FetchRequest(url=URL))
    assert "Café résumé" in res.raw_html
