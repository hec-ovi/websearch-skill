"""websearch-skill: open-source multi-engine web search + extraction for AI agents.

The package is organized as isolated, contract-driven layers (ports + adapters):
Layer 1 searches across engines, Layer 2A fetches and extracts, Layer 2B formats and
stores, and Layer 3 consolidates them into the agent-facing web_search / web_fetch /
web_open surface (CLI and FastMCP), alongside the standalone keyless arxiv and github
tools. Each layer ships a versioned JSON-Schema contract under ``contracts/``.
"""


def __getattr__(name: str) -> str:
    # __version__ is derived from the installed distribution metadata (never a hardcoded
    # string that drifts from pyproject), resolved lazily because importlib.metadata costs
    # ~20 ms and most imports of this package never read the version.
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            resolved = version("websearch-skill")
        except PackageNotFoundError:  # source tree without an installed distribution
            resolved = "0.0.0"
        globals()["__version__"] = resolved  # cache: later reads skip this hook
        return resolved
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
