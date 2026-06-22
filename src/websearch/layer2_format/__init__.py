"""Layer 2B: FORMAT (LLM-ready paginated Markdown + JSON sidecar) + STORE (page index).

Two decoupled sub-ports, mirroring Layer 2A's fetch/extract split. FORMAT turns
vendor-neutral results into one layout-stable Markdown document plus a lossless JSON
sidecar, relevance-ordered and paginated, with near-duplicate dedup and progressive
disclosure. STORE is an ephemeral fetched-page index (SQLite FTS5 in-memory by default,
pure-Python BM25 fallback) exposing add/search/get/resolve_index for passage search and
the progressive-disclosure resolver. Neither port caps output length.

``build_format_pipeline`` and ``build_page_index`` wire the default closure; the renderer
and the index backend are swappable behind their ports.
"""

from __future__ import annotations

from .exceptions import DependencyMissing
from .format_pipeline import FormatPipeline
from .ids import doc_id, passage_id, site_of
from .models import (
    FORMAT_CONTRACT_VERSION,
    STORE_CONTRACT_VERSION,
    AddResult,
    DedupParams,
    FormatPayload,
    FormatRequest,
    FormatSidecar,
    FormattedResult,
    Highlight,
    PageDocument,
    PageInput,
    Passage,
    ResolveIndex,
    ResultInput,
    SearchPageRequest,
    SearchPageResult,
    StoreConfig,
    StoredDoc,
)
from .ports import FormatRenderer, PageIndex
from .renderer import MarkdownRenderer
from .store import MemoryBm25Index, SqliteFts5Index, build_page_index, fts5_available

__all__ = [
    "FORMAT_CONTRACT_VERSION",
    "STORE_CONTRACT_VERSION",
    # FORMAT
    "ResultInput",
    "Highlight",
    "DedupParams",
    "FormatRequest",
    "FormatPayload",
    "FormatSidecar",
    "FormattedResult",
    "FormatRenderer",
    "MarkdownRenderer",
    "FormatPipeline",
    "build_format_pipeline",
    # STORE
    "PageInput",
    "StoredDoc",
    "AddResult",
    "Passage",
    "SearchPageRequest",
    "SearchPageResult",
    "PageDocument",
    "ResolveIndex",
    "StoreConfig",
    "PageIndex",
    "SqliteFts5Index",
    "MemoryBm25Index",
    "build_page_index",
    "fts5_available",
    # shared
    "doc_id",
    "passage_id",
    "site_of",
    "DependencyMissing",
]


def build_format_pipeline(renderer: FormatRenderer | None = None) -> FormatPipeline:
    """The default FORMAT pipeline (MarkdownRenderer)."""
    return FormatPipeline(renderer)
