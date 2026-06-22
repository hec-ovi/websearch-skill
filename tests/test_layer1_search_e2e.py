"""End-to-end Layer 1 through the real CLI entry point.

Only the two external boundaries are stubbed: SearXNG's HTTP endpoint (via
pytest-httpx) and the ddgs library call (via a fake DDGS). Everything else - request
parsing, fan-out, canonicalization, dedup, provenance merge, de-correlated fusion,
Envelope assembly, JSON serialization - runs for real, and the output is validated
against the frozen search contract.
"""

from __future__ import annotations

import json

from tests.conftest import DDGS_ROWS, SEARCH_RESPONSE_REF, SEARXNG_JSON, FakeDDGS
from websearch import cli

SEARXNG_URL = "http://searxng.test"


def test_cli_search_end_to_end(httpx_mock, monkeypatch, capsys, assert_valid):
    httpx_mock.add_response(json=SEARXNG_JSON)
    monkeypatch.setattr("ddgs.DDGS", lambda *a, **k: FakeDDGS(DDGS_ROWS))

    rc = cli.main(["search", "rust", "--json", "--searxng-url", SEARXNG_URL, "--count", "5"])
    assert rc == 0

    env = json.loads(capsys.readouterr().out)
    assert_valid(env, SEARCH_RESPONSE_REF)

    assert env["ok"] is True
    assert env["meta"]["layer"] == "search"
    assert env["meta"]["backend"] == "searxng+ddgs"

    data = env["data"]
    assert data["engines_queried"] == ["searxng", "ddgs"]

    urls = [r["url"] for r in data["results"]]
    # 3 SearXNG + 3 ddgs - 2 overlaps (rust-guide, python.org) = 4 unique
    assert len(data["results"]) == 4
    assert urls[0] == "https://example.com/rust-guide"  # found by both -> fused to the top
    assert "https://python.org/" in urls  # www + utm stripped, deduped across engines

    guide = next(r for r in data["results"] if r["url"] == "https://example.com/rust-guide")
    assert {s["engine"] for s in guide["sources"]} == {"searxng", "ddgs"}
    assert len(guide["snippets"]) == 2

    py = next(r for r in data["results"] if r["url"] == "https://python.org/")
    assert {s["engine"] for s in py["sources"]} == {"searxng", "ddgs"}

    # answers/suggestions are verbatim engine passthrough, not synthesized
    assert data["answers"] == ["Rust is a systems programming language."]
    assert data["suggestions"] == ["rust book"]

    # SearXNG and ddgs share a correlation group -> de-correlation warning
    assert any("correlation group" in w for w in data["warnings"])

    # consumer-driven golden: every ResultItem carries the keys the agent-IO layer reads
    for r in data["results"]:
        assert {"url", "title", "snippet", "fused_score", "sources"} <= set(r)


def test_cli_text_output_and_success_exit_code(httpx_mock, monkeypatch, capsys):
    httpx_mock.add_response(json=SEARXNG_JSON)
    monkeypatch.setattr("ddgs.DDGS", lambda *a, **k: FakeDDGS(DDGS_ROWS))

    rc = cli.main(["search", "rust", "--searxng-url", SEARXNG_URL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "result(s) for: rust" in out
    assert "https://example.com/rust-guide" in out


def test_cli_all_engines_fail_returns_exit_1(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    # No SearXNG URL configured, and ddgs construction fails -> every engine fails.
    monkeypatch.setattr("ddgs.DDGS", boom)
    rc = cli.main(["search", "rust"])
    assert rc == 1
