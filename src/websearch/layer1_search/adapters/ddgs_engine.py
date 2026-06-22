"""ddgs adapter (the zero-config keyless fallback).

``ddgs`` (the maintained successor to duckduckgo-search) is itself multi-backend; we
treat it as one general-aggregator engine. It uses a Rust HTTP client (primp)
internally, so tests inject a fake DDGS via ``ddgs_factory`` rather than mocking HTTP.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..capability import GENERAL_AGGREGATOR
from ..models import FreshnessRange, SearchRequest
from ..port import EngineAdapter, EngineOutput, RawResult

_SAFE = {"off": "off", "moderate": "moderate", "strict": "on"}
_FRESH = {"day": "d", "week": "w", "month": "m", "year": "y"}


class DdgsAdapter(EngineAdapter):
    name = "ddgs"
    correlation_group = GENERAL_AGGREGATOR

    def __init__(self, ddgs_factory: Callable[[], Any] | None = None, *, backend: str = "auto"):
        # ddgs_factory lets tests inject a fake DDGS (the external service boundary).
        self._factory = ddgs_factory
        self._backend = backend

    def _make_client(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from ddgs import DDGS

        return DDGS()

    def _region(self, request: SearchRequest) -> str | None:
        if request.country:
            # ddgs needs a two-part cc-ll region; take the language's primary subtag
            # so a BCP-47 tag like "en-GB" does not produce an invalid "us-en-gb".
            lang = (request.language or "en").lower().split("-")[0]
            return f"{request.country.lower()}-{lang}"
        return None  # let ddgs use its default region

    def search(self, request: SearchRequest) -> EngineOutput:
        kwargs: dict[str, Any] = {
            "max_results": request.count,
            "safesearch": _SAFE[request.safesearch],
            "backend": self._backend,
        }
        region = self._region(request)
        if region:
            kwargs["region"] = region
        if not isinstance(request.freshness, FreshnessRange) and request.freshness != "any":
            kwargs["timelimit"] = _FRESH[request.freshness]
        # Translate offset into ddgs's native 1-based page param (mirrors the searxng
        # adapter). Without this, offset is silently ignored and paging returns page-1
        # results again. ddgs.text accepts page via **kwargs.
        if request.offset:
            kwargs["page"] = (request.offset // max(request.count, 1)) + 1

        try:
            client = self._make_client()
            rows = client.text(request.query, **kwargs)
        except Exception as exc:
            return EngineOutput(engine=self.name, error=f"{type(exc).__name__}: {exc}")

        results: list[RawResult] = []
        for i, row in enumerate(rows or []):
            url = row.get("href") or row.get("url") or ""
            if not url:
                continue
            results.append(
                RawResult(
                    url=url,
                    title=row.get("title") or "",
                    snippet=row.get("body") or "",
                    rank=i + 1,
                    result_type="news" if request.result_type == "news" else "web",
                )
            )
        return EngineOutput(engine=self.name, results=results)
