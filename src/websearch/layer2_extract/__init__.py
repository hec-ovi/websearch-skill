"""Layer 2A: FETCH (tiered) + EXTRACT (Trafilatura default), two decoupled sub-ports.

``build_pipeline`` wires the default closure: HttpxFetcher -> CurlCffiFetcher behind a
``FetchRouter``, feeding a ``TrafilaturaExtractor``. Both sub-ports are swappable via
``extra_fetchers`` / ``extractor`` and the injectable ``curl_cffi_getter`` (used by the
tests to fake the libcurl boundary).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .exceptions import DependencyMissing
from .extractors.trafilatura_extractor import TrafilaturaExtractor
from .fetch_router import FetchRouter
from .fetchers.curl_cffi_fetcher import CurlCffiFetcher
from .fetchers.httpx_fetcher import HttpxFetcher
from .models import (
    EXTRACT_CONTRACT_VERSION,
    FETCH_CONTRACT_VERSION,
    Cookie,
    ExtractPayload,
    ExtractRequest,
    ExtractResult,
    ExtractSource,
    FetchRequest,
    FetchResult,
    Proxy,
)
from .pipeline import FetchExtractPipeline
from .ports import ExtractAdapter, FetchAdapter

__all__ = [
    "FETCH_CONTRACT_VERSION",
    "EXTRACT_CONTRACT_VERSION",
    "FetchRequest",
    "FetchResult",
    "ExtractRequest",
    "ExtractResult",
    "ExtractSource",
    "ExtractPayload",
    "Cookie",
    "Proxy",
    "FetchAdapter",
    "ExtractAdapter",
    "FetchRouter",
    "FetchExtractPipeline",
    "HttpxFetcher",
    "CurlCffiFetcher",
    "TrafilaturaExtractor",
    "DependencyMissing",
    "build_fetch_router",
    "build_pipeline",
]


def build_fetch_router(
    *,
    enable_curl_cffi: bool = True,
    curl_cffi_getter: Callable[..., Any] | None = None,
    impersonate: str = "chrome",
    extra_fetchers: list[FetchAdapter] | None = None,
) -> FetchRouter:
    fetchers: list[FetchAdapter] = [HttpxFetcher()]
    if enable_curl_cffi:
        fetchers.append(CurlCffiFetcher(getter=curl_cffi_getter, impersonate=impersonate))
    if extra_fetchers:
        fetchers.extend(extra_fetchers)
    return FetchRouter(fetchers)


def build_pipeline(
    *,
    enable_curl_cffi: bool = True,
    curl_cffi_getter: Callable[..., Any] | None = None,
    impersonate: str = "chrome",
    extractor: ExtractAdapter | None = None,
    extra_fetchers: list[FetchAdapter] | None = None,
) -> FetchExtractPipeline:
    router = build_fetch_router(
        enable_curl_cffi=enable_curl_cffi,
        curl_cffi_getter=curl_cffi_getter,
        impersonate=impersonate,
        extra_fetchers=extra_fetchers,
    )
    return FetchExtractPipeline(router, extractor or TrafilaturaExtractor())
