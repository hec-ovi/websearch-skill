"""The Layer 2A pipeline: fetch (with tier escalation) then extract.

It depends only on the FETCH and EXTRACT ports, assembles the agent-facing
ExtractPayload, and is the single place that turns the two sub-ports' outcomes into a
success or error Envelope. A transport failure (no HTTP response) is the only fetch
outcome that becomes an error; a 404 or a blocked challenge still returns content with
the situation surfaced in ``source`` and ``warnings`` so the agent can decide.
"""

from __future__ import annotations

import time
import uuid

from pydantic import ValidationError

from .. import errors
from ..envelope import Envelope, error_envelope, ok_envelope
from .exceptions import DependencyMissing
from .fetch_router import FetchRouter
from .models import (
    EXTRACT_CONTRACT_VERSION,
    ExtractPayload,
    ExtractRequest,
    ExtractResult,
    ExtractSource,
    ExtractTiming,
    FetchRequest,
)
from .ports import ExtractAdapter

_DEFAULT_ENGINES = {"trafilatura", "auto"}


def _is_extractable(content_type: str | None) -> bool:
    """Whether the HTML extractor should be run on this content type."""
    if not content_type:
        return True  # unknown type: attempt extraction
    ct = content_type.lower()
    return "html" in ct or "xml" in ct or ct.startswith("text/")


class FetchExtractPipeline:
    def __init__(self, fetch_router: FetchRouter, extractor: ExtractAdapter):
        self._fetch_router = fetch_router
        self._extractor = extractor

    def run(
        self,
        fetch_request: FetchRequest,
        *,
        extract_overrides: dict | None = None,
    ) -> Envelope:
        t0 = time.perf_counter()
        trace_id = uuid.uuid4().hex
        request_id = str(uuid.uuid4())
        overrides = dict(extract_overrides or {})

        def elapsed_ms() -> float:
            return (time.perf_counter() - t0) * 1000

        try:
            fr = self._fetch_router.fetch(fetch_request)
        except DependencyMissing as exc:
            return self._dep_error(exc, request_id, trace_id, elapsed_ms())

        if fr.status == 0 and not fr.ok:
            # A permanent refusal (egress policy, redirect loop, missing optional dependency)
            # is NOT retriable; a transport error or timeout is. Prefer the fetcher's
            # structured failure_kind; fall back to the legacy text check only if it is unset.
            non_retriable_kinds = {"egress_refused", "redirect_loop", "dependency_missing"}
            if fr.failure_kind is not None:
                retriable = fr.failure_kind not in non_retriable_kinds
            else:
                retriable = not (fr.error and ("not installed" in fr.error or "opt-in" in fr.error))
            return error_envelope(
                EXTRACT_CONTRACT_VERSION,
                code=errors.FETCH_FAILED,
                message=fr.error or "the fetch produced no response.",
                retriable=retriable,
                layer="extract",
                backend=fr.fetched_via,
                elapsed_ms=elapsed_ms(),
                trace_id=trace_id,
                request_id=request_id,
            )

        warnings: list[str] = []
        requested_engine = overrides.get("engine", "trafilatura")
        if requested_engine not in _DEFAULT_ENGINES:
            warnings.append(
                f"extract engine '{requested_engine}' is an opt-in adapter not installed in "
                f"this build; used '{self._extractor.name}' instead."
            )
            overrides.pop("engine", None)
        # Never let an override collide with the fields the pipeline owns.
        overrides.pop("html", None)
        overrides.pop("base_url", None)

        if not _is_extractable(fr.content_type):
            warnings.append(
                f"content type '{fr.content_type}' is not HTML/text; extraction was skipped."
            )
            result = ExtractResult(
                content_markdown="",
                page_type="unknown",
                quality_score=0.0,
                extracted_via="none",
                warnings=[f"non-HTML document ({fr.content_type})"],
            )
        else:
            try:
                extract_request = ExtractRequest(
                    # Prefer the post-JS DOM when a rendering tier filled it; fall back to the
                    # raw body. A browser tier that only sets rendered_html would otherwise
                    # extract an empty raw_html.
                    html=fr.rendered_html or fr.raw_html or "",
                    base_url=fr.final_url or fetch_request.url,
                    **overrides,
                )
            except ValidationError as exc:
                # A bad extract_override (e.g. favor='bogus', or an unknown key) must not
                # crash run(); it is a caller error, reported precisely as INVALID_REQUEST.
                return error_envelope(
                    EXTRACT_CONTRACT_VERSION,
                    code=errors.INVALID_REQUEST,
                    message=f"invalid extract overrides: {exc}",
                    retriable=False,
                    layer="extract",
                    backend=self._extractor.name,
                    elapsed_ms=elapsed_ms(),
                    trace_id=trace_id,
                    request_id=request_id,
                )
            try:
                result = self._extractor.extract(extract_request)
            except DependencyMissing as exc:
                return self._dep_error(exc, request_id, trace_id, elapsed_ms())
            except Exception as exc:  # extractor should not raise; never leak a traceback
                return error_envelope(
                    EXTRACT_CONTRACT_VERSION,
                    code=errors.EXTRACT_FAILED,
                    message=f"extraction failed: {type(exc).__name__}: {exc}",
                    retriable=False,
                    layer="extract",
                    backend=self._extractor.name,
                    elapsed_ms=elapsed_ms(),
                    trace_id=trace_id,
                    request_id=request_id,
                )

        if fr.blocked:
            warnings.append(
                f"fetch was blocked ({fr.block_reason}); the content may be a challenge or "
                "interstitial page rather than the real document."
            )
        if fr.status >= 400:
            warnings.append(f"fetch returned HTTP {fr.status}.")
        if fetch_request.screenshot and fr.screenshot_b64 is None:
            warnings.append("screenshot requires a browser tier (opt-in); none was captured.")

        source = ExtractSource(
            url=fetch_request.url,
            final_url=fr.final_url,
            status=fr.status,
            ok=fr.ok,
            fetched_via=fr.fetched_via,
            content_type=fr.content_type,
            blocked=fr.blocked,
            block_reason=fr.block_reason,
            tier_attempts=fr.tier_attempts,
            fetch_ms=fr.fetch_ms,
        )
        payload = ExtractPayload(
            request_id=request_id,
            source=source,
            result=result,
            timing=ExtractTiming(
                total_ms=int(elapsed_ms()), fetch_ms=fr.fetch_ms, extract_ms=result.extract_ms
            ),
            warnings=warnings,
        )
        return ok_envelope(
            EXTRACT_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="extract",
            backend=fr.fetched_via,
            elapsed_ms=elapsed_ms(),
            trace_id=trace_id,
            request_id=request_id,
        )

    def _dep_error(
        self, exc: DependencyMissing, request_id: str, trace_id: str, ms: float
    ) -> Envelope:
        return error_envelope(
            EXTRACT_CONTRACT_VERSION,
            code=errors.DEPENDENCY_MISSING,
            message=str(exc),
            retriable=False,
            layer="extract",
            backend=None,
            elapsed_ms=ms,
            trace_id=trace_id,
            request_id=request_id,
        )
