"""Pydantic models for the Layer-1 search port.

These mirror ``contracts/search.schema.json`` (search@1.1.0). The field names are
capability-named (snippet, fused_score, sources); each backend adapter maps its
native shape onto these models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SEARCH_CONTRACT_VERSION = "1.2.0"

# A clearnet engine answers in a second or two; an onion index is reached through three
# relays and answers in ten to thirty. The clearnet default applied to an onion search
# times out every query, so the default follows the network being searched. An explicit
# timeout_ms still wins, whatever it says.
DEFAULT_TIMEOUT_MS = 8000
ONION_TIMEOUT_MS = 45000

SafeSearch = Literal["off", "moderate", "strict"]
ResultType = Literal["web", "news"]
FreshnessEnum = Literal["any", "day", "week", "month", "year"]
FusionMethod = Literal["rrf", "weighted_rrf", "score_convex"]


class FreshnessRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str
    end: str


class Fusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: FusionMethod = "weighted_rrf"
    k: int = Field(default=60, ge=1)
    weights: dict[str, float] | None = None
    consensus_bonus: bool = True


class Egress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    profile: str | None = None
    country: str | None = None


class SearchRequest(BaseModel):
    """Layer-1 input contract."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    count: int = Field(default=10, ge=1)
    offset: int = Field(default=0, ge=0)
    country: str | None = None
    language: str | None = None
    safesearch: SafeSearch = "moderate"
    freshness: FreshnessEnum | FreshnessRange = "any"
    include_sites: list[str] = Field(default_factory=list)
    exclude_sites: list[str] = Field(default_factory=list)
    result_type: ResultType = "web"
    # Search the onion indexes instead of the clearnet engines. Not a filter over the same
    # fanout and not additive: no clearnet engine indexes onion services and no onion index
    # indexes the clearnet, so this picks which set of engines runs. Needs the tor layer.
    onion: bool = False
    engines: list[str] | None = None
    max_total_results: int = Field(default=20, ge=0)  # 0 = no cap after fusion
    fusion: Fusion = Field(default_factory=Fusion)
    egress: Egress | None = None
    engine_overrides: dict[str, dict] = Field(default_factory=dict)
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1)

    @model_validator(mode="after")
    def _onion_searches_get_longer(self) -> SearchRequest:
        if self.onion and "timeout_ms" not in self.model_fields_set:
            self.timeout_ms = ONION_TIMEOUT_MS
        return self


class SourceProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str
    rank: int = Field(ge=1)
    raw_score: float | None = None
    native_id: str | None = None


class ResultItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    display_url: str
    title: str
    snippet: str
    snippets: list[str] = Field(default_factory=list)
    published_date: str | None = None
    fused_score: float
    sources: list[SourceProvenance]
    result_type: ResultType = "web"
    language: str | None = None
    favicon: str | None = None
    thumbnail: str | None = None


class UnresponsiveEngine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: str
    reason: str


class Timing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_ms: int = 0
    per_engine_ms: dict[str, int] = Field(default_factory=dict)


class SearchPayload(BaseModel):
    """Envelope.data for a search response."""

    model_config = ConfigDict(extra="forbid")

    query: str
    request_id: str
    results: list[ResultItem]
    answers: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    engines_queried: list[str]
    unresponsive_engines: list[UnresponsiveEngine] = Field(default_factory=list)
    timing: Timing = Field(default_factory=Timing)
    warnings: list[str] = Field(default_factory=list)
