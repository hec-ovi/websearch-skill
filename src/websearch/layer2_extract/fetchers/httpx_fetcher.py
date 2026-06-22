"""Tier 0 fetcher: plain httpx.

The cheapest tier and the absolute first attempt. It uses the stdlib-ish httpx
client (already a project dependency), so it is interceptable by pytest-httpx at the
network layer. When it is blocked, the router escalates to the curl_cffi tier (browser
TLS impersonation), then to the opt-in browser/stealth tiers.
"""

from __future__ import annotations

import time

import httpx

from ..blocks import detect_block
from ..models import FetchRequest, FetchResult
from ..ports import FetchAdapter
from .util import DEFAULT_USER_AGENT, cap_body


class HttpxFetcher(FetchAdapter):
    name = "httpx"
    fetched_via = "http"
    tier_class = "http"
    escalation_order = 0

    def fetch(self, request: FetchRequest) -> FetchResult:
        t0 = time.perf_counter()
        headers = dict(request.headers)
        headers.setdefault("User-Agent", request.user_agent or DEFAULT_USER_AGENT)
        cookies = {c.name: c.value for c in request.cookies}
        proxy = request.proxy.url if (request.proxy and request.proxy.type != "none") else None
        timeout = request.timeout_ms / 1000.0

        def elapsed() -> int:
            return int((time.perf_counter() - t0) * 1000)

        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                proxy=proxy,
                headers=headers,
                cookies=cookies,
            ) as client:
                resp = client.get(request.url)
        except Exception as exc:  # transport failure: no response at all
            return FetchResult(
                url=request.url,
                status=0,
                ok=False,
                fetched_via=self.fetched_via,
                error=f"{type(exc).__name__}: {exc}",
                fetch_ms=elapsed(),
            )

        body = cap_body(resp.content, resp.text, resp.encoding, request.max_bytes)
        resp_headers = {k: v for k, v in resp.headers.items()}
        blocked, reason = detect_block(resp.status_code, body, resp_headers)
        return FetchResult(
            url=request.url,
            final_url=str(resp.url),
            status=resp.status_code,
            ok=resp.status_code < 400,
            fetched_via=self.fetched_via,
            raw_html=body,
            content_type=resp.headers.get("content-type"),
            redirects=[str(r.url) for r in resp.history],
            headers=resp_headers,
            blocked=blocked,
            block_reason=reason,
            fetch_ms=elapsed(),
        )
