"""Escalation fetcher: curl_cffi with browser TLS/JA3 impersonation.

curl_cffi binds the curl-impersonate fork of libcurl (C), so it presents a real
browser fingerprint and passes many Cloudflare/managed-challenge cases plain httpx
fails. The I/O happens inside libcurl, so this tier is NOT interceptable by
pytest-httpx; tests inject ``getter`` (a fake ``curl_cffi.get``) at the library
boundary, the way the ddgs adapter is faked.

It applies the same SSRF egress guard as the httpx tier (an http(s) scheme allowlist
plus a private/reserved address check before each request) and follows redirects
manually so libcurl never auto-follows into the internal network or a non-http scheme.
The real path streams through one reused ``curl_cffi.Session`` so ``max_bytes`` stops
the download mid-body, mirroring the httpx tier's capped read.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..blocks import detect_block
from ..egress import BlockedEgress, guard_url
from ..models import FetchRequest, FetchResult
from ..ports import FetchAdapter
from .util import (
    DEFAULT_USER_AGENT,
    MAX_REDIRECTS,
    REDIRECT_STATUS,
    header,
    hop_cookies,
    hop_headers,
    read_body,
)


def _close(resp: Any) -> None:
    close = getattr(resp, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _read_capped(resp: Any, max_bytes: int | None) -> bytes:
    """Read the body, stopping the download once ``max_bytes`` is reached.

    A streamed response (the real Session path) is consumed chunk by chunk so a
    multi-GB body is never transferred past the guard. A buffered response (an
    injected test fake) falls back to ``content``; ``text`` is encoded only when
    ``content`` is absent (getattr's default would eagerly encode on every call).
    """
    iter_content = getattr(resp, "iter_content", None)
    if callable(iter_content):
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in iter_content():
                chunks.append(chunk)
                total += len(chunk)
                if max_bytes is not None and total >= max_bytes:
                    break
        finally:
            _close(resp)
        return b"".join(chunks)
    content = getattr(resp, "content", None)
    if content is None:
        content = resp.text.encode("utf-8", errors="replace")
    return content


class CurlCffiFetcher(FetchAdapter):
    name = "curl_cffi"
    fetched_via = "curl_cffi"
    tier_class = "http"
    escalation_order = 1

    def __init__(self, getter: Callable[..., Any] | None = None, impersonate: str = "chrome"):
        # getter mirrors curl_cffi.get(url, **kwargs) -> Response; injected in tests.
        self._getter = getter
        self._impersonate = impersonate
        self._session: Any = None  # lazily-built curl_cffi.Session, reused across fetches

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

        if self._session is None:
            self._session = curl_cffi.Session()
        session = self._session

        def _get(url: str, **kwargs: Any) -> Any:
            # stream=True lets _read_capped stop the transfer at max_bytes;
            # discard_cookies keeps the reused session stateless (hop cookies are
            # scoped explicitly per request, never accumulated across fetches).
            return session.get(url, stream=True, discard_cookies=True, **kwargs)

        return _get

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
        origin_host = (urlsplit(request.url).hostname or "").lower()
        base_kwargs: dict[str, Any] = {
            "impersonate": self._impersonate,
            "timeout": request.timeout_ms / 1000.0,
            "allow_redirects": False,
        }
        proxied = bool(request.proxy and request.proxy.type != "none")
        if proxied:
            base_kwargs["proxies"] = {"http": request.proxy.url, "https": request.proxy.url}

        redirects: list[str] = []
        current = request.url
        for _hop in range(MAX_REDIRECTS + 1):
            try:
                guard_url(
                    current,
                    allow_private=request.allow_private_hosts,
                    proxied=proxied,
                    tor=bool(request.proxy and request.proxy.tor),
                )
            except BlockedEgress as exc:
                return fail(
                    exc.reason,
                    current if current != request.url else None,
                    redirects,
                    kind="egress_refused",
                )
            # Per-hop credential scoping: a cross-origin redirect never receives the
            # Authorization header or caller cookies (Cookie.domain widens a cookie to
            # matching hosts only).
            host = urlsplit(current).hostname
            kwargs = dict(base_kwargs)
            kwargs["headers"] = hop_headers(headers, origin_host, host)
            jar = hop_cookies(request.cookies, origin_host, host)
            if jar:
                kwargs["cookies"] = jar
            try:
                resp = getter(current, **kwargs)
            except Exception as exc:
                return fail(f"{type(exc).__name__}: {exc}", current, redirects)
            # curl_cffi (libcurl) materializes and decodes the body lazily, so a mid-body
            # connection reset or a charset error surfaces on .headers/.status_code/the
            # body read here, NOT on the getter() call above. Guard the whole
            # response-processing block (mirroring the httpx tier) so such a failure
            # becomes a status==0 result the router can escalate, never an uncaught
            # traceback out of the pipeline.
            try:
                resp_headers = {str(k): str(v) for k, v in dict(resp.headers).items()}
                status = int(resp.status_code)
                location = header(resp_headers, "location")
                if status in REDIRECT_STATUS and location:
                    _close(resp)
                    redirects.append(current)
                    current = urljoin(current, location)
                    continue

                content = _read_capped(resp, request.max_bytes)
                body = read_body(content, header(resp_headers, "content-type"), request.max_bytes)
                blocked, reason = detect_block(status, body, resp_headers)
                return FetchResult(
                    url=request.url,
                    final_url=str(getattr(resp, "url", current)),
                    status=status,
                    ok=status < 400,
                    fetched_via=self.fetched_via,
                    raw_html=body,
                    content_type=header(resp_headers, "content-type"),
                    redirects=redirects,
                    headers=resp_headers,
                    blocked=blocked,
                    block_reason=reason,
                    fetch_ms=elapsed(),
                )
            except Exception as exc:
                _close(resp)
                return fail(f"{type(exc).__name__}: {exc}", current, redirects)
        return fail("too many redirects", current, redirects, kind="redirect_loop")
