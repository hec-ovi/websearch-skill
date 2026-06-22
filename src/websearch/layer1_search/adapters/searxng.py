"""SearXNG adapter.

SearXNG is the keyless default backbone: a private metasearch instance with JSON
output enabled. This adapter is near-passthrough because the port's optional fields
align with the SearXNG result shape; it maps content -> snippet, score -> raw_score,
and so on. Point it at any instance via the base URL.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..capability import GENERAL_AGGREGATOR
from ..models import FreshnessRange, SearchRequest
from ..port import EngineAdapter, EngineOutput, RawResult

_SAFE = {"off": 0, "moderate": 1, "strict": 2}
_FRESH = {"day": "day", "week": "week", "month": "month", "year": "year"}

_USER_AGENT = "websearch-skill/0.1 (+https://github.com/hec-ovi/websearch-skill)"


def _as_str_list(value: Any) -> list[str]:
    out: list[str] = []
    for item in value or []:
        if isinstance(item, str):
            if item:
                out.append(item)
        elif isinstance(item, dict):
            s = item.get("answer") or item.get("title") or item.get("content")
            if s:
                out.append(str(s))
    return out


class SearxngAdapter(EngineAdapter):
    name = "searxng"
    correlation_group = GENERAL_AGGREGATOR

    def __init__(
        self,
        base_url: str | None,
        *,
        engines: list[str] | None = None,
        client: httpx.Client | None = None,
        timeout_s: float = 8.0,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._engines = engines
        self._client = client
        self._timeout_s = timeout_s

    def enabled(self) -> bool:
        return bool(self.base_url)

    def _params(self, request: SearchRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": request.query,
            "format": "json",
            "safesearch": _SAFE[request.safesearch],
            "pageno": (request.offset // max(request.count, 1)) + 1,
        }
        if not isinstance(request.freshness, FreshnessRange) and request.freshness != "any":
            params["time_range"] = _FRESH[request.freshness]
        if request.language:
            params["language"] = request.language
        if request.result_type == "news":
            params["categories"] = "news"
        override = request.engine_overrides.get("searxng", {}) or {}
        engines = self._engines or override.get("engines")
        if engines:
            params["engines"] = ",".join(engines) if isinstance(engines, list) else str(engines)
        return params

    def search(self, request: SearchRequest) -> EngineOutput:
        if not self.enabled():
            return EngineOutput(engine=self.name, error="searxng base_url not configured")

        client = self._client or httpx.Client(
            timeout=self._timeout_s, follow_redirects=True, headers={"User-Agent": _USER_AGENT}
        )
        owns_client = self._client is None
        try:
            resp = client.get(f"{self.base_url}/search", params=self._params(request))
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            return EngineOutput(engine=self.name, error=f"{type(exc).__name__}: {exc}")
        finally:
            if owns_client:
                client.close()

        results: list[RawResult] = []
        for i, item in enumerate(payload.get("results", []) or []):
            url = item.get("url") or ""
            if not url:
                continue
            is_news = item.get("category") == "news" or request.result_type == "news"
            results.append(
                RawResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("content") or "",
                    rank=i + 1,
                    raw_score=item.get("score"),
                    published_date=item.get("publishedDate"),
                    result_type="news" if is_news else "web",
                    thumbnail=item.get("thumbnail") or item.get("img_src"),
                )
            )

        return EngineOutput(
            engine=self.name,
            results=results,
            answers=_as_str_list(payload.get("answers")),
            suggestions=_as_str_list(payload.get("suggestions")),
            corrections=_as_str_list(payload.get("corrections")),
        )
