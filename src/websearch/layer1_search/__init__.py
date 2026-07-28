"""Layer 1: multi-engine search aggregation.

A thin router fans a normalized SearchRequest out to isolated per-engine adapters,
then canonicalizes, dedups, and fuses (provenance-aware weighted RRF). The public
surface is ``SearchRouter`` plus the ``build_router`` factory; everything else is an
adapter behind the ``EngineAdapter`` port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .adapters import AhmiaAdapter, DdgsAdapter, SearxngAdapter
from .adapters.searxng import ONION_ENGINE_NAME, ONIONS_CATEGORY
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
    ddgs_backend: str = "auto",
    extra_adapters: list[EngineAdapter] | None = None,
    proxy: str | None = None,
    onion: bool = False,
) -> SearchRouter:
    """Assemble a SearchRouter from the available backends.

    SearXNG is included only when a base URL is given; ddgs is the keyless default.
    ``ddgs_backend`` selects which underlying keyless engines ddgs queries (a
    comma-separated list like "google,brave,mojeek", or "auto" for all). Unknown names
    are ignored by ddgs. ``extra_adapters`` lets a caller plug in keyed/decorrelated
    engines. ``proxy`` routes both built-in engines' egress through one proxy URL
    (extra adapters own their transport and are unaffected).

    ``onion`` swaps the clearnet fanout for the onion one (Ahmia, plus SearXNG's onions
    category when an instance is configured). It replaces rather than extends: ddgs
    cannot see an onion service and Ahmia does not index the clearnet, so running both
    would produce one half-empty list per engine and a fusion over two disjoint corpora.
    """
    adapters: list[EngineAdapter] = []
    if onion:
        adapters.append(AhmiaAdapter(proxy=proxy))
        if searxng_url:
            adapters.append(
                SearxngAdapter(
                    searxng_url,
                    proxy=proxy,
                    category=ONIONS_CATEGORY,
                    name=ONION_ENGINE_NAME,
                )
            )
        if extra_adapters:
            adapters.extend(extra_adapters)
        return SearchRouter(adapters)
    if searxng_url:
        adapters.append(SearxngAdapter(searxng_url, engines=searxng_engines, proxy=proxy))
    if enable_ddgs:
        adapters.append(DdgsAdapter(ddgs_factory=ddgs_factory, backend=ddgs_backend, proxy=proxy))
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
    "AhmiaAdapter",
    "ONIONS_CATEGORY",
    "ONION_ENGINE_NAME",
    "build_router",
]
