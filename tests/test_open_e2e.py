"""End-to-end Layer 2A + 2B through the real CLI ``websearch open``.

Each URL is fetched and extracted for real (only the httpx boundary is stubbed via
pytest-httpx, and curl_cffi where escalation would otherwise hit the network), then
formatted into one paginated document and indexed. The output is validated against the
format response contract.
"""

from __future__ import annotations

import json

from tests.conftest import ARTICLE_HTML, FORMAT_RESPONSE_REF
from websearch import cli

URL_A = "https://page.test/rust"
URL_B = "https://page.test/python"

SECOND_HTML = """<!doctype html><html lang="en"><head>
<title>Python Memory Management</title>
<meta property="og:type" content="article">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Article","headline":"Python Memory Management",
 "author":{"@type":"Person","name":"Sam Py"},"datePublished":"2026-04-02"}
</script></head><body>
<article><h1>Python Memory Management</h1>
<p>Python reclaims memory with reference counting backed by a cyclic garbage collector.
Each object tracks how many references point at it, and when that count reaches zero the
interpreter frees the object immediately without waiting for a collection pass.</p>
<p>The cyclic collector exists because reference counting alone cannot reclaim reference
cycles. It runs periodically, walks container objects, and breaks cycles that are no longer
reachable from the program roots so their memory can be returned to the allocator.</p>
<p>Most programs never tune the collector, though long-running services sometimes disable
it during latency-sensitive sections and run it explicitly between requests instead.</p>
</article></body></html>"""


def test_cli_open_two_distinct_urls(httpx_mock, capsys, assert_valid):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    httpx_mock.add_response(url=URL_B, html=SECOND_HTML)
    rc = cli.main(["open", URL_A, URL_B, "--json"])
    assert rc == 0

    env = json.loads(capsys.readouterr().out)
    assert_valid(env, FORMAT_RESPONSE_REF)
    assert env["ok"] is True
    assert env["meta"]["layer"] == "format"
    sc = env["data"]["sidecar"]
    assert sc["total_results"] == 2  # distinct bodies, not folded
    titles = {r["title"] for r in sc["results"]}
    assert titles == {"Understanding Rust Ownership", "Python Memory Management"}
    # input URL order preserved (no relevance score on a direct open)
    assert [r["url"] for r in sc["results"]] == [URL_A, URL_B]


def test_cli_open_folds_identical_pages(httpx_mock, capsys):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    httpx_mock.add_response(url=URL_B, html=ARTICLE_HTML)  # same body
    rc = cli.main(["open", URL_A, URL_B, "--json"])
    assert rc == 0
    sc = json.loads(capsys.readouterr().out)["data"]["sidecar"]
    assert sc["total_results"] == 1
    assert sc["total_dropped_duplicates"] == 1


def test_cli_open_human_markdown(httpx_mock, capsys):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    rc = cli.main(["open", URL_A])
    assert rc == 0
    out = capsys.readouterr().out
    assert "## 1. Understanding Rust Ownership" in out
    assert "<!-- result doc_" in out  # layout-stable delimiter
    assert "Ownership is the mechanism" in out  # full body inlined (small page, auto -> full)


def test_cli_open_search_passages(httpx_mock, capsys):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    rc = cli.main(["open", URL_A, "--search", "borrow checker", "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    search = env["meta"]["page_search"]
    assert search["backend"] in ("sqlite-fts5", "memory-bm25")
    assert search["total"] >= 1
    assert any("borrow" in p["text"].lower() for p in search["passages"])


def test_cli_open_anthropic_blocks(httpx_mock, capsys):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    rc = cli.main(["open", URL_A, "--anthropic-blocks", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    blocks = out["data"]["sidecar"]["anthropic_search_result_blocks"]
    assert blocks[0]["type"] == "search_result"
    assert blocks[0]["source"] == URL_A
    assert blocks[0]["citations"] == {"enabled": True}


def test_cli_open_partial_failure_warns(httpx_mock, monkeypatch, capsys):
    import httpx

    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    httpx_mock.add_exception(httpx.ConnectError("refused"), url=URL_B)

    def boom(url, **kwargs):
        raise RuntimeError("curl down")

    monkeypatch.setattr("curl_cffi.get", boom)  # block escalation to the real network
    rc = cli.main(["open", URL_A, URL_B, "--json"])
    assert rc == 0  # one succeeded
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["sidecar"]["total_results"] == 1
    assert any(URL_B in w for w in env["data"]["warnings"])


def test_cli_open_all_fail_is_error(httpx_mock, monkeypatch, capsys):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"))

    def boom(url, **kwargs):
        raise RuntimeError("curl down")

    monkeypatch.setattr("curl_cffi.get", boom)
    rc = cli.main(["open", URL_A, "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error"]["code"] == "fetch_failed"


def test_cli_open_invalid_url_is_clean_error(capsys):
    rc = cli.main(["open", "ftp://nope", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "invalid_request"


def test_cli_open_index_mode_offloads_body(httpx_mock, capsys):
    httpx_mock.add_response(url=URL_A, html=ARTICLE_HTML)
    rc = cli.main(["open", URL_A, "--mode", "index", "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["sidecar"]["mode"] == "index"
    assert "full body available by id" in env["data"]["markdown"]
    # lossless: sidecar still carries the full body even in index mode
    assert "Ownership is the mechanism" in env["data"]["sidecar"]["results"][0]["body_markdown"]
