"""The Layer-1 SEARCH port: the abstract interface every engine adapter implements.

The router depends only on ``EngineAdapter`` and the dataclasses below, never on a
concrete backend. Adding or swapping an engine touches only its adapter module plus
the capability map.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .models import SearchRequest


@dataclass
class RawResult:
    """One result from a single engine, before canonicalization and fusion."""

    url: str
    title: str
    snippet: str
    rank: int  # 1-based position within this engine's own list
    raw_score: float | None = None
    native_id: str | None = None
    published_date: str | None = None
    result_type: str = "web"
    favicon: str | None = None
    thumbnail: str | None = None


@dataclass
class EngineOutput:
    """What an adapter returns to the router for one request."""

    engine: str
    results: list[RawResult] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    # None means the engine responded; a string is the reason it was unresponsive.
    error: str | None = None


class EngineAdapter(ABC):
    """Abstract base for a single search engine.

    ``correlation_group`` is the load-bearing field for provenance-aware fusion:
    engines in the same group share an underlying crawler (e.g. SearXNG and ddgs both
    lean on Google/Bing), so the router collapses them to one independent vote and a
    consensus bonus only applies across *distinct* groups.
    """

    name: str
    correlation_group: str = "general-aggregator"

    @abstractmethod
    def search(self, request: SearchRequest) -> EngineOutput:
        """Run one search. Implementations should not raise for ordinary engine
        failures; return an ``EngineOutput`` with ``error`` set instead. The router
        also defends against unexpected exceptions."""
        raise NotImplementedError

    def enabled(self) -> bool:
        """Whether this adapter is usable (key present, URL configured, ...)."""
        return True
