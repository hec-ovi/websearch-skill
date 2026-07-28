"""SearXNG adapter.

SearXNG is the optional self-hosted engine: a private metasearch instance with JSON
output enabled. This adapter is near-passthrough because the port's optional fields
align with the SearXNG result shape; it maps content -> snippet, score -> raw_score,
and so on. Point it at any instance via the base URL.
"""

from __future__ import annotations

from typing import Any

import httpx

from ...proxy import proxy_for
from ..capability import GENERAL_AGGREGATOR, ONION_INDEX
from ..models import FreshnessRange, SearchRequest
from ..port import EngineAdapter, EngineOutput, RawResult

# The SearXNG category that holds its onion engines (ahmia, torch). Selecting it is what
# turns a clearnet instance into an onion index; the instance still has to be configured
# to reach Tor, which `websearch searxng up` does when the tor layer is on.
ONIONS_CATEGORY = "onions"
ONION_ENGINE_NAME = "searxng-onions"

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
        proxy: str | None = None,
        category: str | None = None,
        name: str | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self._engines = engines
        self._category = category
        # An onion instance and a clearnet one are the same code against the same URL with
        # a different category, but they are two entries in the fanout and two lines of
        # provenance, so the name is per instance rather than per class.
        if name:
            self.name = name
        if category == ONIONS_CATEGORY:
            self.correlation_group = ONION_INDEX
        self._client = client
        self._owned_client: httpx.Client | None = None
        # A self-hosted SearXNG usually lives on loopback or the LAN, where an egress
        # proxy cannot reach it: the exit node would try to connect to its own
        # localhost. The proxy still applies to a SearXNG on a public address, and
        # either way the engine traffic that leaves the instance is SearXNG's own
        # outgoing config, not this hop.
        self._proxy = proxy_for(self.base_url, proxy)

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
        if self._category:
            params["categories"] = self._category
        elif request.result_type == "news":
            params["categories"] = "news"
        override = request.engine_overrides.get(self.name, {}) or {}
        # A request-scoped override wins over the adapter's static engine list.
        engines = override.get("engines") or self._engines
        if engines:
            params["engines"] = ",".join(engines) if isinstance(engines, list) else str(engines)
        return params

    def _parse_results(self, payload: dict, request: SearchRequest) -> list[RawResult]:
        results: list[RawResult] = []
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            return results
        for i, item in enumerate(raw_results):
            if not isinstance(item, dict):
                continue  # tolerate a malformed entry without crashing the engine
            url = item.get("url") or ""
            if not url:
                continue
            is_news = item.get("category") == "news" or request.result_type == "news"
            score = item.get("score")
            published = item.get("publishedDate")
            results.append(
                RawResult(
                    url=url,
                    title=item.get("title") or "",
                    snippet=item.get("content") or "",
                    rank=i + 1,
                    raw_score=float(score) if isinstance(score, (int, float)) else None,
                    published_date=str(published) if published is not None else None,
                    result_type="news" if is_news else "web",
                    thumbnail=item.get("thumbnail") or item.get("img_src") or None,
                )
            )
        return results

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        # Cache the owned client so a long-lived adapter (MCP server) reuses connections
        # instead of paying a TCP+TLS handshake per search.
        if self._owned_client is None:
            self._owned_client = httpx.Client(
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
                proxy=self._proxy,
            )
        return self._owned_client

    def search(self, request: SearchRequest) -> EngineOutput:
        if not self.enabled():
            return EngineOutput(engine=self.name, error="searxng base_url not configured")

        client = self._get_client()
        try:
            # Honor the request's per-engine timeout when we own the client; an injected
            # client owns its transport and keeps its own timeout.
            kwargs: dict[str, Any] = {"params": self._params(request)}
            if self._client is None:
                kwargs["timeout"] = request.timeout_ms / 1000.0
            resp = client.get(f"{self.base_url}/search", **kwargs)
            resp.raise_for_status()
            payload = resp.json()
            if not isinstance(payload, dict):
                raise ValueError("searxng returned a non-object JSON body")
            return EngineOutput(
                engine=self.name,
                results=self._parse_results(payload, request),
                answers=_as_str_list(payload.get("answers")),
                suggestions=_as_str_list(payload.get("suggestions")),
                corrections=_as_str_list(payload.get("corrections")),
            )
        except Exception as exc:
            return EngineOutput(engine=self.name, error=f"{type(exc).__name__}: {exc}")
