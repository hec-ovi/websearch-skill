"""The Layer-1 router: fan out to adapters, then canonicalize, dedup, and fuse.

The router depends only on the ``EngineAdapter`` port. It fans out concurrently,
tolerates per-engine failure (recording it in ``unresponsive_engines`` rather than
failing the whole request), and only returns an error Envelope when every selected
engine fails.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .. import errors
from ..envelope import Envelope, error_envelope, ok_envelope
from .dedup import DedupedDoc, dedupe
from .fusion import fuse
from .models import (
    SEARCH_CONTRACT_VERSION,
    ResultItem,
    SearchPayload,
    SearchRequest,
    SourceProvenance,
    Timing,
    UnresponsiveEngine,
)
from .port import EngineAdapter, EngineOutput


def _dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def _site_match(host: str, sites: list[str]) -> bool:
    h = host.lower()
    for s in sites:
        s = s.strip().lower().lstrip(".")
        if s.startswith("www."):
            s = s[4:]
        if h == s or h.endswith("." + s):
            return True
    return False


class SearchRouter:
    def __init__(self, adapters: list[EngineAdapter]):
        self._adapters = list(adapters)

    @property
    def adapters(self) -> list[EngineAdapter]:
        return list(self._adapters)

    def _select(self, request: SearchRequest) -> list[EngineAdapter]:
        enabled = [a for a in self._adapters if a.enabled()]
        if request.engines is None:
            return enabled
        wanted = list(request.engines)
        by_name = {a.name: a for a in enabled}
        # Preserve the caller's requested order, skipping unknown/disabled engines.
        return [by_name[n] for n in wanted if n in by_name]

    def _run_one(self, adapter: EngineAdapter, request: SearchRequest) -> EngineOutput:
        start = time.perf_counter()
        try:
            out = adapter.search(request)
        except Exception as exc:  # adapters shouldn't raise, but never let one kill fan-out
            return EngineOutput(
                engine=adapter.name,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.perf_counter() - start) * 1000),
            )
        if not out.elapsed_ms:
            out.elapsed_ms = int((time.perf_counter() - start) * 1000)
        return out

    def _fusion_warnings(self, selected: list[EngineAdapter]) -> list[str]:
        groups: dict[str, list[str]] = {}
        for a in selected:
            groups.setdefault(a.correlation_group, []).append(a.name)
        warnings: list[str] = []
        for group, names in groups.items():
            if len(names) > 1:
                warnings.append(
                    f"Engines {sorted(names)} share correlation group '{group}'; their "
                    "agreement was de-correlated (counted as one independent vote) in fusion."
                )
        return warnings

    def _filter_sites(self, docs: list[DedupedDoc], request: SearchRequest) -> list[DedupedDoc]:
        if not request.include_sites and not request.exclude_sites:
            return docs
        out: list[DedupedDoc] = []
        for d in docs:
            host = _host(d.url)
            if request.exclude_sites and _site_match(host, request.exclude_sites):
                continue
            if request.include_sites and not _site_match(host, request.include_sites):
                continue
            out.append(d)
        return out

    def search(self, request: SearchRequest) -> Envelope:
        t0 = time.perf_counter()
        trace_id = uuid.uuid4().hex
        request_id = str(uuid.uuid4())
        selected = self._select(request)
        backend_id = "+".join(a.name for a in selected) or None

        if not selected:
            return error_envelope(
                SEARCH_CONTRACT_VERSION,
                code=errors.NO_ENGINES_ENABLED,
                message="No search engines enabled or matched the requested set.",
                retriable=False,
                layer="search",
                backend=backend_id,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                trace_id=trace_id,
                request_id=request_id,
            )

        outputs: dict[str, EngineOutput] = {}
        timeout_s = request.timeout_ms / 1000.0
        with ThreadPoolExecutor(max_workers=len(selected)) as ex:
            futures = {ex.submit(self._run_one, a, request): a for a in selected}
            for fut in list(futures):
                adapter = futures[fut]
                try:
                    outputs[adapter.name] = fut.result(timeout=timeout_s + 2.0)
                except Exception as exc:
                    outputs[adapter.name] = EngineOutput(
                        engine=adapter.name, error=f"timeout_or_error: {exc}"
                    )

        group_of = {a.name: a.correlation_group for a in selected}
        tagged: list[tuple[str, str, object]] = []
        answers: list[str] = []
        suggestions: list[str] = []
        corrections: list[str] = []
        unresponsive: list[UnresponsiveEngine] = []
        per_engine_ms: dict[str, int] = {}
        responded: list[str] = []

        for a in selected:
            out = outputs.get(a.name)
            if out is None:
                unresponsive.append(UnresponsiveEngine(engine=a.name, reason="no_output"))
                continue
            per_engine_ms[a.name] = out.elapsed_ms
            if out.error:
                unresponsive.append(UnresponsiveEngine(engine=a.name, reason=out.error))
                continue
            responded.append(a.name)
            for r in out.results:
                tagged.append((a.name, group_of[a.name], r))
            answers.extend(out.answers)
            suggestions.extend(out.suggestions)
            corrections.extend(out.corrections)

        if not responded:
            return error_envelope(
                SEARCH_CONTRACT_VERSION,
                code=errors.ALL_ENGINES_FAILED,
                message="All selected engines failed or returned no response.",
                retriable=True,
                layer="search",
                backend=backend_id,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
                trace_id=trace_id,
                request_id=request_id,
            )

        docs = self._filter_sites(dedupe(tagged), request)  # type: ignore[arg-type]
        scored = fuse(docs, request.fusion)[: request.max_total_results]

        results = [
            ResultItem(
                url=doc.url,
                display_url=doc.display_url,
                title=doc.title,
                snippet=doc.snippet,
                snippets=doc.snippets,
                published_date=doc.published_date,
                fused_score=score,
                sources=[
                    SourceProvenance(
                        engine=s.engine, rank=s.rank, raw_score=s.raw_score, native_id=s.native_id
                    )
                    for s in doc.sources
                ],
                result_type=doc.result_type if doc.result_type in ("web", "news") else "web",
                favicon=doc.favicon,
                thumbnail=doc.thumbnail,
                language=request.language,
            )
            for doc, score in scored
        ]

        warnings = self._fusion_warnings(selected)
        if request.fusion.method == "score_convex":
            warnings.append(
                "fusion.method 'score_convex' is not implemented yet; used weighted_rrf instead."
            )

        payload = SearchPayload(
            query=request.query,
            request_id=request_id,
            results=results,
            answers=_dedup_keep_order(answers),
            suggestions=_dedup_keep_order(suggestions),
            corrections=_dedup_keep_order(corrections),
            engines_queried=[a.name for a in selected],
            unresponsive_engines=unresponsive,
            timing=Timing(
                total_ms=int((time.perf_counter() - t0) * 1000), per_engine_ms=per_engine_ms
            ),
            warnings=warnings,
        )
        return ok_envelope(
            SEARCH_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="search",
            backend="+".join(responded),
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            trace_id=trace_id,
        )
