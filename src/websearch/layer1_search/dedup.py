"""Deterministic dedup + provenance merge.

Results from all engines are keyed by canonical URL. When two engines return the
same canonical URL they merge into one ``DedupedDoc`` that accumulates one
``DocSource`` per engine occurrence, so fusion has full rank provenance to
de-correlate engines downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .canonical import canonicalize_url
from .port import RawResult


@dataclass
class DocSource:
    engine: str
    group: str  # correlation_group of the engine
    rank: int
    raw_score: float | None = None
    native_id: str | None = None


@dataclass
class DedupedDoc:
    url: str  # canonical; the dedup key and Layer-2 handoff token
    display_url: str  # first-seen original
    title: str
    snippet: str
    snippets: list[str] = field(default_factory=list)
    published_date: str | None = None
    result_type: str = "web"
    favicon: str | None = None
    thumbnail: str | None = None
    sources: list[DocSource] = field(default_factory=list)


def dedupe(tagged: list[tuple[str, str, RawResult]]) -> list[DedupedDoc]:
    """Merge raw results into deduped docs.

    ``tagged`` is a list of ``(engine_name, correlation_group, RawResult)`` in a
    deterministic order (the router's engine order, each engine by ascending rank).
    Insertion order of first-seen canonical URLs is preserved.
    """
    docs: dict[str, DedupedDoc] = {}

    for engine, group, r in tagged:
        canonical = canonicalize_url(r.url)
        if not canonical:
            continue
        src = DocSource(
            engine=engine,
            group=group,
            rank=r.rank,
            raw_score=r.raw_score,
            native_id=r.native_id,
        )
        existing = docs.get(canonical)
        if existing is None:
            docs[canonical] = DedupedDoc(
                url=canonical,
                display_url=r.url,
                title=r.title,
                snippet=r.snippet,
                snippets=[r.snippet] if r.snippet else [],
                published_date=r.published_date,
                result_type=r.result_type,
                favicon=r.favicon,
                thumbnail=r.thumbnail,
                sources=[src],
            )
            continue

        existing.sources.append(src)
        # Keep the longest snippet as primary; collect all distinct snippets.
        if r.snippet and r.snippet not in existing.snippets:
            existing.snippets.append(r.snippet)
        if r.snippet and len(r.snippet) > len(existing.snippet):
            existing.snippet = r.snippet
        # Title from the best-ranked source seen so far.
        best_rank = min(s.rank for s in existing.sources[:-1])
        if r.rank < best_rank and r.title:
            existing.title = r.title
        # Fill missing optional fields from later engines.
        if existing.published_date is None and r.published_date is not None:
            existing.published_date = r.published_date
        if existing.favicon is None and r.favicon is not None:
            existing.favicon = r.favicon
        if existing.thumbnail is None and r.thumbnail is not None:
            existing.thumbnail = r.thumbnail

    return list(docs.values())
