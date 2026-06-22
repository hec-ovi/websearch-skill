"""Keyless arXiv search over the official export.arxiv.org Atom API.

No API key. One GET per call (GET benefits from arXiv's Fastly cache, which 429s
far less than POST), with exponential backoff on HTTP 429 per the 2026 throttling.
The HTTP boundary is injectable (``http_get``) so tests feed canned Atom XML rather
than hitting the network, mirroring the ddgs/curl_cffi fakes elsewhere.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC
from typing import Any

from pydantic import ValidationError

from ..envelope import Envelope, error_envelope, ok_envelope
from ..errors import RATE_LIMITED, UPSTREAM_ERROR
from .models import ARXIV_CONTRACT_VERSION, ArxivPaper, ArxivSearchPayload, ArxivSearchRequest

ENDPOINT = "https://export.arxiv.org/api/query"
USER_AGENT = "websearch-skill (+https://github.com/hec-ovi/websearch-skill)"
_LAYER = "arxiv"
_BACKEND = "arxiv-api"

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# http_get(url, *, params, headers, timeout_s) -> response with .status_code/.headers/.text
HttpGet = Callable[..., Any]


def _httpx_get(url: str, *, params: dict, headers: dict, timeout_s: float) -> Any:
    import httpx

    return httpx.get(url, params=params, headers=headers, timeout=timeout_s, follow_redirects=True)


def _header(resp: Any, name: str) -> str | None:
    """Case-insensitive header read that tolerates dict or httpx.Headers."""
    headers = getattr(resp, "headers", {}) or {}
    try:
        val = headers.get(name)
        if val is not None:
            return str(val)
    except AttributeError:
        pass
    for key, val in dict(headers).items():
        if key.lower() == name.lower():
            return str(val)
    return None


def _http_date_seconds(value: str) -> float | None:
    """Seconds from now until an HTTP-date (RFC 7231 Retry-After form), or None."""
    from datetime import datetime
    from email.utils import parsedate_to_datetime

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=UTC)
    return max(0.0, (target - datetime.now(UTC)).total_seconds())


def _https(url: str) -> str:
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _el_text(parent: ET.Element, path: str) -> str:
    el = parent.find(path, _NS)
    return (el.text or "").strip() if el is not None and el.text else ""


def _parse_entry(entry: ET.Element) -> ArxivPaper | None:
    id_text = _el_text(entry, "atom:id")
    # Real arXiv papers always carry an /abs/ id. Error sentinels (id under /api/errors)
    # and any other non-paper entry are rejected here rather than becoming fake papers.
    if "/abs/" not in id_text:
        return None
    arxiv_id = id_text.rsplit("/abs/", 1)[-1]
    title = " ".join(_el_text(entry, "atom:title").split())
    # arXiv hard-wraps abstracts; collapse the artifact newlines into single spaces.
    summary = " ".join(_el_text(entry, "atom:summary").split())
    published = _el_text(entry, "atom:published") or None
    updated = _el_text(entry, "atom:updated") or None
    authors = [
        name
        for a in entry.findall("atom:author", _NS)
        if (
            name := (a.find("atom:name", _NS).text or "").strip()
            if a.find("atom:name", _NS) is not None
            else ""
        )
    ]

    abs_url = _https(id_text)
    pdf_url: str | None = None
    for link in entry.findall("atom:link", _NS):
        href = link.get("href")
        if not href:
            continue
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = _https(href)
        elif link.get("rel") == "alternate":
            abs_url = _https(href)

    prim = entry.find("arxiv:primary_category", _NS)
    primary_category = prim.get("term") if prim is not None else None
    categories = [c.get("term") for c in entry.findall("atom:category", _NS) if c.get("term")]
    comment = _el_text(entry, "arxiv:comment") or None
    doi = _el_text(entry, "arxiv:doi") or None

    if not (arxiv_id and title and abs_url):
        return None
    return ArxivPaper(
        arxiv_id=arxiv_id,
        title=title,
        authors=authors,
        summary=summary,
        published=published,
        updated=updated,
        abs_url=abs_url,
        pdf_url=pdf_url,
        primary_category=primary_category,
        categories=categories,
        comment=comment,
        doi=doi,
    )


def _parse_feed(xml_text: str) -> tuple[int | None, list[ArxivPaper], list[str]]:
    root = ET.fromstring(xml_text)
    total_el = root.find("opensearch:totalResults", _NS)
    total = None
    if total_el is not None and total_el.text and total_el.text.strip().isdigit():
        total = int(total_el.text.strip())
    papers: list[ArxivPaper] = []
    warnings: list[str] = []
    for entry in root.findall("atom:entry", _NS):
        id_text = _el_text(entry, "atom:id")
        if "/api/errors" in id_text:
            # arXiv signals a bad query with an HTTP-200 feed carrying one error entry.
            msg = " ".join(_el_text(entry, "atom:summary").split()) or _el_text(entry, "atom:title")
            warnings.append(f"arXiv rejected the query: {msg}".strip())
            continue
        if paper := _parse_entry(entry):
            papers.append(paper)
    return total, papers, warnings


class ArxivTool:
    """The arXiv search port. Swap ``http_get`` to retarget the transport."""

    def __init__(
        self,
        *,
        http_get: HttpGet,
        sleep: Callable[[float], None],
        max_retries: int = 3,
        base_backoff_s: float = 3.0,
        timeout_s: float = 20.0,
    ):
        self._http_get = http_get
        self._sleep = sleep
        self._max_retries = max_retries
        self._base_backoff_s = base_backoff_s
        self._timeout_s = timeout_s

    def _error(self, *, code: str, message: str, retriable: bool, elapsed_ms: float) -> Envelope:
        return error_envelope(
            ARXIV_CONTRACT_VERSION,
            code=code,
            message=message,
            retriable=retriable,
            layer=_LAYER,
            backend=_BACKEND,
            elapsed_ms=elapsed_ms,
        )

    def search(self, request: ArxivSearchRequest) -> Envelope:
        params = {
            "search_query": request.search_query(),
            "start": request.start,
            "max_results": request.max_results,
            "sortBy": request.sort_by,
            "sortOrder": request.sort_order,
        }
        headers = {"User-Agent": USER_AGENT, "Accept": "application/atom+xml"}
        t0 = time.perf_counter()

        attempt = 0
        while True:
            try:
                resp = self._http_get(
                    ENDPOINT, params=params, headers=headers, timeout_s=self._timeout_s
                )
            except Exception as exc:  # network/transport failure
                return self._error(
                    code=UPSTREAM_ERROR,
                    message=f"arXiv request failed: {type(exc).__name__}: {exc}",
                    retriable=True,
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
            status = int(getattr(resp, "status_code", 0))
            if status == 429:
                if attempt < self._max_retries:
                    self._sleep(self._backoff(attempt, resp))
                    attempt += 1
                    continue
                ra = _header(resp, "retry-after")
                suffix = f"; retry after {ra}s" if ra else ""
                return self._error(
                    code=RATE_LIMITED,
                    message=f"arXiv rate-limited (HTTP 429){suffix}. Space requests >=3s apart.",
                    retriable=True,
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
            if status >= 400:
                return self._error(
                    code=UPSTREAM_ERROR,
                    message=f"arXiv returned HTTP {status}.",
                    retriable=status >= 500,
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
            break

        try:
            total, papers, warnings = _parse_feed(getattr(resp, "text", "") or "")
        except ET.ParseError as exc:
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"arXiv response was not valid Atom XML: {exc}",
                retriable=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )
        except (ValidationError, ValueError, TypeError) as exc:
            # A malformed entry that slips past _parse_entry's guards (e.g. an ArxivPaper
            # field of the wrong type) must surface as a clean error, never a traceback.
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"arXiv response had an unexpected shape: {type(exc).__name__}: {exc}",
                retriable=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        payload = ArxivSearchPayload(
            query=params["search_query"],
            total_results=total,
            start=request.start,
            papers=papers,
            warnings=warnings,
        )
        return ok_envelope(
            ARXIV_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer=_LAYER,
            backend=_BACKEND,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            endpoint=ENDPOINT,
        )

    def _backoff(self, attempt: int, resp: Any) -> float:
        ra = _header(resp, "retry-after")
        if ra:
            ra = ra.strip()
            if ra.isdigit():
                return float(ra)
            # RFC 7231 also permits an HTTP-date; honor it, clamped so a far-future
            # date cannot stall the call.
            secs = _http_date_seconds(ra)
            if secs is not None:
                return min(secs, 60.0)
        return self._base_backoff_s * (2**attempt)


def build_arxiv_tool(
    *,
    http_get: HttpGet | None = None,
    sleep: Callable[[float], None] | None = None,
    max_retries: int = 3,
    base_backoff_s: float = 3.0,
    timeout_s: float = 20.0,
) -> ArxivTool:
    return ArxivTool(
        http_get=http_get or _httpx_get,
        sleep=sleep or time.sleep,
        max_retries=max_retries,
        base_backoff_s=base_backoff_s,
        timeout_s=timeout_s,
    )
