"""Layer 1: multi-engine search aggregation.

A thin router fans a normalized SearchRequest out to isolated per-engine adapters,
then canonicalizes, dedups, and fuses (provenance-aware weighted RRF). The public
surface is ``SearchRouter`` plus the ``build_router`` factory; everything else is an
adapter behind the ``EngineAdapter`` port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .adapters import DdgsAdapter, SearxngAdapter
from .models import (
    SEARCH_CONTRACT_VERSION,
    Fusion,
    ResultItem,
    SearchPayload,
    SearchRequest,
    SourceProvenance,
)
from .port import EngineAdapter, EngineOutput, RawResult
from .router import SearchRouter


def build_router(
    *,
    searxng_url: str | None = None,
    searxng_engines: list[str] | None = None,
    enable_ddgs: bool = True,
    ddgs_factory: Callable[[], Any] | None = None,
    extra_adapters: list[EngineAdapter] | None = None,
) -> SearchRouter:
    """Assemble a SearchRouter from the available backends.

    SearXNG is included only when a base URL is given; ddgs is the keyless default.
    ``extra_adapters`` lets a caller plug in keyed/decorrelated engines.
    """
    adapters: list[EngineAdapter] = []
    if searxng_url:
        adapters.append(SearxngAdapter(searxng_url, engines=searxng_engines))
    if enable_ddgs:
        adapters.append(DdgsAdapter(ddgs_factory=ddgs_factory))
    if extra_adapters:
        adapters.extend(extra_adapters)
    return SearchRouter(adapters)


__all__ = [
    "SEARCH_CONTRACT_VERSION",
    "SearchRequest",
    "SearchPayload",
    "ResultItem",
    "SourceProvenance",
    "Fusion",
    "SearchRouter",
    "EngineAdapter",
    "EngineOutput",
    "RawResult",
    "SearxngAdapter",
    "DdgsAdapter",
    "build_router",
]
