"""Extraction quality scoring and cheap page-type classification.

No gold labels: ``quality_score`` is a weighted mean of runtime signals (text
density, word-count saturation, paragraph count, inverse link density, JSON-LD
presence, clean title), with hard vetoes that pull soft-404s and shells down. Below
~0.80 a page is a fallback candidate (the format/store layer decides whether to spend
on a neural/structured fallback). ``classify_page_type`` resolves JSON-LD @type, then
og:type, then URL shape; first confident hit wins.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .blocks import title_looks_like_error
from .models import PageType

# Bracket text disallows '[' so a long run of '[' cannot cause O(n^2) backtracking.
_MD_LINK = re.compile(r"\[([^\[\]]*)\]\((https?://[^)\s]+)\)")

_WEIGHTS = {
    "text_density": 0.25,
    "word_count": 0.25,
    "paragraph": 0.10,
    "link_ratio": 0.15,
    "json_ld": 0.10,
    "title": 0.15,
}

# Article-class structured data is the strongest positive (full credit). Entity-class
# (product/forum/listing/event) gets partial credit so a thin product or forum page is
# not lifted over the 0.80 gate the way a real article is (research: articles saturate
# ~0.93; products ~0.67, listings ~0.70, forums ~0.79).
_ARTICLE_JSONLD_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "techarticle",
    "report",
    "recipe",
    "howto",
    "liveblogposting",
}
_ENTITY_JSONLD_TYPES = {
    "product",
    "productgroup",
    "offer",
    "aggregateoffer",
    "qapage",
    "discussionforumposting",
    "event",
    "jobposting",
    "itemlist",
    "collectionpage",
}

# Page types where link density is legitimate, so the inverse-link-ratio signal is held
# neutral rather than rewarded (a freebie 1.0 would lift link-farm pages over the gate).
_LINK_RELAXED_TYPES = ("listing", "collection", "forum", "documentation")


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _density_score(text_len: int, html_len: int) -> float:
    if html_len <= 0:
        return 0.0
    d = text_len / html_len
    if d < 0.01:
        return 0.0
    if d < 0.05:
        return 0.6 * (d - 0.01) / 0.04
    if d < 0.25:
        return 0.6 + 0.4 * (d - 0.05) / 0.20
    return 1.0


def _word_count_score(wc: int) -> float:
    if wc < 25:
        return 0.0
    if wc < 200:
        return wc / 200
    return 1.0


def _paragraph_score(paragraphs: int) -> float:
    return {0: 0.0, 1: 0.3, 2: 0.6}.get(paragraphs, 1.0)


def _link_ratio_score(anchor_chars: int, text_chars: int, page_type: PageType) -> float:
    if page_type in _LINK_RELAXED_TYPES:
        return 0.6  # neutral: link density is legitimate here, but not a free 1.0
    if text_chars <= 0:
        return 0.0
    # Widened tolerance band so a low-confidence misclassification of a legitimately
    # link-heavy page degrades gracefully instead of dropping straight to 0.
    ratio = anchor_chars / text_chars
    if ratio < 0.4:
        return 1.0
    if ratio > 0.8:
        return 0.0
    return 1.0 - (ratio - 0.4) / 0.4


def jsonld_types(json_ld: list[dict[str, Any]]) -> set[str]:
    """Collect lowercased schema.org @type strings, walking @graph nodes."""
    types: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, str):
            types.add(value.rsplit("/", 1)[-1].lower())
        elif isinstance(value, list):
            for v in value:
                add(v)

    for block in json_ld:
        if not isinstance(block, dict):
            continue
        add(block.get("@type"))
        graph = block.get("@graph")
        if isinstance(graph, list):
            for node in graph:
                if isinstance(node, dict):
                    add(node.get("@type"))
    return types


def _jsonld_score(types: set[str]) -> float:
    if not types:
        return 0.4
    if types & _ARTICLE_JSONLD_TYPES:
        return 1.0
    if types & _ENTITY_JSONLD_TYPES:
        return 0.7
    return 0.6


def _count_paragraphs(text: str) -> int:
    """Robust paragraph count: blank-line blocks, falling back to sentence groups when
    a page uses single-newline markdown (so a real article does not lose the signal)."""
    blocks = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if len(blocks) >= 2:
        return len(blocks)
    sentences = len(re.findall(r"[.!?](?:\s|$)", text))
    if sentences >= 6:
        return 3
    if sentences >= 2:
        return 2
    return 1 if text.strip() else 0


def _title_score(title: str | None) -> float:
    if not title:
        return 0.3
    if title_looks_like_error(title):
        return 0.0
    return 1.0


def score_extraction(
    *,
    raw_html_len: int,
    content_text: str,
    content_markdown: str,
    word_count: int,
    json_ld: list[dict[str, Any]],
    title: str | None,
    page_type: PageType,
) -> tuple[float, dict[str, float]]:
    """Return ``(quality_score, signal_subscores)``."""
    text_len = len(content_text)
    paragraphs = _count_paragraphs(content_text)
    anchor_chars = sum(len(m.group(1)) for m in _MD_LINK.finditer(content_markdown))

    subs = {
        "text_density": _density_score(text_len, raw_html_len),
        "word_count": _word_count_score(word_count),
        "paragraph": _paragraph_score(paragraphs),
        "link_ratio": _link_ratio_score(anchor_chars, text_len, page_type),
        "json_ld": _jsonld_score(jsonld_types(json_ld)),
        "title": _title_score(title),
    }
    score = sum(_WEIGHTS[k] * v for k, v in subs.items())

    # Hard vetoes: catch soft-404s / shells the weighted mean would rate as borderline.
    if word_count < 25:
        score = min(score, 0.3)
    if title and title_looks_like_error(title):
        score = min(score, 0.25)
    if raw_html_len > 0 and (text_len / raw_html_len) < 0.005:
        score = min(score, 0.2)

    return _clamp(score), subs


_JSONLD_TYPE_MAP: list[tuple[set[str], PageType]] = [
    ({"product", "productgroup", "offer", "aggregateoffer"}, "product"),
    ({"qapage", "discussionforumposting", "socialmediaposting"}, "forum"),
    ({"itemlist", "collectionpage", "searchresultspage"}, "listing"),
    ({"apireference"}, "documentation"),
    ({"event", "jobposting", "localbusiness", "service", "organization"}, "service"),
    ({"article", "newsarticle", "blogposting", "report", "recipe", "howto"}, "article"),
]

_URL_PATTERNS: list[tuple[re.Pattern[str], PageType]] = [
    (re.compile(r"/(docs?|documentation|reference|api|guide|man)(/|$)"), "documentation"),
    (re.compile(r"/(product|p|dp|item|sku)(/|$)"), "product"),
    (re.compile(r"/(forum|thread|t|viewtopic|questions|comments)(/|$)"), "forum"),
    (re.compile(r"/(category|tag|collection|search|shop|c)(/|$)"), "listing"),
    (re.compile(r"/(blog|news|article|post)(/|$)|/(19|20)\d\d/"), "article"),
]


def classify_page_type(
    json_ld: list[dict[str, Any]],
    og_type: str | None,
    url: str | None,
) -> tuple[PageType, str]:
    """Return ``(page_type, confidence)`` where confidence is high|medium|low|none."""
    types = jsonld_types(json_ld)
    if "techarticle" in types:
        # TechArticle is documentation on a docs-ish host, otherwise an article.
        host_path = (url or "").lower()
        if re.search(r"(docs?|developer|reference|readthedocs)", host_path):
            return "documentation", "high"
        return "article", "high"
    for type_set, page_type in _JSONLD_TYPE_MAP:
        if types & type_set:
            return page_type, "high"

    if og_type:
        ot = og_type.strip().lower()
        if ot.startswith("article"):
            return "article", "medium"
        if ot.startswith("product"):
            return "product", "medium"
        if ot == "profile":
            return "service", "medium"

    if url:
        path = urlsplit(url).path.lower() or "/"
        for pattern, page_type in _URL_PATTERNS:
            if pattern.search(path):
                return page_type, "low"
        if path == "/" or re.search(r"/(about|contact|pricing)(/|$)", path):
            return "service", "low"

    return "unknown", "none"
