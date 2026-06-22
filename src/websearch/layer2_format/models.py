"""Pydantic models mirroring the Layer 2B contracts.

``format.schema.json`` and ``store.schema.json`` are the source of truth; these
models are the in-process Python view of the same shapes. Requests forbid unknown
fields (fail fast on a typo); results allow the schema's documented fields only.

Layer 2B is two decoupled sub-ports: FORMAT (vendor-neutral results in, one
layout-stable Markdown document plus a parallel JSON sidecar out) and STORE (full
pages in, ranked passages and a resolver out). There is no output-length cap
anywhere: full bodies are stored and echoed in the sidecar verbatim.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FORMAT_CONTRACT_VERSION = "1.0.0"
STORE_CONTRACT_VERSION = "1.0.0"

# Dependency-free token estimate: OpenAI's documented "~4 chars per token" rule of
# thumb. Configurable per request; a caller may inject a real tokenizer instead.
DEFAULT_CHARS_PER_TOKEN = 4.0

# MinHash near-duplicate defaults (Manning IR-book / datasketch): word 4-gram
# shingles, 128 permutations, conservative Jaccard 0.9.
DEFAULT_NUM_PERM = 128
DEFAULT_SHINGLE_SIZE = 4
DEFAULT_JACCARD_THRESHOLD = 0.9


# --- FORMAT sub-port ----------------------------------------------------------------


class Highlight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    score: float | None = None


class ResultInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    id: str | None = None
    title: str | None = None
    published_date: str | None = None
    author: str | None = None
    site: str | None = None
    score: float | None = None
    lang: str | None = None
    fetched_at: str | None = None
    highlights: list[Highlight] = Field(default_factory=list)
    summary: str | None = None
    body_markdown: str | None = None
    body_blocks: list[str] = Field(default_factory=list)
    page_type: str | None = None
    quality_score: float | None = None
    content_hash: str | None = None
    token_estimate: int | None = Field(default=None, ge=0)


class DedupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    method: Literal["exact", "minhash", "both"] = "both"
    jaccard_threshold: float = Field(default=DEFAULT_JACCARD_THRESHOLD, ge=0.0, le=1.0)
    num_perm: int = Field(default=DEFAULT_NUM_PERM, ge=16)
    shingle_size: int = Field(default=DEFAULT_SHINGLE_SIZE, ge=1)


class FormatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ResultInput]
    query: str | None = None
    page: int = Field(default=0, ge=0)
    page_size: int = Field(default=5, ge=1)
    mode: Literal["auto", "index", "full"] = "auto"
    body: Literal["highlights", "summary", "text"] = "highlights"
    inline_token_budget: int = Field(default=6000, ge=0)
    body_char_budget: int | None = Field(default=4000, ge=0)
    dedup: DedupParams = Field(default_factory=DedupParams)
    include_sidecar: bool = True
    include_anthropic_blocks: bool = False
    anthropic_citations: bool = True
    chars_per_token: float = Field(default=DEFAULT_CHARS_PER_TOKEN, gt=0.0)


class DroppedDuplicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    id: str | None = None
    similarity: float | None = None
    reason: Literal["exact", "minhash"] = "minhash"


class FormattedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    rank: int = Field(ge=1)
    title: str | None = None
    published_date: str | None = None
    author: str | None = None
    site: str | None = None
    score: float | None = None
    lang: str | None = None
    fetched_at: str | None = None
    page_type: str | None = None
    quality_score: float | None = None
    highlights: list[Highlight] = Field(default_factory=list)
    summary: str | None = None
    body_markdown: str | None = None
    body_blocks: list[str] = Field(default_factory=list)
    token_estimate: int = Field(default=0, ge=0)
    rendered_full: bool = False
    truncated_in_markdown: bool = False
    dedup_of: str | None = None
    dropped_duplicates: list[DroppedDuplicate] = Field(default_factory=list)


class AnthropicTextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"] = "text"
    text: str = Field(min_length=1)


class AnthropicCitations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class AnthropicCacheControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ephemeral"] = "ephemeral"


class AnthropicSearchResultBlock(BaseModel):
    # Omit citations/cache_control entirely (never null) when unused.
    model_config = ConfigDict(extra="forbid")

    type: Literal["search_result"] = "search_result"
    source: str = Field(min_length=1)
    title: str
    content: list[AnthropicTextBlock] = Field(min_length=1)
    citations: AnthropicCitations | None = None
    cache_control: AnthropicCacheControl | None = None

    def to_block(self) -> dict:
        """Serialize to the exact Anthropic content-block shape, dropping unset optionals."""
        return self.model_dump(mode="json", exclude_none=True)


class FormatSidecar(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=0)
    page_size: int = Field(ge=1)
    total_results: int = Field(ge=0)
    total_pages: int = Field(ge=0)
    mode: Literal["index", "full"]
    results: list[FormattedResult]
    query: str | None = None
    next_cursor: str | None = None
    page_token_estimate: int = Field(default=0, ge=0)
    total_dropped_duplicates: int = Field(default=0, ge=0)
    anthropic_search_result_blocks: list[dict] = Field(default_factory=list)


class FormatPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    markdown: str
    sidecar: FormatSidecar | None = None
    warnings: list[str] = Field(default_factory=list)


# --- STORE / PageIndex sub-port -----------------------------------------------------


class StoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    adapter: Literal["sqlite-fts5", "memory", "sqlite-vec", "tantivy"] = "sqlite-fts5"
    persist_path: str | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=0)
    chunk_strategy: Literal["heading", "fixed"] = "heading"
    chunk_max_chars: int = Field(default=1200, ge=1)
    chunk_overlap: int = Field(default=0, ge=0)


class PageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1)
    markdown: str
    title: str | None = None
    fetched_at: str | None = None
    content_hash: str | None = None


class StoredDoc(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    n_passages: int = Field(ge=0)
    content_hash: str
    title: str | None = None
    fetched_at: str | None = None
    token_estimate: int = Field(default=0, ge=0)
    deduped: bool = False


class AddResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    added: list[StoredDoc]


class Passage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    doc_id: str
    url: str
    text: str
    score: float
    char_span: tuple[int, int]
    ordinal: int = Field(ge=0)
    title: str | None = None


class SearchPageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int = Field(default=10, ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=5, ge=1)
    mode: Literal["bm25", "vector", "hybrid"] = "bm25"


class SearchPageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passages: list[Passage]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    has_more: bool
    backend: str


class PageDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    markdown: str
    title: str | None = None
    fetched_at: str | None = None
    content_hash: str | None = None
    n_passages: int = Field(default=0, ge=0)
    token_estimate: int = Field(default=0, ge=0)


class ResolveIndexEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    url: str
    n_passages: int = Field(ge=0)
    title: str | None = None
    fetched_at: str | None = None
    token_estimate: int = Field(default=0, ge=0)


class ResolveIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    docs: list[ResolveIndexEntry]
    total: int = Field(ge=0)
    backend: str
