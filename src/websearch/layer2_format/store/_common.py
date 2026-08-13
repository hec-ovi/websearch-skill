"""Shared helpers for the page-index adapters.

Chunking, the FTS5-safe query escaper, and token estimation are identical across the
SQLite-FTS5 and the pure-Python BM25 backends, so they live here. Each adapter only
differs in how it indexes and ranks the passages these helpers produce.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..chunk import chunk_markdown
from ..dedup import content_hash
from ..exceptions import DependencyMissing
from ..ids import doc_id
from ..models import DEFAULT_CHARS_PER_TOKEN, PageInput, StoreConfig, StoredDoc
from ..tokens import estimate_tokens

# Map C0 control characters and DEL to spaces before tokenizing. A NUL in particular is
# fatal to FTS5 (its query parser treats it as a C-string terminator and raises
# "unterminated string"); the rest are stripped for parity with the memory tokenizer.
_CONTROL_TO_SPACE = {c: " " for c in range(0x20)}
_CONTROL_TO_SPACE[0x7F] = " "


@dataclass
class PreparedPassage:
    # No stored id: both adapters recompute passage_id(doc_id, ordinal) at search time.
    doc_id: str
    url: str
    title: str | None
    text: str
    ordinal: int
    start: int
    end: int


@dataclass
class PreparedDoc:
    id: str
    url: str
    title: str | None
    markdown: str
    fetched_at: str | None
    content_hash: str
    token_estimate: int
    passages: list[PreparedPassage]


def prepare_doc(page: PageInput, config: StoreConfig) -> PreparedDoc:
    """Chunk a page into passages and compute its stored-doc fields."""
    did = doc_id(page.url)
    chash = page.content_hash or content_hash(page.markdown)
    chunks = chunk_markdown(
        page.markdown,
        strategy=config.chunk_strategy,
        max_chars=config.chunk_max_chars,
        overlap=config.chunk_overlap,
    )
    passages = [
        PreparedPassage(
            doc_id=did,
            url=page.url,
            title=page.title,
            text=text,
            ordinal=ordinal,
            start=start,
            end=end,
        )
        for ordinal, (text, start, end) in enumerate(chunks)
    ]
    return PreparedDoc(
        id=did,
        url=page.url,
        title=page.title,
        markdown=page.markdown,
        fetched_at=page.fetched_at,
        content_hash=chash,
        token_estimate=estimate_tokens(page.markdown, chars_per_token=DEFAULT_CHARS_PER_TOKEN),
        passages=passages,
    )


def escape_fts5_query(query: str) -> str | None:
    """Turn an arbitrary user query into a safe FTS5 MATCH string.

    Every whitespace token is wrapped in double quotes (internal quotes doubled), so
    FTS5 operators and special characters (``"`` ``*`` ``:`` ``-`` ``(`` ``)`` ``AND``
    ``OR`` ``NOT`` ``NEAR``) are treated as literal terms instead of raising a syntax
    error. Tokens are joined with ``OR`` so a passage matching ANY query term is a
    candidate and BM25 ranks it (standard recall-oriented search behavior, and the same
    semantics as the pure-Python BM25 fallback). Returns None when the query has no
    usable tokens (caller short-circuits to an empty result), since an empty MATCH
    string is itself a syntax error.
    """
    tokens = query.translate(_CONTROL_TO_SPACE).split()
    if not tokens:
        return None
    quoted = ['"' + tok.replace('"', '""') + '"' for tok in tokens]
    return " OR ".join(quoted)


def require_bm25(mode: str) -> None:
    """Refuse a search mode neither built-in adapter implements.

    Answering a vector request with BM25 ranked results would be a silent lie about the
    backend; vector and hybrid are opt-in adapters, the same rule as StoreConfig.adapter.
    """
    if mode != "bm25":
        raise DependencyMissing(
            f"search mode '{mode}' needs an opt-in vector adapter; none is installed "
            "(the built-in indexes implement bm25)."
        )


def stored_doc(prepared: PreparedDoc, *, n_passages: int, deduped: bool) -> StoredDoc:
    """The AddResult row for one prepared document."""
    return StoredDoc(
        id=prepared.id,
        url=prepared.url,
        title=prepared.title,
        n_passages=n_passages,
        content_hash=prepared.content_hash,
        fetched_at=prepared.fetched_at,
        token_estimate=prepared.token_estimate,
        deduped=deduped,
    )
