"""The FORMAT pipeline: dedup, order, paginate, render, and build the sidecar.

It depends only on the FORMAT renderer port. The flow:

1. Normalize each result (derive id, site, content hash, body blocks, token estimate).
2. Dedup (byte-exact then MinHash), folding duplicates into the best-scored canonical.
3. Order the survivors by descending score (missing scores sort last, stably).
4. Paginate over the deduped set; compute the next cursor.
5. Resolve the render mode (``auto`` -> full when the page fits the token budget, else
   index) and render the layout-stable Markdown document.
6. Assemble the lossless JSON sidecar (full bodies verbatim) and, on request, the
   derived Anthropic search_result blocks.

The store is intentionally NOT required here: the sidecar is the lossless record. The
store/resolver is a separate, swappable port (a caller wires it when it wants passage
search or cross-turn resolution); keeping them decoupled mirrors the fetch/extract split.
"""

from __future__ import annotations

import uuid

from ..envelope import Envelope, ok_envelope
from . import dedup as dedup_mod
from .anthropic_blocks import to_anthropic_blocks
from .chunk import chunk_markdown
from .ids import doc_id, site_of
from .models import (
    FORMAT_CONTRACT_VERSION,
    DroppedDuplicate,
    FormatPayload,
    FormatRequest,
    FormatSidecar,
    FormattedResult,
    ResultInput,
)
from .ports import FormatRenderer
from .renderer import MarkdownRenderer
from .tokens import Estimator, estimate_tokens


def _score_key(item: ResultInput) -> tuple[int, float]:
    """Sort key: present scores first (descending); missing scores last, stably."""
    if item.score is None:
        return (0, 0.0)
    return (1, item.score)


class FormatPipeline:
    def __init__(self, renderer: FormatRenderer | None = None):
        self._renderer = renderer or MarkdownRenderer()

    def run(
        self,
        request: FormatRequest,
        *,
        token_estimator: Estimator | None = None,
        trace_id: str | None = None,
    ) -> Envelope:
        request_id = str(uuid.uuid4())
        cpt = request.chars_per_token

        def toks(text: str | None) -> int:
            return estimate_tokens(text, chars_per_token=cpt, estimator=token_estimator)

        # 1. Normalize: fill derived fields without mutating the input models.
        norm: list[ResultInput] = []
        for item in request.results:
            data = item.model_dump()
            data["id"] = item.id or doc_id(item.url)
            data["site"] = item.site or site_of(item.url)
            data["content_hash"] = item.content_hash or dedup_mod.content_hash(item.body_markdown)
            if item.token_estimate is None:
                data["token_estimate"] = toks(item.body_markdown)
            if not item.body_blocks and item.body_markdown:
                data["body_blocks"] = [t for (t, _s, _e) in chunk_markdown(item.body_markdown)]
            norm.append(ResultInput(**data))

        # 2. Dedup into canonical clusters.
        dup_items = [
            dedup_mod.DupItem(
                url=r.url,
                body=r.body_markdown or "",
                score=r.score,
                content_hash=r.content_hash or "",
                order=i,
                payload=r,
            )
            for i, r in enumerate(norm)
        ]
        clusters = dedup_mod.dedup_items(
            dup_items,
            enabled=request.dedup.enabled,
            method=request.dedup.method,
            jaccard_threshold=request.dedup.jaccard_threshold,
            num_perm=request.dedup.num_perm,
            shingle_size=request.dedup.shingle_size,
        )
        total_dropped = sum(len(c.duplicates) for c in clusters)

        # 3. Order canonicals by descending score (missing last, stable).
        ordered = sorted(clusters, key=lambda c: _score_key(c.canonical.payload), reverse=True)

        # 4. Paginate over the deduped set.
        total_results = len(ordered)
        page_size = request.page_size
        total_pages = (total_results + page_size - 1) // page_size if total_results else 0
        start = request.page * page_size
        page_clusters = ordered[start : start + page_size]
        has_more = start + page_size < total_results
        next_cursor = str(request.page + 1) if has_more else None

        # 5. Build the page's FormattedResults (rank == 1-based position in the full set).
        page_results: list[FormattedResult] = []
        for offset, cluster in enumerate(page_clusters):
            src: ResultInput = cluster.canonical.payload
            rank = start + offset + 1
            dropped = [
                DroppedDuplicate(
                    url=item.url,
                    id=doc_id(item.url),
                    similarity=sim,
                    reason=reason,
                )
                for (item, reason, sim) in cluster.duplicates
            ]
            page_results.append(
                FormattedResult(
                    id=src.id,
                    url=src.url,
                    rank=rank,
                    title=src.title,
                    published_date=src.published_date,
                    author=src.author,
                    site=src.site,
                    score=src.score,
                    lang=src.lang,
                    fetched_at=src.fetched_at,
                    page_type=src.page_type,
                    quality_score=src.quality_score,
                    highlights=src.highlights,
                    summary=src.summary,
                    body_markdown=src.body_markdown,
                    body_blocks=src.body_blocks,
                    token_estimate=src.token_estimate or 0,
                    dropped_duplicates=dropped,
                )
            )

        page_token_estimate = sum(r.token_estimate for r in page_results)

        # 6. Resolve the render mode.
        if request.mode == "auto":
            fits = page_token_estimate <= request.inline_token_budget
            resolved_mode = "full" if fits else "index"
        else:
            resolved_mode = request.mode

        # Mark per-result render state for the lossless sidecar.
        for r in page_results:
            full = (r.body_markdown or "").strip()
            if resolved_mode == "full":
                if request.body_char_budget is not None and len(full) > request.body_char_budget:
                    r.rendered_full = False
                    r.truncated_in_markdown = True
                else:
                    r.rendered_full = bool(full)
            else:
                r.rendered_full = False

        markdown = self._renderer.render(
            page_results,
            query=request.query,
            mode=resolved_mode,
            body=request.body,
            page=request.page,
            page_size=page_size,
            total_results=total_results,
            total_pages=total_pages,
            next_cursor=next_cursor,
            total_dropped_duplicates=total_dropped,
            page_token_estimate=page_token_estimate,
            body_char_budget=request.body_char_budget,
        )

        # 7. Sidecar (lossless: full bodies verbatim) and the optional Anthropic view.
        sidecar = None
        if request.include_sidecar:
            blocks: list[dict] = []
            if request.include_anthropic_blocks:
                blocks = to_anthropic_blocks(page_results, citations=request.anthropic_citations)
            sidecar = FormatSidecar(
                query=request.query,
                page=request.page,
                page_size=page_size,
                total_results=total_results,
                total_pages=total_pages,
                next_cursor=next_cursor,
                mode=resolved_mode,
                page_token_estimate=page_token_estimate,
                total_dropped_duplicates=total_dropped,
                results=page_results,
                anthropic_search_result_blocks=blocks,
            )

        warnings: list[str] = []
        if request.page >= total_pages and total_results > 0:
            warnings.append(
                f"page {request.page} is past the last page ({total_pages - 1}); no results shown."
            )

        payload = FormatPayload(
            request_id=request_id,
            markdown=markdown,
            sidecar=sidecar,
            warnings=warnings,
        )
        return ok_envelope(
            FORMAT_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="format",
            backend=self._renderer.name,
            trace_id=trace_id,
            request_id=request_id,
        )
