"""Escalation fetcher: curl_cffi with browser TLS/JA3 impersonation.

curl_cffi binds the curl-impersonate fork of libcurl (C), so it presents a real
browser TLS + HTTP/2 fingerprint and passes many Cloudflare/managed-challenge cases
that plain httpx fails. Because the I/O happens inside libcurl, this tier is NOT
interceptable by pytest-httpx; tests inject ``getter`` (a fake ``curl_cffi.get``)
exactly the way the ddgs adapter is faked at its library boundary.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..blocks import detect_block
from ..models import FetchRequest, FetchResult
from ..ports import FetchAdapter
from .util import DEFAULT_USER_AGENT, cap_body


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

        try:
            getter = self._resolve_getter()
        except ImportError as exc:
            return FetchResult(
                url=request.url,
                status=0,
                ok=False,
                fetched_via=self.fetched_via,
                error=f"curl_cffi not installed: {exc}",
                fetch_ms=elapsed(),
            )

        headers = dict(request.headers)
        if request.user_agent:
            headers.setdefault("User-Agent", request.user_agent)
        else:
            headers.setdefault("User-Agent", DEFAULT_USER_AGENT)

        kwargs: dict[str, Any] = {
            "impersonate": self._impersonate,
            "timeout": request.timeout_ms / 1000.0,
            "allow_redirects": True,
            "headers": headers,
        }
        if request.cookies:
            kwargs["cookies"] = {c.name: c.value for c in request.cookies}
        if request.proxy and request.proxy.type != "none":
            kwargs["proxies"] = {"http": request.proxy.url, "https": request.proxy.url}

        try:
            resp = getter(request.url, **kwargs)
        except Exception as exc:
            return FetchResult(
                url=request.url,
                status=0,
                ok=False,
                fetched_via=self.fetched_via,
                error=f"{type(exc).__name__}: {exc}",
                fetch_ms=elapsed(),
            )

        resp_headers = {str(k): str(v) for k, v in dict(resp.headers).items()}
        text = resp.text
        content = getattr(resp, "content", text.encode("utf-8", errors="replace"))
        body = cap_body(content, text, getattr(resp, "encoding", None), request.max_bytes)
        status = int(resp.status_code)
        blocked, reason = detect_block(status, body, resp_headers)
        return FetchResult(
            url=request.url,
            final_url=str(getattr(resp, "url", request.url)),
            status=status,
            ok=status < 400,
            fetched_via=self.fetched_via,
            raw_html=body,
            content_type=resp_headers.get("content-type"),
            headers=resp_headers,
            blocked=blocked,
            block_reason=reason,
            fetch_ms=elapsed(),
        )
