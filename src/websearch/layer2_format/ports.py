"""The two Layer 2B capability ports: FORMAT (renderer) and STORE (page index).

Both are decoupled and independently swappable, mirroring Layer 2A's fetch/extract
split. The default closure is ``MarkdownRenderer`` (FORMAT) and the SQLite-FTS5
in-memory index with a pure-Python BM25 fallback (STORE). Alternate renderers and
opt-in index backends (vector, tantivy) implement the same ports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import (
    AddResult,
    FormattedResult,
    PageDocument,
    PageInput,
    ResolveIndex,
    SearchPageRequest,
    SearchPageResult,
)


class FormatRenderer(ABC):
    """Renders the page's ordered, deduped results into one layout-stable document."""

    #: Stable adapter id, e.g. "markdown".
    name: str = "markdown"

    @abstractmethod
    def render(
        self,
        results: list[FormattedResult],
        *,
        query: str | None,
        mode: str,
        body: str,
        page: int,
        page_size: int,
        total_results: int,
        total_pages: int,
        next_cursor: str | None,
        total_dropped_duplicates: int,
        page_token_estimate: int,
        body_char_budget: int | None,
    ) -> str:
        """Return the rendered document for this page."""


class PageIndex(ABC):
    """The ephemeral fetched-page store and progressive-disclosure resolver."""

    #: Stable backend id reported in results, e.g. "sqlite-fts5" | "memory-bm25".
    name: str = "page-index"

    @abstractmethod
    def add(self, pages: list[PageInput]) -> AddResult:
        """Index full pages (idempotent on url + content_hash). Stores Markdown verbatim."""

    @abstractmethod
    def search(self, request: SearchPageRequest) -> SearchPageResult:
        """Rank passages across the held corpus (BM25 by default)."""

    @abstractmethod
    def get(self, id_or_url: str) -> PageDocument | None:
        """Return the full document for a resolver deep dive, or None if absent."""

    @abstractmethod
    def resolve_index(self) -> ResolveIndex:
        """Return lightweight metadata for every held document (cheap index first)."""

    def available(self) -> bool:
        return True

    def close(self) -> None:  # noqa: B027  (optional hook; default is intentionally a no-op)
        """Release any backing resources (no-op by default)."""
