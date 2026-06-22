"""Tier 0 fetcher: plain httpx.

The cheapest tier and the first attempt. Interceptable by pytest-httpx (httpx
transport). It runs the SSRF egress guard before the first request and on every
redirect hop (redirects are followed manually, not automatically, so a public URL
cannot 30x into the internal network), streams the body with a byte cap, and decodes
with charset detection rather than a blind UTF-8 fallback.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

import httpx

from ..blocks import detect_block
from ..egress import BlockedEgress, guard_url
from ..models import FetchRequest, FetchResult
from ..ports import FetchAdapter
from .util import DEFAULT_USER_AGENT, read_body

_MAX_REDIRECTS = 10
_REDIRECT_STATUS = (301, 302, 303, 307, 308)


def _header(headers: dict[str, str], name: str) -> str | None:
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None


class HttpxFetcher(FetchAdapter):
    name = "httpx"
    fetched_via = "http"
    tier_class = "http"
    escalation_order = 0

    def fetch(self, request: FetchRequest) -> FetchResult:
        t0 = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - t0) * 1000)

        def fail(
            reason: str,
            final_url: str | None,
            redirects: list[str],
            kind: str = "transport_error",
        ) -> FetchResult:
            return FetchResult(
                url=request.url,
                final_url=final_url,
                status=0,
                ok=False,
                fetched_via=self.fetched_via,
                redirects=redirects,
                error=reason,
                failure_kind=kind,  # type: ignore[arg-type]
                fetch_ms=elapsed(),
            )

        headers = dict(request.headers)
        headers.setdefault("User-Agent", request.user_agent or DEFAULT_USER_AGENT)
        cookies = {c.name: c.value for c in request.cookies}
        proxy = request.proxy.url if (request.proxy and request.proxy.type != "none") else None
        timeout = request.timeout_ms / 1000.0

        redirects: list[str] = []
        current = request.url
        try:
            with httpx.Client(
                follow_redirects=False,
                timeout=timeout,
                proxy=proxy,
                headers=headers,
                cookies=cookies,
            ) as client:
                for _hop in range(_MAX_REDIRECTS + 1):
                    try:
                        guard_url(current, allow_private=request.allow_private_hosts)
                    except BlockedEgress as exc:
                        return fail(
                            exc.reason,
                            current if current != request.url else None,
                            redirects,
                            kind="egress_refused",
                        )

                    content, status, resp_headers, final_url = self._get(
                        client, current, request.max_bytes
                    )

                    location = _header(resp_headers, "location")
                    if status in _REDIRECT_STATUS and location:
                        redirects.append(current)
                        current = urljoin(current, location)
                        continue

                    body = read_body(
                        content, _header(resp_headers, "content-type"), request.max_bytes
                    )
                    blocked, reason = detect_block(status, body, resp_headers)
                    return FetchResult(
                        url=request.url,
                        final_url=final_url,
                        status=status,
                        ok=status < 400,
                        fetched_via=self.fetched_via,
                        raw_html=body,
                        content_type=_header(resp_headers, "content-type"),
                        redirects=redirects,
                        headers=resp_headers,
                        blocked=blocked,
                        block_reason=reason,
                        fetch_ms=elapsed(),
                    )
                return fail("too many redirects", current, redirects, kind="redirect_loop")
        except httpx.TimeoutException as exc:
            return fail(f"{type(exc).__name__}: {exc}", None, redirects, kind="timeout")
        except Exception as exc:  # transport failure: no usable response
            return fail(f"{type(exc).__name__}: {exc}", None, redirects, kind="transport_error")

    def _get(
        self, client: httpx.Client, url: str, max_bytes: int | None
    ) -> tuple[bytes, int, dict[str, str], str]:
        with client.stream("GET", url) as resp:
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if max_bytes is not None and total >= max_bytes:
                    break
            return b"".join(chunks), resp.status_code, dict(resp.headers), str(resp.url)
