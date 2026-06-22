"""Pydantic models mirroring the Layer 3 agent-io contract.

``agent-io.schema.json`` (agentio@1.0.0) is the source of truth; these are the
in-process view. Requests forbid unknown fields (fail fast on a typo). The three
capabilities are web_search (-> Layer 1), web_fetch (-> Layer 2A, fenced + paginated),
and web_open (paginate an already-fetched page from the Layer 2B store). ``handle`` is
the only cross-layer key and is human-readable. There is no output-length cap:
pagination is progressive disclosure, and the full body is preserved in the store.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AGENTIO_CONTRACT_VERSION = "1.0.0"

# Engineering defaults (NOT documented platform constants). 8 results balances recall
# against the per-call token budget; 4000-token pages stay well under the 25,000-token
# tool-output cap some harnesses (e.g. Claude Code) impose.
DEFAULT_MAX_RESULTS = 8
DEFAULT_PAGE_SIZE_TOKENS = 4000
DEFAULT_CHARS_PER_TOKEN = 4.0

Detail = Literal["concise", "detailed"]
SafeSearch = Literal["off", "moderate", "strict"]
Freshness = Literal["any", "day", "week", "month", "year"]
FetchTier = Literal["auto", "http", "browser", "stealth"]


# --- web_search --------------------------------------------------------------------


class AgentSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1)
    offset: int = Field(default=0, ge=0)
    detail: Detail = "concise"
    engines: list[str] | None = None
    country: str | None = None
    language: str | None = None
    freshness: Freshness = "any"
    safesearch: SafeSearch = "moderate"
    site: str | None = None


class AgentSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)
    url: str
    handle: str = Field(min_length=1)
    title: str | None = None
    snippet: str | None = None
    engines: list[str] = Field(default_factory=list)
    score: float | None = None
    published: str | None = None


class AgentSearchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    results: list[AgentSearchHit]
    total_returned: int = Field(default=0, ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)


# --- web_fetch / web_open ----------------------------------------------------------


class AgentFetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    page_size_tokens: int = Field(default=DEFAULT_PAGE_SIZE_TOKENS, ge=1)
    tier: FetchTier = "auto"
    timeout_ms: int = Field(default=20000, ge=1)
    allow_private_hosts: bool = False
    datamark: bool = False
    chars_per_token: float = Field(default=DEFAULT_CHARS_PER_TOKEN, gt=0.0)


class AgentOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1)
    page: int = Field(default=1, ge=1)
    page_size_tokens: int = Field(default=DEFAULT_PAGE_SIZE_TOKENS, ge=1)
    datamark: bool = False
    chars_per_token: float = Field(default=DEFAULT_CHARS_PER_TOKEN, gt=0.0)


class FenceInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nonce: str = Field(min_length=1)
    open: str
    close: str
    datamarked: bool = False


class AgentPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handle: str = Field(min_length=1)
    url: str
    content: str
    page: int = Field(ge=1)
    total_pages: int = Field(ge=1)
    untrusted: Literal[True] = True
    fence: FenceInfo
    title: str | None = None
    page_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    has_more: bool = False
    blocked: bool = False
    block_reason: str | None = None
    source: Literal["live", "cache"] = "live"
    fetched_at: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AgentFetchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pages: list[AgentPage]
    query: str | None = None
    warnings: list[str] = Field(default_factory=list)
