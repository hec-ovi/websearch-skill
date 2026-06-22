"""Per-engine capability map and correlation groups.

This is the single source of truth for an engine's ``correlation_group`` (used by
provenance-aware fusion) and a description of which request params it can honor, so
the router can drop unsupported params instead of failing.
"""

from __future__ import annotations

from dataclasses import dataclass

# Correlation groups. Engines that share an underlying crawler belong to the same
# group; fusion collapses them to one independent vote so a consensus bonus cannot
# amplify the same crawler agreeing with itself.
GENERAL_AGGREGATOR = "general-aggregator"  # SearXNG, ddgs: both lean on Google/Bing
NEURAL_INDEX = "neural-index"  # Exa-style independent neural index
CURATED_INDEX = "curated-index"  # Tavily-style curated index
BRAVE_INDEX = "brave-index"  # Brave's own independent index


@dataclass(frozen=True)
class Capability:
    name: str
    correlation_group: str
    decorrelated: bool = False  # True = an independent index worth real cross-engine fusion
    requires_key: bool = False
    supports_country: bool = True
    supports_language: bool = True
    supports_freshness: bool = True
    supports_safesearch: bool = True
    supports_news: bool = True
    max_count: int | None = None  # per-engine result cap, None = no known cap


CAPABILITIES: dict[str, Capability] = {
    "searxng": Capability("searxng", GENERAL_AGGREGATOR, decorrelated=False),
    "ddgs": Capability("ddgs", GENERAL_AGGREGATOR, decorrelated=False),
    # Keyed, decorrelated engines are added with their adapters in a later slice:
    # "exa": Capability("exa", NEURAL_INDEX, decorrelated=True, requires_key=True),
    # "tavily": Capability("tavily", CURATED_INDEX, decorrelated=True, requires_key=True),
    # "brave": Capability("brave", BRAVE_INDEX, decorrelated=True, requires_key=True),
}
