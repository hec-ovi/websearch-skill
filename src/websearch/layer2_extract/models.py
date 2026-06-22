"""Pydantic models mirroring the Layer 2A contracts.

``fetch.schema.json`` and ``extract.schema.json`` are the source of truth; these
models are the in-process Python view of the same shapes. Requests forbid unknown
fields (fail fast on a typo); results allow the schema's documented fields only.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

FETCH_CONTRACT_VERSION = "1.1.0"
EXTRACT_CONTRACT_VERSION = "1.0.0"

# Default transport guard: bound how much of a response we buffer/hand downstream.
# This is a DoS defense, not an output/LLM cap; extracted content is never truncated.
DEFAULT_MAX_BYTES = 10_000_000

# Recommended block_reason vocabulary (kept as plain strings in the contract so a
# new anti-bot vendor never forces a contract bump). Grouped by escalation policy.
ESCALATABLE_BLOCKS = frozenset(
    {
        "cloudflare_challenge",
        "cloudflare_firewall",
        "datadome",
        "perimeterx",
        "akamai",
        "imperva",
        "ddos_guard",
        "forbidden_suspected_bot",
        "unavailable_suspected_block",
    }
)
# These will not be helped by a stealthier tier from the same egress, so stop.
TERMINAL_BLOCKS = frozenset({"rate_limited", "auth_required", "legal_geo_block"})


# --- FETCH sub-port ----------------------------------------------------------------


class Proxy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    type: Literal["http", "socks5", "wireguard", "none"] = "http"


class Cookie(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: str
    domain: str | None = None
    path: str | None = None


class Politeness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_host_delay_ms: int = Field(default=0, ge=0)
    respect_robots: bool = False


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    tier_hint: Literal["auto", "http", "browser", "stealth"] = "auto"
    render_js: bool | None = None
    wait_for: str | None = None
    timeout_ms: int = Field(default=20000, ge=1)
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: list[Cookie] = Field(default_factory=list)
    proxy: Proxy | None = None
    user_agent: str | None = None
    screenshot: bool = False
    max_bytes: int | None = Field(default=DEFAULT_MAX_BYTES, ge=1)
    allow_private_hosts: bool = False
    politeness: Politeness = Field(default_factory=Politeness)

    @field_validator("url")
    @classmethod
    def _http_scheme_only(cls, v: str) -> str:
        if urlsplit(v).scheme.lower() not in ("http", "https"):
            raise ValueError("url must be an absolute http(s) URL")
        return v


class FetchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str | None = None
    status: int
    ok: bool
    fetched_via: Literal[
        "http", "curl_cffi", "browser", "undetected", "nodriver", "camoufox", "jina_reader"
    ]
    raw_html: str | None = None
    rendered_html: str | None = None
    content_type: str | None = None
    redirects: list[str] = Field(default_factory=list)
    headers: dict[str, str] = Field(default_factory=dict)
    blocked: bool = False
    block_reason: str | None = None
    screenshot_b64: str | None = None
    tier_attempts: list[str] = Field(default_factory=list)
    fetch_ms: int = Field(default=0, ge=0)
    error: str | None = None


# --- EXTRACT sub-port --------------------------------------------------------------

PageType = Literal[
    "article", "forum", "product", "listing", "collection", "documentation", "service", "unknown"
]


class ExtractRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    html: str
    base_url: str | None = None
    engine: Literal[
        "trafilatura", "resiliparse", "rs_trafilatura", "crawl4ai", "jina_readerlm", "auto"
    ] = "trafilatura"
    favor: Literal["precision", "recall", "balanced"] = "balanced"
    output_format: Literal["markdown", "text", "json"] = "markdown"
    include_tables: bool = True
    include_links: bool = True
    include_images: bool = False
    include_comments: bool = False
    query: str | None = None
    neural_fallback: bool = True


class ExtractResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_markdown: str
    content_text: str | None = None
    title: str | None = None
    byline: str | None = None
    date: str | None = None
    language: str | None = None
    page_type: PageType = "unknown"
    json_ld: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    word_count: int = Field(default=0, ge=0)
    quality_score: float = Field(ge=0.0, le=1.0)
    extracted_via: str
    extract_ms: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class ExtractSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    final_url: str | None = None
    status: int
    ok: bool
    fetched_via: str
    content_type: str | None = None
    blocked: bool = False
    block_reason: str | None = None
    tier_attempts: list[str] = Field(default_factory=list)
    fetch_ms: int = Field(default=0, ge=0)


class ExtractTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_ms: int = Field(default=0, ge=0)
    fetch_ms: int = Field(default=0, ge=0)
    extract_ms: int = Field(default=0, ge=0)


class ExtractPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    source: ExtractSource
    result: ExtractResult
    timing: ExtractTiming = Field(default_factory=ExtractTiming)
    warnings: list[str] = Field(default_factory=list)
