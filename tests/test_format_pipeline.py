"""FormatPipeline behavior: ordering, dedup, pagination, modes, sidecar, Anthropic view."""

from __future__ import annotations

import re

from tests.conftest import FORMAT_RESPONSE_REF
from websearch.layer2_format import FormatRequest, ResultInput, build_format_pipeline

LOREM = (
    "Ownership is the mechanism Rust uses to manage memory. Every value has a single owner "
    "and there can be only one owner at a time. When the owner goes out of scope the value "
    "is dropped and its memory is freed automatically by the compiler at compile time."
)


def _run(req: FormatRequest):
    env = build_format_pipeline().run(req)
    return env, env.data


def test_response_conforms_to_contract(assert_valid):
    req = FormatRequest(
        query="rust",
        results=[ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=LOREM)],
        include_anthropic_blocks=True,
    )
    env = build_format_pipeline().run(req)
    assert_valid(env.model_dump(mode="json"), FORMAT_RESPONSE_REF)
    assert env.meta.layer == "format"
    assert env.contract_version == "1.0.0"


def test_orders_by_score_descending():
    req = FormatRequest(
        results=[
            ResultInput(url="https://a.test/low", title="low", score=0.2, body_markdown="x"),
            ResultInput(url="https://a.test/high", title="high", score=0.9, body_markdown="y"),
            ResultInput(url="https://a.test/mid", title="mid", score=0.5, body_markdown="z"),
        ]
    )
    _env, data = _run(req)
    ranks = [(r["rank"], r["title"]) for r in data["sidecar"]["results"]]
    assert ranks == [(1, "high"), (2, "mid"), (3, "low")]


def test_missing_scores_sort_last_in_input_order():
    req = FormatRequest(
        results=[
            ResultInput(url="https://a.test/1", title="first", body_markdown="a"),
            ResultInput(url="https://a.test/2", title="scored", score=0.5, body_markdown="b"),
            ResultInput(url="https://a.test/3", title="second", body_markdown="c"),
        ]
    )
    _env, data = _run(req)
    titles = [r["title"] for r in data["sidecar"]["results"]]
    assert titles == ["scored", "first", "second"]


def test_dedup_folds_exact_and_reports_count():
    req = FormatRequest(
        results=[
            ResultInput(url="https://a.test/1", title="A", score=0.9, body_markdown=LOREM),
            ResultInput(
                url="https://mirror.test/1", title="A mirror", score=0.4, body_markdown=LOREM
            ),
            ResultInput(
                url="https://a.test/2", title="B", score=0.5, body_markdown="different body"
            ),
        ]
    )
    _env, data = _run(req)
    sc = data["sidecar"]
    assert sc["total_results"] == 2
    assert sc["total_dropped_duplicates"] == 1
    canonical = next(r for r in sc["results"] if r["url"] == "https://a.test/1")
    assert canonical["dropped_duplicates"][0]["url"] == "https://mirror.test/1"


def test_pagination_cursor_and_rank_continuity():
    results = [
        ResultInput(
            url=f"https://a.test/{i}", title=f"r{i}", score=1.0 - i / 100, body_markdown=f"body {i}"
        )
        for i in range(12)
    ]
    _e0, p0 = _run(FormatRequest(results=results, page=0, page_size=5))
    _e1, p1 = _run(FormatRequest(results=results, page=1, page_size=5))
    _e2, p2 = _run(FormatRequest(results=results, page=2, page_size=5))
    assert p0["sidecar"]["total_pages"] == 3
    assert p0["sidecar"]["next_cursor"] == "1"
    assert [r["rank"] for r in p0["sidecar"]["results"]] == [1, 2, 3, 4, 5]
    assert [r["rank"] for r in p1["sidecar"]["results"]] == [6, 7, 8, 9, 10]
    assert [r["rank"] for r in p2["sidecar"]["results"]] == [11, 12]
    assert p2["sidecar"]["next_cursor"] is None  # last page


def test_auto_mode_full_when_small():
    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=LOREM)
            ],
            mode="auto",
            inline_token_budget=10_000,
        )
    )
    assert data["sidecar"]["mode"] == "full"
    assert LOREM[:40] in data["markdown"]  # body inlined


def test_auto_mode_index_when_over_budget():
    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=LOREM)
            ],
            mode="auto",
            inline_token_budget=5,  # force index
        )
    )
    assert data["sidecar"]["mode"] == "index"
    assert "full body available by id" in data["markdown"]
    # lossless: the sidecar still carries the FULL body in index mode
    assert data["sidecar"]["results"][0]["body_markdown"] == LOREM


def test_body_char_budget_offloads_markdown_but_sidecar_is_full():
    long_body = "para. " * 2000  # ~12k chars
    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=long_body)
            ],
            mode="full",
            body_char_budget=500,
        )
    )
    r = data["sidecar"]["results"][0]
    assert r["truncated_in_markdown"] is True
    assert r["body_markdown"] == long_body  # sidecar lossless
    assert len(data["markdown"]) < len(long_body)  # rendered view offloaded
    assert "full body available by id" in data["markdown"]


def test_no_truncate_inlines_full_body():
    long_body = "para. " * 2000
    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=long_body)
            ],
            mode="full",
            body_char_budget=None,
        )
    )
    assert long_body.strip() in data["markdown"]
    assert data["sidecar"]["results"][0]["truncated_in_markdown"] is False


def test_layout_stable_delimited_blocks_carry_ids():
    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/1", title="A", score=0.9, body_markdown="a"),
                ResultInput(url="https://a.test/2", title="B", score=0.5, body_markdown="b"),
            ]
        )
    )
    md = data["markdown"]
    opens = re.findall(r"<!-- result (doc_[0-9a-f]+) rank \d+ -->", md)
    closes = re.findall(r"<!-- /result (doc_[0-9a-f]+) -->", md)
    assert len(opens) == 2
    assert opens == closes  # every block opens and closes with the same id
    assert "next_cursor:" in md and "total_results: 2" in md


def test_anthropic_blocks_shape(assert_valid):
    from tests.conftest import ANTHROPIC_BLOCK_REF

    _env, data = _run(
        FormatRequest(
            results=[
                ResultInput(url="https://a.test/x", title="A", score=0.9, body_markdown=LOREM)
            ],
            include_anthropic_blocks=True,
            anthropic_citations=True,
        )
    )
    blocks = data["sidecar"]["anthropic_search_result_blocks"]
    assert len(blocks) == 1
    block = blocks[0]
    assert_valid(block, ANTHROPIC_BLOCK_REF)
    assert block["type"] == "search_result"
    assert block["source"] == "https://a.test/x"  # bare string, not nested
    assert block["content"][0]["type"] == "text"
    assert block["content"][0]["text"].strip()
    assert block["citations"] == {"enabled": True}


def test_anthropic_blocks_omitted_by_default():
    _env, data = _run(
        FormatRequest(results=[ResultInput(url="https://a.test/x", title="A", body_markdown="x")])
    )
    assert data["sidecar"]["anthropic_search_result_blocks"] == []


def test_no_sidecar_when_disabled():
    _env, data = _run(
        FormatRequest(
            results=[ResultInput(url="https://a.test/x", body_markdown="x")],
            include_sidecar=False,
        )
    )
    assert data["sidecar"] is None
    assert data["markdown"]  # markdown still emitted


def test_derived_id_is_stable_and_site_extracted():
    _env, data = _run(
        FormatRequest(results=[ResultInput(url="https://www.example.com/path", body_markdown="x")])
    )
    r = data["sidecar"]["results"][0]
    assert r["id"].startswith("doc_")
    assert r["site"] == "example.com"  # www stripped
