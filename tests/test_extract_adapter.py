"""TrafilaturaExtractor run for real against canned HTML (trafilatura is a fast,
deterministic local library, so there is nothing to mock)."""

from __future__ import annotations

from tests.conftest import ARTICLE_HTML
from websearch.layer2_extract.extractors.trafilatura_extractor import TrafilaturaExtractor
from websearch.layer2_extract.models import ExtractRequest


def _extract(html: str, **kw) -> object:
    return TrafilaturaExtractor().extract(ExtractRequest(html=html, **kw))


def test_article_extraction_full_fidelity():
    r = _extract(ARTICLE_HTML, base_url="https://example.com/blog/rust")
    assert r.title == "Understanding Rust Ownership"
    assert r.byline == "Jane Dev"
    assert r.date == "2026-05-01"
    assert r.page_type == "article"
    assert r.extracted_via == "trafilatura"
    assert r.quality_score >= 0.80
    assert "# Understanding Rust Ownership" in r.content_markdown
    assert [b.get("@type") for b in r.json_ld] == ["Article"]
    assert "https://doc.rust-lang.org/book" in r.links
    assert r.metadata.get("og_type") == "article"
    assert r.word_count > 100


def test_content_markdown_is_not_truncated():
    big = ARTICLE_HTML.replace(
        "</article>", "<p>" + ("extra sentence here. " * 2000) + "</p></article>"
    )
    r = _extract(big)
    assert len(r.content_markdown) > 20_000  # no length cap applied anywhere


def test_favor_precision_and_recall_both_run():
    for favor in ("precision", "recall", "balanced"):
        r = _extract(ARTICLE_HTML, favor=favor)
        assert r.content_markdown
        assert r.quality_score >= 0.0


def test_boilerplate_only_page_is_low_quality_with_warning():
    r = _extract("<html><body><nav><a href='/'>home</a></nav></body></html>")
    assert r.quality_score < 0.80
    assert r.warnings  # either "no extractable content" or "fallback candidate"


def test_query_emits_not_implemented_warning():
    r = _extract(ARTICLE_HTML, query="ownership")
    assert any("query-focused extraction is not implemented" in w for w in r.warnings)


def test_include_comments_default_off():
    html = ARTICLE_HTML.replace(
        "</article>",
        "</article><section id='comments'><p>FIRST POST spam comment here.</p></section>",
    )
    r = _extract(html, include_comments=False)
    assert "FIRST POST spam" not in r.content_markdown
