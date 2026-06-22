"""The default page-index adapter: SQLite FTS5 over an in-memory connection.

SQLite ships in the Python stdlib and, on the python.org and uv (python-build-standalone)
interpreters, with FTS5 compiled in, so this backend needs zero third-party packages and
gets BM25 ranking for free. FTS5 is not guaranteed on every build (a distro or self-built
SQLite may omit it), so availability is probed at runtime and the factory falls back to
the pure-Python BM25 index when it is missing.

Ranking uses ``ORDER BY bm25(passages) ASC`` (FTS5 returns negative BM25 scores, so
ascending is best-first); the score is negated to a positive number in the output.
Untrusted queries are escaped so FTS5 operators never raise a syntax error. Persistence
is the presence of ``persist_path`` (WAL is enabled for a file-backed database);
behavior is otherwise identical to the in-memory case.
"""

from __future__ import annotations

import sqlite3

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
from ._common import escape_fts5_query, prepare_doc

_NAME = "sqlite-fts5"


def fts5_available() -> bool:
    """Probe whether the local SQLite has FTS5 compiled in (authoritative, not a guess)."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
            return True
        finally:
            con.close()
    except sqlite3.OperationalError:
        return False


class SqliteFts5Index:
    name = _NAME

    def __init__(self, config: StoreConfig | None = None):
        self._config = config or StoreConfig()
        target = self._config.persist_path or ":memory:"
        self._con = sqlite3.connect(target, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        if self._config.persist_path:
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        self._con.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs (
                id TEXT PRIMARY KEY,
                url TEXT UNIQUE,
                title TEXT,
                markdown TEXT,
                fetched_at TEXT,
                content_hash TEXT,
                n_passages INTEGER,
                token_estimate INTEGER
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
                text,
                doc_id UNINDEXED,
                url UNINDEXED,
                title UNINDEXED,
                ordinal UNINDEXED,
                start_off UNINDEXED,
                end_off UNINDEXED
            );
            """
        )
        self._con.commit()

    def available(self) -> bool:
        return True

    def add(self, pages: list[PageInput]) -> AddResult:
        added: list[StoredDoc] = []
        for page in pages:
            prepared = prepare_doc(page, self._config)
            row = self._con.execute(
                "SELECT content_hash, n_passages FROM docs WHERE id = ?", (prepared.id,)
            ).fetchone()
            if row is not None and row["content_hash"] == prepared.content_hash:
                added.append(
                    StoredDoc(
                        id=prepared.id,
                        url=prepared.url,
                        title=prepared.title,
                        n_passages=row["n_passages"],
                        content_hash=prepared.content_hash,
                        fetched_at=prepared.fetched_at,
                        token_estimate=prepared.token_estimate,
                        deduped=True,
                    )
                )
                continue
            if row is not None:  # content changed: replace doc + passages
                self._con.execute("DELETE FROM passages WHERE doc_id = ?", (prepared.id,))
                self._con.execute("DELETE FROM docs WHERE id = ?", (prepared.id,))
            self._con.execute(
                "INSERT INTO docs (id, url, title, markdown, fetched_at, content_hash, "
                "n_passages, token_estimate) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    prepared.id,
                    prepared.url,
                    prepared.title,
                    prepared.markdown,
                    prepared.fetched_at,
                    prepared.content_hash,
                    len(prepared.passages),
                    prepared.token_estimate,
                ),
            )
            self._con.executemany(
                "INSERT INTO passages (text, doc_id, url, title, ordinal, start_off, end_off) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (p.text, p.doc_id, p.url, p.title, p.ordinal, p.start, p.end)
                    for p in prepared.passages
                ],
            )
            self._con.commit()
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
        return AddResult(added=added)

    def search(self, request: SearchPageRequest) -> SearchPageResult:
        match = escape_fts5_query(request.query)
        if match is None:
            return SearchPageResult(
                passages=[],
                total=0,
                page=request.page,
                page_size=request.page_size,
                has_more=False,
                backend=self.name,
            )
        rows = self._con.execute(
            "SELECT text, doc_id, url, title, ordinal, start_off, end_off, "
            "bm25(passages) AS rank FROM passages WHERE passages MATCH ? "
            "ORDER BY rank ASC LIMIT ?",
            (match, request.top_k),
        ).fetchall()
        total = len(rows)
        start = (request.page - 1) * request.page_size
        window = rows[start : start + request.page_size]
        passages = [
            Passage(
                id=passage_id(r["doc_id"], r["ordinal"]),
                doc_id=r["doc_id"],
                url=r["url"],
                title=r["title"],
                text=r["text"],
                score=-float(r["rank"]),  # FTS5 bm25 is negative; positive = better
                char_span=(int(r["start_off"]), int(r["end_off"])),
                ordinal=int(r["ordinal"]),
            )
            for r in window
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
        row = self._con.execute(
            "SELECT id, url, title, markdown, fetched_at, content_hash, n_passages, "
            "token_estimate FROM docs WHERE id = ? OR url = ? LIMIT 1",
            (id_or_url, id_or_url),
        ).fetchone()
        if row is None:
            return None
        return PageDocument(
            id=row["id"],
            url=row["url"],
            title=row["title"],
            markdown=row["markdown"],
            fetched_at=row["fetched_at"],
            content_hash=row["content_hash"],
            n_passages=row["n_passages"] or 0,
            token_estimate=row["token_estimate"] or 0,
        )

    def resolve_index(self) -> ResolveIndex:
        rows = self._con.execute(
            "SELECT id, url, title, n_passages, fetched_at, token_estimate FROM docs ORDER BY rowid"
        ).fetchall()
        docs = [
            ResolveIndexEntry(
                id=r["id"],
                url=r["url"],
                title=r["title"],
                n_passages=r["n_passages"] or 0,
                fetched_at=r["fetched_at"],
                token_estimate=r["token_estimate"] or 0,
            )
            for r in rows
        ]
        return ResolveIndex(docs=docs, total=len(docs), backend=self.name)

    def close(self) -> None:
        self._con.close()
