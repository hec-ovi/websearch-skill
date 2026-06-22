"""Extraction quality scoring and page-type classification heuristics."""

from __future__ import annotations

from websearch.layer2_extract.quality import classify_page_type, score_extraction


def _article_text(paragraphs: int = 8) -> str:
    para = (
        "Ownership is the mechanism Rust uses to manage memory and every value has a single "
        "owner that drops the value when it goes out of scope which prevents memory leaks."
    )
    return "\n\n".join([para] * paragraphs)


def test_real_article_clears_threshold():
    text = _article_text()
    score, subs = score_extraction(
        raw_html_len=len(text) * 3,
        content_text=text,
        content_markdown=text,
        word_count=len(text.split()),
        json_ld=[{"@type": "Article"}],
        title="Understanding Rust Ownership",
        page_type="article",
    )
    assert score >= 0.80
    assert subs["word_count"] == 1.0


def test_soft_404_is_vetoed_low():
    score, _ = score_extraction(
        raw_html_len=400,
        content_text="Not found",
        content_markdown="Not found",
        word_count=2,
        json_ld=[],
        title="404 Not Found",
        page_type="unknown",
    )
    # word_count veto (<25) and error-title veto both apply.
    assert score <= 0.3


def _score(**kw):
    base = dict(
        raw_html_len=3000,
        content_text="word " * 120,
        content_markdown="word " * 120,
        word_count=120,
        json_ld=[],
        title="A Title",
        page_type="unknown",
    )
    base.update(kw)
    return score_extraction(**base)


def test_link_density_relaxed_for_listing_forum_docs():
    # Link-heavy pages are held neutral (0.6), not penalized and not given a free 1.0.
    for pt in ("listing", "collection", "forum", "documentation"):
        _, subs = _score(page_type=pt)
        assert subs["link_ratio"] == 0.6


def test_jsonld_tiers_article_full_entity_partial():
    assert _score(json_ld=[{"@type": "Article"}])[1]["json_ld"] == 1.0
    assert _score(json_ld=[{"@type": "Product"}])[1]["json_ld"] == 0.7
    assert _score(json_ld=[{"@type": "QAPage"}])[1]["json_ld"] == 0.7
    assert _score(json_ld=[{"@type": "WebPage"}])[1]["json_ld"] == 0.6
    assert _score(json_ld=[])[1]["json_ld"] == 0.4


def test_thin_entity_page_stays_below_gate():
    # A nav-heavy product page (little prose, many links) must not clear the 0.80 gate,
    # matching the research expectation that products/listings fall below articles.
    markdown = " ".join(f"[item {i}](https://shop.test/p/{i})" for i in range(60))
    score, _ = score_extraction(
        raw_html_len=40000,
        content_text="buy now in stock add to cart",
        content_markdown=markdown,
        word_count=7,
        json_ld=[{"@type": "Product"}],
        title="Some Product",
        page_type="product",
    )
    assert score < 0.80


def test_paragraph_count_robust_to_single_newline():
    # Single-newline markdown with several sentences still earns the paragraph signal.
    text = "First sentence here. Second one follows. Third. Fourth. Fifth! Sixth?"
    _, subs = _score(content_text=text)
    assert subs["paragraph"] == 1.0


def test_classify_by_jsonld_type():
    assert classify_page_type([{"@type": "Product"}], None, None)[0] == "product"
    assert classify_page_type([{"@type": "NewsArticle"}], None, None)[0] == "article"
    assert classify_page_type([{"@type": "QAPage"}], None, None)[0] == "forum"
    assert classify_page_type([{"@type": "ItemList"}], None, None)[0] == "listing"


def test_classify_techarticle_on_docs_host_is_documentation():
    pt, conf = classify_page_type([{"@type": "TechArticle"}], None, "https://docs.rust-lang.org/x")
    assert (pt, conf) == ("documentation", "high")


def test_classify_jsonld_graph_walk():
    blocks = [{"@graph": [{"@type": "WebPage"}, {"@type": "Product"}]}]
    assert classify_page_type(blocks, None, None)[0] == "product"


def test_classify_by_og_type_when_no_jsonld():
    pt, conf = classify_page_type([], "article", None)
    assert (pt, conf) == ("article", "medium")


def test_classify_by_url_shape_lowest_confidence():
    assert classify_page_type([], None, "https://blog.test/blog/my-post")[0] == "article"
    assert classify_page_type([], None, "https://shop.test/product/42")[0] == "product"
    assert classify_page_type([], None, "https://site.test/docs/intro")[0] == "documentation"


def test_classify_unknown_default():
    pt, conf = classify_page_type([], None, "https://example.test/random/x")
    assert (pt, conf) == ("unknown", "none")
