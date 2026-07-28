"""Correlation groups: the single source of truth for an engine's ``correlation_group``.

Engines that share an underlying crawler belong to the same group; provenance-aware
fusion collapses them to one independent vote so a consensus bonus cannot amplify the
same crawler agreeing with itself.
"""

from __future__ import annotations

GENERAL_AGGREGATOR = "general-aggregator"  # SearXNG, ddgs: both lean on Google/Bing
NEURAL_INDEX = "neural-index"  # Exa-style independent neural index
# Onion indexes. Ahmia queried directly and Ahmia reached through SearXNG's onions
# category are the same crawler seen twice, so they share a group and their agreement
# counts once. Nothing here overlaps the clearnet groups: no Google-derived index
# crawls onion services.
ONION_INDEX = "onion-index"
