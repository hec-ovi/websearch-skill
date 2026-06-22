"""The default EXTRACT adapter: Trafilatura (heuristic, Apache-2.0).

Trafilatura beats neural extractors on both quality and cost (WCXB, May 2026). It
emits native Markdown and rich metadata, but it parses schema.org JSON-LD only
internally (mapping it into metadata) and never exposes the raw blocks, and it has no
word count. So this adapter parses the raw HTML once with lxml to recover the JSON-LD
blocks, og:type, and a title fallback, runs Trafilatura for the Markdown body, plain
text, and metadata, then classifies the page type and scores extraction quality.

The adapter never raises on a normal extraction failure (it returns a low-quality
result with a warning); it raises only ``DependencyMissing`` when trafilatura/lxml are
absent, which the pipeline maps to a clean error Envelope.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..exceptions import DependencyMissing
from ..models import ExtractRequest, ExtractResult
from ..ports import ExtractAdapter
from ..quality import classify_page_type, score_extraction

# Quality at or below this routes a page to a fallback (the format/store layer decides).
QUALITY_FALLBACK_THRESHOLD = 0.80


class TrafilaturaExtractor(ExtractAdapter):
    name = "trafilatura"

    def available(self) -> bool:
        try:
            import lxml  # noqa: F401
            import trafilatura  # noqa: F401
        except ImportError:
            return False
        return True

    def extract(self, request: ExtractRequest) -> ExtractResult:
        t0 = time.perf_counter()
        try:
            import trafilatura
            from trafilatura import bare_extraction
        except ImportError as exc:
            raise DependencyMissing("trafilatura", "pip install 'trafilatura>=2.1'") from exc

        warnings: list[str] = []
        if request.query:
            warnings.append(
                "query-focused extraction is not implemented for the trafilatura engine; "
                "the full page was extracted (query ignored)."
            )

        html = request.html
        jsonld, og_type, html_title, h1 = _parse_html_signals(html, warnings)

        favor_precision = request.favor == "precision"
        favor_recall = request.favor == "recall"
        common = dict(
            url=request.base_url,
            favor_precision=favor_precision,
            favor_recall=favor_recall,
            include_tables=request.include_tables,
            include_links=request.include_links,
            include_images=request.include_images,
            include_comments=request.include_comments,
            deduplicate=False,  # avoid process-global dedup suppressing repeat fetches
        )

        try:
            markdown = trafilatura.extract(html, output_format="markdown", **common)
            doc = bare_extraction(html, with_metadata=True, **common)
        except Exception as exc:  # never let the engine kill the request
            warnings.append(f"trafilatura raised {type(exc).__name__}: {exc}")
            markdown, doc = None, None

        meta: dict[str, Any] = {}
        doc_text: str | None = None
        if doc is not None:
            meta = doc.as_dict()
            doc_text = (meta.get("text") or "").strip() or None

        content_markdown = (markdown or doc_text or "").strip()
        # content_text is genuine plain text: trafilatura's serialized text keeps
        # markdown link syntax when include_links is on, so derive it from the body.
        content_text = _markdown_to_text(content_markdown) or None
        if not content_markdown:
            warnings.append("no extractable content (boilerplate-only or empty page).")

        title = meta.get("title") or html_title or h1 or None
        if title:
            title = title.strip() or None

        word_count = len((content_text or content_markdown).split())
        page_url = request.base_url or meta.get("url")
        page_type, _confidence = classify_page_type(jsonld, og_type, page_url)

        quality, _signals = score_extraction(
            raw_html_len=len(html),
            content_text=content_text or content_markdown,
            content_markdown=content_markdown,
            word_count=word_count,
            json_ld=jsonld,
            title=title,
            page_type=page_type,
        )
        if quality < QUALITY_FALLBACK_THRESHOLD:
            warnings.append(
                f"quality_score {quality:.2f} is below the {QUALITY_FALLBACK_THRESHOLD:.2f} "
                "fallback threshold; this page is a fallback candidate."
            )

        return ExtractResult(
            content_markdown=content_markdown,
            content_text=content_text,
            title=title,
            byline=(meta.get("author") or None),
            date=(meta.get("date") or None),
            language=(meta.get("language") or None),
            page_type=page_type,
            json_ld=jsonld,
            metadata=_clean_metadata(meta, og_type),
            links=_links_from_markdown(content_markdown),
            word_count=word_count,
            quality_score=quality,
            extracted_via=self.name,
            extract_ms=int((time.perf_counter() - t0) * 1000),
            warnings=warnings,
        )


def _parse_html_signals(
    html: str, warnings: list[str]
) -> tuple[list[dict[str, Any]], str | None, str | None, str | None]:
    """Recover JSON-LD blocks, og:type, <title>, and first <h1> with lxml."""
    try:
        from lxml import html as lxml_html
    except ImportError as exc:
        raise DependencyMissing("lxml", "comes with trafilatura") from exc

    jsonld: list[dict[str, Any]] = []
    og_type = html_title = h1 = None
    try:
        tree = lxml_html.fromstring(html)
    except Exception:
        warnings.append("raw HTML could not be parsed for structured signals.")
        return jsonld, og_type, html_title, h1

    for raw in tree.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            jsonld.append(obj)
        elif isinstance(obj, list):
            jsonld.extend(o for o in obj if isinstance(o, dict))

    og = tree.xpath('//meta[@property="og:type"]/@content')
    og_type = og[0] if og else None
    titles = tree.xpath("//title/text()")
    html_title = titles[0] if titles else None
    h1s = tree.xpath("//h1//text()")
    h1 = "".join(h1s).strip() if h1s else None
    return jsonld, og_type, html_title, h1


# Bracket text disallows '[' to keep these linear on adversarial '[' runs (no ReDoS).
_MD_IMG = re.compile(r"!\[([^\[\]]*)\]\([^)]*\)")
_MD_LINK_TEXT = re.compile(r"\[([^\[\]]*)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^>\s?", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*\*|\*|__|_|~~|`)")


def _markdown_to_text(markdown: str) -> str:
    """A genuine plain-text rendering: drop link/image URLs, headings, emphasis."""
    text = _MD_IMG.sub(r"\1", markdown)
    text = _MD_LINK_TEXT.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BLOCKQUOTE.sub("", text)
    text = _MD_EMPHASIS.sub("", text)
    return text.strip()


def _links_from_markdown(markdown: str) -> list[str]:
    from ..quality import _MD_LINK

    seen: set[str] = set()
    out: list[str] = []
    for m in _MD_LINK.finditer(markdown):
        url = m.group(2)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _clean_metadata(meta: dict[str, Any], og_type: str | None) -> dict[str, Any]:
    """Keep the useful, JSON-able metadata fields and drop trafilatura internals."""
    keep = ("sitename", "description", "hostname", "categories", "tags", "license", "pagetype")
    out: dict[str, Any] = {}
    for k in keep:
        v = meta.get(k)
        if v not in (None, "", [], {}):
            out[k] = v
    if og_type:
        out["og_type"] = og_type
    return out
