"""End-to-end Layer 3 through the real CLI entry point.

web-search / web-fetch / web-open / mcp, with the engine boundary faked (ddgs) or mocked
(SearXNG via pytest-httpx) and the fetch boundary stubbed (pytest-httpx). Output is
validated against the agent-io response contract.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import (
    AGENTIO_FETCH_RESPONSE_REF,
    AGENTIO_SEARCH_RESPONSE_REF,
    ARTICLE_HTML,
    DDGS_ROWS,
    SEARXNG_JSON,
    FakeDDGS,
)
from websearch import cli
from websearch.layer2_format import fts5_available

SEARXNG_URL = "http://searxng.test"
FETCH_URL = "https://page.test/rust"


def test_cli_web_search_json(httpx_mock, monkeypatch, capsys, assert_valid):
    httpx_mock.add_response(json=SEARXNG_JSON)
    monkeypatch.setattr("ddgs.DDGS", lambda *a, **k: FakeDDGS(DDGS_ROWS))
    rc = cli.main(["web-search", "rust", "--json", "--searxng-url", SEARXNG_URL])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert_valid(env, AGENTIO_SEARCH_RESPONSE_REF)
    assert env["ok"] and env["meta"]["layer"] == "agentio"
    assert env["data"]["results"][0]["handle"]


def test_cli_web_search_human(httpx_mock, capsys):
    httpx_mock.add_response(json=SEARXNG_JSON)
    rc = cli.main(["web-search", "rust", "--no-ddgs", "--searxng-url", SEARXNG_URL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "result(s) for: rust" in out
    assert "handle:" in out


def test_cli_web_fetch_json_is_fenced(httpx_mock, capsys, assert_valid):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    rc = cli.main(["web-fetch", FETCH_URL, "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert_valid(env, AGENTIO_FETCH_RESPONSE_REF)
    page = env["data"]["pages"][0]
    assert page["untrusted"] is True
    assert page["fence"]["close"] in page["content"]


def test_cli_web_fetch_human_prints_fenced_content(httpx_mock, capsys):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    rc = cli.main(["web-fetch", FETCH_URL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "UNTRUSTED-WEB-CONTENT" in out  # the fence is printed to stdout
    assert "Ownership is the mechanism" in out


def test_cli_web_fetch_then_open_across_invocations(httpx_mock, tmp_path, capsys):
    if not fts5_available():
        pytest.skip("FTS5 not available; cross-invocation web-open needs a persisted store")
    db = str(tmp_path / "idx.sqlite")
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    rc = cli.main(
        ["web-fetch", FETCH_URL, "--page-size-tokens", "20", "--persist-path", db, "--json"]
    )
    assert rc == 0
    handle = json.loads(capsys.readouterr().out)["data"]["pages"][0]["handle"]
    # A SEPARATE process invocation: web-open resolves the handle from the persisted store.
    rc = cli.main(
        [
            "web-open",
            handle,
            "--page",
            "2",
            "--page-size-tokens",
            "20",
            "--persist-path",
            db,
            "--json",
        ]
    )
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] and env["data"]["pages"][0]["source"] == "cache"


def test_cli_web_fetch_invalid_url_is_clean_error(capsys):
    rc = cli.main(["web-fetch", "ftp://nope", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "invalid_request"


def test_cli_web_open_unknown_handle_is_error(capsys):
    rc = cli.main(["web-open", "nope~deadbeef", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "not_opened"


def test_cli_mcp_command_without_fastmcp_is_actionable(monkeypatch, capsys):
    # Simulate the optional dependency being absent: the import helper raises ImportError,
    # and the command must surface an actionable error, not a traceback or a blocking run.
    def boom():
        raise ImportError("No module named 'fastmcp'")

    monkeypatch.setattr(cli, "_load_mcp_server", boom)
    rc = cli.main(["mcp"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "dependency_missing" in err and "fastmcp" in err
