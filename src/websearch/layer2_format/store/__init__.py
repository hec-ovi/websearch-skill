"""The STORE sub-port: an ephemeral page index behind the PageIndex contract.

``build_page_index`` resolves the configured adapter. The default ``sqlite-fts5`` falls
back to the pure-Python ``memory`` BM25 index when FTS5 is not compiled into the local
SQLite, so the default install always works with zero third-party packages. ``sqlite-vec``
and ``tantivy`` are named in the contract enum but are opt-in and not part of the base
install; requesting them without the adapter installed raises a clear error.
"""

from __future__ import annotations

from ..exceptions import DependencyMissing
from ..models import StoreConfig
from ..ports import PageIndex
from .memory_bm25 import MemoryBm25Index
from .sqlite_fts5 import SqliteFts5Index, fts5_available

__all__ = [
    "PageIndex",
    "SqliteFts5Index",
    "MemoryBm25Index",
    "fts5_available",
    "build_page_index",
]


def build_page_index(config: StoreConfig | None = None) -> PageIndex:
    """Construct the page index for ``config``, with a graceful FTS5 fallback."""
    config = config or StoreConfig()
    if config.adapter == "memory":
        return MemoryBm25Index(config)
    if config.adapter == "sqlite-fts5":
        if fts5_available():
            return SqliteFts5Index(config)
        return MemoryBm25Index(config)
    raise DependencyMissing(
        f"page-index adapter '{config.adapter}' is an opt-in backend not installed in this "
        "build; use 'sqlite-fts5' (default) or 'memory'."
    )
