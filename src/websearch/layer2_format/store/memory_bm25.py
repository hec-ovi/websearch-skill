"""Pure-Python BM25 page index: the zero-dependency fallback.

Used when the local SQLite has no FTS5 (a distro or self-built interpreter may omit it).
It holds the same passages the SQLite adapter would and ranks them with the standard
Okapi BM25 (k1=1.2, b=0.75), matching FTS5's IDF, over a Unicode-folding tokenizer that
approximates FTS5's unicode61, returning the identical Passage / SearchPageResult /
PageDocument / ResolveIndex shapes so it is a drop-in behind the PageIndex port. At the
corpus size a single search cycle produces (tens to hundreds of passages) recomputing the
term statistics on each change is negligible.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from ..ids import passage_id
from ..models import (
    AddResult,
    PageDocument,
    PageInput,
    Passage,
    ResolveIndex,
    ResolveIndexEntry,
    SearchPageRequest,
    SearchPageResult,
    StoreConfig,
    StoredDoc,
)
from ._common import prepare_doc

_NAME = "memory-bm25"
_K1 = 1.2
_B = 0.75
# Unicode word characters minus the connector underscore. Together with NFKD folding
# this approximates FTS5's default unicode61 tokenizer (case-folded, diacritics removed,
# Unicode letters tokenized), so accented and non-Latin queries match like the SQLite
# adapter instead of silently returning nothing on a fallback-only machine.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _TOKEN.findall(folded)


@dataclass
class _MemPassage:
    id: str
    doc_id: str
    url: str
    title: str | None
    text: str
    ordinal: int
    start: int
    end: int
    tf: Counter = field(default_factory=Counter)
    length: int = 0


@dataclass
class _MemDoc:
    id: str
    url: str
    title: str | None
    markdown: str
    fetched_at: str | None
    content_hash: str
    n_passages: int
    token_estimate: int


class MemoryBm25Index:
    name = _NAME

    def __init__(self, config: StoreConfig | None = None):
        self._config = config or StoreConfig()
        self._docs: dict[str, _MemDoc] = {}
        self._order: list[str] = []  # doc ids in insertion order
        self._passages: list[_MemPassage] = []
        self._df: Counter = Counter()
        self._avgdl: float = 0.0

    def available(self) -> bool:
        return True

    def _reindex(self) -> None:
        self._df = Counter()
        total_len = 0
        for p in self._passages:
            for term in p.tf:
                self._df[term] += 1
            total_len += p.length
        self._avgdl = (total_len / len(self._passages)) if self._passages else 0.0

    def add(self, pages: list[PageInput]) -> AddResult:
        added: list[StoredDoc] = []
        changed = False
        for page in pages:
            prepared = prepare_doc(page, self._config)
            existing = self._docs.get(prepared.id)
            if existing is not None and existing.content_hash == prepared.content_hash:
                added.append(
                    StoredDoc(
                        id=prepared.id,
                        url=prepared.url,
                        title=prepared.title,
                        n_passages=existing.n_passages,
                        content_hash=prepared.content_hash,
                        fetched_at=prepared.fetched_at,
                        token_estimate=prepared.token_estimate,
                        deduped=True,
                    )
                )
                continue
            if existing is not None:  # changed content: drop old passages, move to end
                self._passages = [p for p in self._passages if p.doc_id != prepared.id]
                # SQLite reassigns a higher rowid on replace, so resolve_index lists the
                # changed doc last; match that here for adapter-consistent ordering.
                self._order.remove(prepared.id)
                self._order.append(prepared.id)
            else:
                self._order.append(prepared.id)
            self._docs[prepared.id] = _MemDoc(
                id=prepared.id,
                url=prepared.url,
                title=prepared.title,
                markdown=prepared.markdown,
                fetched_at=prepared.fetched_at,
                content_hash=prepared.content_hash,
                n_passages=len(prepared.passages),
                token_estimate=prepared.token_estimate,
            )
            for p in prepared.passages:
                tokens = _tokenize(p.text)
                self._passages.append(
                    _MemPassage(
                        id=p.id,
                        doc_id=p.doc_id,
                        url=p.url,
                        title=p.title,
                        text=p.text,
                        ordinal=p.ordinal,
                        start=p.start,
                        end=p.end,
                        tf=Counter(tokens),
                        length=len(tokens),
                    )
                )
            changed = True
            added.append(
                StoredDoc(
                    id=prepared.id,
                    url=prepared.url,
                    title=prepared.title,
                    n_passages=len(prepared.passages),
                    content_hash=prepared.content_hash,
                    fetched_at=prepared.fetched_at,
                    token_estimate=prepared.token_estimate,
                    deduped=False,
                )
            )
        if changed:
            self._reindex()
        return AddResult(added=added)

    def _idf(self, term: str, n: int) -> float:
        # Match FTS5's bm25 IDF exactly (ext/fts5/fts5_aux.c): the unclamped
        # Robertson/Sparck-Jones log, then floor a non-positive value (a term in more
        # than half the rows) to 1e-6 so common terms get near-zero weight as in FTS5.
        n_q = self._df.get(term, 0)
        idf = math.log((n - n_q + 0.5) / (n_q + 0.5))
        return 1e-6 if idf <= 0 else idf

    def _score(self, passage: _MemPassage, query_terms: list[str], n: int) -> float:
        if self._avgdl <= 0:
            return 0.0
        score = 0.0
        for term in query_terms:
            tf = passage.tf.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term, n)
            denom = tf + _K1 * (1 - _B + _B * passage.length / self._avgdl)
            score += idf * (tf * (_K1 + 1)) / denom
        return score

    def search(self, request: SearchPageRequest) -> SearchPageResult:
        query_terms = _tokenize(request.query)
        n = len(self._passages)
        if not query_terms or n == 0:
            return SearchPageResult(
                passages=[],
                total=0,
                page=request.page,
                page_size=request.page_size,
                has_more=False,
                backend=self.name,
            )
        scored = [(self._score(p, query_terms, n), idx, p) for idx, p in enumerate(self._passages)]
        scored = [s for s in scored if s[0] > 0]
        # Descending score; ties broken by insertion order for determinism.
        scored.sort(key=lambda s: (-s[0], s[1]))
        scored = scored[: request.top_k]
        total = len(scored)
        start = (request.page - 1) * request.page_size
        window = scored[start : start + request.page_size]
        passages = [
            Passage(
                id=passage_id(p.doc_id, p.ordinal),
                doc_id=p.doc_id,
                url=p.url,
                title=p.title,
                text=p.text,
                score=score,
                char_span=(p.start, p.end),
                ordinal=p.ordinal,
            )
            for (score, _idx, p) in window
        ]
        return SearchPageResult(
            passages=passages,
            total=total,
            page=request.page,
            page_size=request.page_size,
            has_more=start + request.page_size < total,
            backend=self.name,
        )

    def get(self, id_or_url: str) -> PageDocument | None:
        doc = self._docs.get(id_or_url)
        if doc is None:
            doc = next((d for d in self._docs.values() if d.url == id_or_url), None)
        if doc is None:
            return None
        return PageDocument(
            id=doc.id,
            url=doc.url,
            title=doc.title,
            markdown=doc.markdown,
            fetched_at=doc.fetched_at,
            content_hash=doc.content_hash,
            n_passages=doc.n_passages,
            token_estimate=doc.token_estimate,
        )

    def resolve_index(self) -> ResolveIndex:
        docs = [
            ResolveIndexEntry(
                id=self._docs[did].id,
                url=self._docs[did].url,
                title=self._docs[did].title,
                n_passages=self._docs[did].n_passages,
                fetched_at=self._docs[did].fetched_at,
                token_estimate=self._docs[did].token_estimate,
            )
            for did in self._order
        ]
        return ResolveIndex(docs=docs, total=len(docs), backend=self.name)

    def close(self) -> None:
        self._docs.clear()
        self._order.clear()
        self._passages.clear()
