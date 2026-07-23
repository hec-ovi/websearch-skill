"""Correlation groups: the single source of truth for an engine's ``correlation_group``.

Engines that share an underlying crawler belong to the same group; provenance-aware
fusion collapses them to one independent vote so a consensus bonus cannot amplify the
same crawler agreeing with itself.
"""

from __future__ import annotations

GENERAL_AGGREGATOR = "general-aggregator"  # SearXNG, ddgs: both lean on Google/Bing
NEURAL_INDEX = "neural-index"  # Exa-style independent neural index
