"""websearch-skill: open-source multi-engine web search + extraction for AI agents.

The package is organized as isolated, contract-driven layers (ports + adapters).
Layer 1 (search) is the first vertical slice; later layers (extract, format,
agent-io) plug in behind their own versioned JSON-Schema contracts.
"""

__version__ = "0.1.0"
