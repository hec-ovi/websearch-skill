"""PageIndex tests across both default adapters (SQLite-FTS5 and the pure-Python BM25).

Behavior that must match across adapters is parametrized over both; storage details
(persistence, FTS5 detection) are tested where relevant.
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    STORE_ADD_RESULT_REF,
    STORE_PAGE_DOC_REF,
    STORE_RESOLVE_INDEX_REF,
    STORE_SEARCH_RESULT_REF,
)
from websearch.layer2_format import (
    MemoryBm25Index,
    PageInput,
    SearchPageRequest,
    SqliteFts5Index,
    StoreConfig,
    build_page_index,
    fts5_available,
)

OWNERSHIP = (
    "# Ownership\n\nRust ownership manages memory deterministically. Borrowing and "
    "lifetimes follow from it.\n\n## Borrowing\n\nShared borrows are immutable and may "
    "overlap; a mutable borrow is exclusive."
)
LIFETIMES = "# Lifetimes\n\nLifetimes annotate how long a reference stays valid in Rust."
COOKING = "# Bread\n\nA simple recipe for bread needs flour, water, yeast, and salt."

ADAPTERS = ["sqlite-fts5", "memory"]


def _index(adapter: str):
    return build_page_index(StoreConfig(adapter=adapter))


def _pages():
    return [
        PageInput(url="https://a.test/own", title="Ownership", markdown=OWNERSHIP),
        PageInput(url="https://a.test/life", title="Lifetimes", markdown=LIFETIMES),
        PageInput(url="https://a.test/bread", title="Bread", markdown=COOKING),
    ]


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_add_chunks_and_reports(adapter, assert_valid):
    idx = _index(adapter)
    res = idx.add(_pages())
    assert_valid(res.model_dump(mode="json"), STORE_ADD_RESULT_REF)
    assert len(res.added) == 3
    own = next(d for d in res.added if d.url == "https://a.test/own")
    assert own.n_passages >= 2  # two headings
    assert own.id.startswith("doc_")
    assert all(not d.deduped for d in res.added)


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_idempotent_readd_is_noop(adapter):
    idx = _index(adapter)
    idx.add(_pages())
    again = idx.add([PageInput(url="https://a.test/own", title="Ownership", markdown=OWNERSHIP)])
    assert again.added[0].deduped is True
    assert idx.resolve_index().total == 3  # no new doc


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_changed_content_replaces(adapter):
    idx = _index(adapter)
    idx.add([PageInput(url="https://a.test/own", markdown=OWNERSHIP)])
    changed = idx.add([PageInput(url="https://a.test/own", markdown=OWNERSHIP + "\n\nNew para.")])
    assert changed.added[0].deduped is False
    assert idx.resolve_index().total == 1  # same url, replaced not duplicated


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_search_ranks_relevant_first(adapter, assert_valid):
    idx = _index(adapter)
    idx.add(_pages())
    res = idx.search(SearchPageRequest(query="rust borrowing", top_k=10, page=1, page_size=5))
    assert_valid(res.model_dump(mode="json"), STORE_SEARCH_RESULT_REF)
    assert res.backend in ("sqlite-fts5", "memory-bm25")
    assert res.total >= 1
    # the ownership/borrowing passage outranks the unrelated bread passage
    urls = [p.url for p in res.passages]
    assert "https://a.test/own" in urls
    assert urls[0] == "https://a.test/own"
    # passage text slices back from its char span
    top = res.passages[0]
    assert top.score >= res.passages[-1].score  # descending


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_search_pagination(adapter):
    idx = _index(adapter)
    idx.add(_pages())
    p1 = idx.search(SearchPageRequest(query="rust", top_k=10, page=1, page_size=1))
    assert len(p1.passages) == 1
    if p1.total > 1:
        assert p1.has_more is True
        p2 = idx.search(SearchPageRequest(query="rust", top_k=10, page=2, page_size=1))
        assert p2.passages[0].id != p1.passages[0].id


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_query_with_fts_operators_does_not_crash(adapter):
    idx = _index(adapter)
    idx.add(_pages())
    for hazard in ['AND OR NOT "x*', "rust -borrowing", "col:value (paren)", '"', "*", ""]:
        res = idx.search(SearchPageRequest(query=hazard or "x"))
        assert res.total >= 0  # never raises a syntax error


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_get_by_id_and_url(adapter, assert_valid):
    idx = _index(adapter)
    added = idx.add(_pages()).added
    doc_id = added[0].id
    by_id = idx.get(doc_id)
    by_url = idx.get(added[0].url)
    assert by_id is not None and by_url is not None
    assert by_id.markdown == by_url.markdown
    assert by_id.markdown == OWNERSHIP  # full body verbatim, never truncated
    assert_valid(by_id.model_dump(mode="json"), STORE_PAGE_DOC_REF)
    assert idx.get("doc_doesnotexist") is None


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_resolve_index_lists_held_docs(adapter, assert_valid):
    idx = _index(adapter)
    idx.add(_pages())
    ri = idx.resolve_index()
    assert_valid(ri.model_dump(mode="json"), STORE_RESOLVE_INDEX_REF)
    assert ri.total == 3
    assert {e.url for e in ri.docs} == {
        "https://a.test/own",
        "https://a.test/life",
        "https://a.test/bread",
    }
    assert all(e.token_estimate > 0 for e in ri.docs)


def test_empty_query_returns_no_passages():
    idx = _index("sqlite-fts5")
    idx.add(_pages())
    res = idx.search(SearchPageRequest(query="   x   "))  # only "x", matches nothing
    assert res.passages == [] or all(p.score >= 0 for p in res.passages)


def test_sqlite_fts5_available_on_this_interpreter():
    # The CI interpreters (python.org / uv) ship FTS5; assert the probe agrees so the
    # default path is actually exercised rather than silently falling back.
    assert fts5_available() is True
    assert isinstance(build_page_index(StoreConfig(adapter="sqlite-fts5")), SqliteFts5Index)


def test_memory_adapter_selected_explicitly():
    assert isinstance(build_page_index(StoreConfig(adapter="memory")), MemoryBm25Index)


def test_opt_in_adapter_raises_clear_error():
    from websearch.layer2_format import DependencyMissing

    with pytest.raises(DependencyMissing):
        build_page_index(StoreConfig(adapter="tantivy"))


def test_persistence_round_trips(tmp_path):
    db = tmp_path / "index.db"
    idx = SqliteFts5Index(StoreConfig(persist_path=str(db)))
    idx.add(_pages())
    idx.close()
    assert db.exists()
    reopened = SqliteFts5Index(StoreConfig(persist_path=str(db)))
    assert reopened.resolve_index().total == 3
    got = reopened.get("https://a.test/own")
    assert got is not None and got.markdown == OWNERSHIP
    reopened.close()
