"""Layer 3: agent I/O. The consolidated agent-facing surface over Layers 1/2A/2B.

Three capabilities, all over the cross-cutting Envelope (meta.layer "agentio"):

- ``web_search`` -> Layer 1 search, reshaped to agent results with human-readable handles.
- ``web_fetch``  -> Layer 2A fetch+extract, with the untrusted-content fence and lossless
  token-budget pagination, indexed into the Layer 2B store for later resolution.
- ``web_open``   -> paginate an already-fetched page from the store by handle, no re-fetch.

``build_agent_io`` wires the default closure; the router, pipeline, and store are
swappable behind their ports. ``mcp_server`` (using the base ``fastmcp`` dependency) exposes
the same three as MCP tools; it is imported lazily so the non-MCP commands do not pay the
fastmcp import at startup.
"""

from __future__ import annotations

from .facade import AgentIO, build_agent_io, make_handle
from .fence import DEFAULT_DATAMARK, fence_untrusted, make_nonce
from .models import (
    AGENTIO_CONTRACT_VERSION,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_MAX_RESULTS,
    DEFAULT_PAGE_SIZE_TOKENS,
    AgentFetchPayload,
    AgentFetchRequest,
    AgentOpenRequest,
    AgentPage,
    AgentSearchHit,
    AgentSearchPayload,
    AgentSearchRequest,
    FenceInfo,
)
from .pagination import paginate

__all__ = [
    "AGENTIO_CONTRACT_VERSION",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_PAGE_SIZE_TOKENS",
    "DEFAULT_CHARS_PER_TOKEN",
    "DEFAULT_DATAMARK",
    # models
    "AgentSearchRequest",
    "AgentSearchHit",
    "AgentSearchPayload",
    "AgentFetchRequest",
    "AgentOpenRequest",
    "AgentPage",
    "AgentFetchPayload",
    "FenceInfo",
    # facade
    "AgentIO",
    "build_agent_io",
    "make_handle",
    # fence + pagination
    "fence_untrusted",
    "make_nonce",
    "paginate",
]
