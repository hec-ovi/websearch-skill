"""FetchRouter escalation policy, exercised with fake fetchers (no network)."""

from __future__ import annotations

from websearch.layer2_extract.fetch_router import FetchRouter
from websearch.layer2_extract.models import FetchRequest, FetchResult
from websearch.layer2_extract.ports import FetchAdapter


class FakeFetcher(FetchAdapter):
    def __init__(
        self,
        name: str,
        *,
        tier_class: str = "http",
        order: int = 0,
        available: bool = True,
        fetched_via: str = "http",
        **result_kwargs,
    ):
        self.name = name
        self.fetched_via = fetched_via
        self.tier_class = tier_class
        self.escalation_order = order
        self._available = available
        self._result_kwargs = result_kwargs or {
            "status": 200,
            "ok": True,
            "raw_html": "<p>ok</p>",
        }
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def fetch(self, request: FetchRequest) -> FetchResult:
        self.calls += 1
        return FetchResult(url=request.url, fetched_via=self.fetched_via, **self._result_kwargs)


def _http(name, order, **kw):
    return FakeFetcher(name, tier_class="http", order=order, fetched_via="http", **kw)


def test_clean_success_does_not_escalate():
    httpx = _http("httpx", 0, status=200, ok=True, raw_html="<p>ok</p>")
    curl = FakeFetcher("curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True)
    res = FetchRouter([httpx, curl]).fetch(FetchRequest(url="https://x.test/"))
    assert res.fetched_via == "http"
    assert res.tier_attempts == ["httpx"]
    assert curl.calls == 0


def test_escalates_on_cloudflare_block():
    httpx = _http(
        "httpx",
        0,
        status=403,
        ok=False,
        blocked=True,
        block_reason="cloudflare_challenge",
        raw_html="<title>Just a moment...</title>",
    )
    curl = FakeFetcher(
        "curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True, raw_html="<p>real</p>"
    )
    res = FetchRouter([httpx, curl]).fetch(FetchRequest(url="https://x.test/"))
    assert res.fetched_via == "curl_cffi"
    assert res.ok is True
    assert res.tier_attempts == ["httpx", "curl_cffi"]
    assert httpx.calls == 1 and curl.calls == 1


def test_terminal_block_does_not_escalate():
    httpx = _http("httpx", 0, status=429, ok=False, blocked=True, block_reason="rate_limited")
    curl = FakeFetcher("curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True)
    res = FetchRouter([httpx, curl]).fetch(FetchRequest(url="https://x.test/"))
    assert res.block_reason == "rate_limited"
    assert curl.calls == 0


def test_genuine_404_does_not_escalate():
    httpx = _http("httpx", 0, status=404, ok=False, blocked=False, raw_html="<p>nope</p>")
    curl = FakeFetcher("curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True)
    res = FetchRouter([httpx, curl]).fetch(FetchRequest(url="https://x.test/"))
    assert res.status == 404
    assert curl.calls == 0


def test_transport_error_escalates():
    httpx = _http("httpx", 0, status=0, ok=False, error="ConnectError")
    curl = FakeFetcher(
        "curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True, raw_html="<p>ok</p>"
    )
    res = FetchRouter([httpx, curl]).fetch(FetchRequest(url="https://x.test/"))
    assert res.fetched_via == "curl_cffi"
    assert res.ok is True


def test_tier_hint_http_excludes_browser_tier():
    httpx = _http("httpx", 0, status=200, ok=True, raw_html="<p>ok</p>")
    browser = FakeFetcher("browser", tier_class="browser", order=2, fetched_via="browser")
    router = FetchRouter([httpx, browser])
    res = router.fetch(FetchRequest(url="https://x.test/", tier_hint="http"))
    assert res.fetched_via == "http"
    assert browser.calls == 0


def test_browser_tier_unavailable_is_clear_failure():
    httpx = _http("httpx", 0, status=200, ok=True)
    res = FetchRouter([httpx]).fetch(FetchRequest(url="https://x.test/", tier_hint="browser"))
    assert res.status == 0
    assert res.ok is False
    assert "opt-in" in (res.error or "")


def test_render_js_requires_browser_tier():
    httpx = _http("httpx", 0, status=200, ok=True)
    res = FetchRouter([httpx]).fetch(FetchRequest(url="https://x.test/", render_js=True))
    assert res.ok is False
    assert "not installed" in (res.error or "")


def test_unavailable_fetcher_is_skipped():
    dead = _http("dead", 0, available=False)
    live = FakeFetcher("curl_cffi", order=1, fetched_via="curl_cffi", status=200, ok=True)
    res = FetchRouter([dead, live]).fetch(FetchRequest(url="https://x.test/"))
    assert res.fetched_via == "curl_cffi"
    assert dead.calls == 0
