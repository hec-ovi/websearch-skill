"""Escalation fetcher: curl_cffi with browser TLS/JA3 impersonation.

curl_cffi binds the curl-impersonate fork of libcurl (C), so it presents a real
browser fingerprint and passes many Cloudflare/managed-challenge cases plain httpx
fails. The I/O happens inside libcurl, so this tier is NOT interceptable by
pytest-httpx; tests inject ``getter`` (a fake ``curl_cffi.get``) at the library
boundary, the way the ddgs adapter is faked.

It applies the same SSRF egress guard as the httpx tier (an http(s) scheme allowlist
plus a private/reserved address check before each request) and follows redirects
manually so libcurl never auto-follows into the internal network or a non-http scheme.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin

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


class CurlCffiFetcher(FetchAdapter):
    name = "curl_cffi"
    fetched_via = "curl_cffi"
    tier_class = "http"
    escalation_order = 1

    def __init__(self, getter: Callable[..., Any] | None = None, impersonate: str = "chrome"):
        # getter mirrors curl_cffi.get(url, **kwargs) -> Response; injected in tests.
        self._getter = getter
        self._impersonate = impersonate

    def available(self) -> bool:
        if self._getter is not None:
            return True
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            return False
        return True

    def _resolve_getter(self) -> Callable[..., Any]:
        if self._getter is not None:
            return self._getter
        import curl_cffi

        return curl_cffi.get

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

        try:
            getter = self._resolve_getter()
        except ImportError as exc:
            return fail(f"curl_cffi not installed: {exc}", None, [], kind="dependency_missing")

        headers = dict(request.headers)
        headers.setdefault("User-Agent", request.user_agent or DEFAULT_USER_AGENT)
        kwargs: dict[str, Any] = {
            "impersonate": self._impersonate,
            "timeout": request.timeout_ms / 1000.0,
            "allow_redirects": False,
            "headers": headers,
        }
        if request.cookies:
            kwargs["cookies"] = {c.name: c.value for c in request.cookies}
        if request.proxy and request.proxy.type != "none":
            kwargs["proxies"] = {"http": request.proxy.url, "https": request.proxy.url}

        redirects: list[str] = []
        current = request.url
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
            try:
                resp = getter(current, **kwargs)
            except Exception as exc:
                return fail(f"{type(exc).__name__}: {exc}", current, redirects)
            # curl_cffi (libcurl) materializes and decodes the body lazily, so a mid-body
            # connection reset or a charset error surfaces on .headers/.status_code/.text
            # here, NOT on the getter() call above. Guard the whole response-processing
            # block (mirroring the httpx tier) so such a failure becomes a status==0 result
            # the router can escalate, never an uncaught traceback out of the pipeline.
            try:
                resp_headers = {str(k): str(v) for k, v in dict(resp.headers).items()}
                status = int(resp.status_code)
                location = _header(resp_headers, "location")
                if status in _REDIRECT_STATUS and location:
                    redirects.append(current)
                    current = urljoin(current, location)
                    continue

                text = resp.text
                content = getattr(resp, "content", text.encode("utf-8", errors="replace"))
                body = read_body(content, _header(resp_headers, "content-type"), request.max_bytes)
                blocked, reason = detect_block(status, body, resp_headers)
                return FetchResult(
                    url=request.url,
                    final_url=str(getattr(resp, "url", current)),
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
            except Exception as exc:
                return fail(f"{type(exc).__name__}: {exc}", current, redirects)
        return fail("too many redirects", current, redirects, kind="redirect_loop")
