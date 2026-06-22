"""End-to-end Layer 2A through the real CLI ``websearch fetch``.

Only the two external boundaries are stubbed: the httpx Tier-0 request (via
pytest-httpx) and, when escalation is exercised, ``curl_cffi.get`` (faked at the
libcurl boundary, since pytest-httpx cannot intercept it). Request parsing, tier
escalation, block detection, extraction, quality scoring, Envelope assembly, and JSON
serialization all run for real, and the output is validated against the contract.
"""

from __future__ import annotations

import json

from tests.conftest import (
    ARTICLE_HTML,
    CLOUDFLARE_HTML,
    EXTRACT_RESPONSE_REF,
    fake_curl_getter,
)
from websearch import cli

URL = "https://page.test/article"


def test_cli_fetch_success_end_to_end(httpx_mock, capsys, assert_valid):
    httpx_mock.add_response(html=ARTICLE_HTML)
    rc = cli.main(["fetch", URL, "--json"])
    assert rc == 0

    env = json.loads(capsys.readouterr().out)
    assert_valid(env, EXTRACT_RESPONSE_REF)
    assert env["ok"] is True
    assert env["meta"]["layer"] == "extract"

    data = env["data"]
    assert data["source"]["fetched_via"] == "http"
    assert data["source"]["status"] == 200
    result = data["result"]
    assert result["page_type"] == "article"
    assert result["quality_score"] >= 0.80
    assert "# Understanding Rust Ownership" in result["content_markdown"]
    assert result["title"] == "Understanding Rust Ownership"


def test_cli_fetch_human_output(httpx_mock, capsys):
    httpx_mock.add_response(html=ARTICLE_HTML)
    rc = cli.main(["fetch", URL])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Understanding Rust Ownership" in out
    assert "Ownership is the mechanism" in out  # full markdown body printed


def test_cli_fetch_invalid_url_is_clean_error(capsys):
    rc = cli.main(["fetch", "ftp://nope", "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_request"


def test_cli_fetch_transport_failure_is_fetch_failed(httpx_mock, monkeypatch, capsys):
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("refused"))

    def boom(url, **kwargs):
        raise RuntimeError("curl down")

    monkeypatch.setattr("curl_cffi.get", boom)  # block escalation to the real network
    rc = cli.main(["fetch", URL, "--json"])
    assert rc == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "fetch_failed"


def test_cli_fetch_escalates_to_curl_cffi(httpx_mock, monkeypatch, capsys):
    httpx_mock.add_response(status_code=403, html=CLOUDFLARE_HTML)
    monkeypatch.setattr("curl_cffi.get", fake_curl_getter(ARTICLE_HTML))
    rc = cli.main(["fetch", URL, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["source"]["fetched_via"] == "curl_cffi"
    assert data["source"]["tier_attempts"] == ["httpx", "curl_cffi"]
    assert data["result"]["page_type"] == "article"


def test_cli_fetch_blocked_is_surfaced(httpx_mock, monkeypatch, capsys):
    httpx_mock.add_response(status_code=403, html=CLOUDFLARE_HTML)
    monkeypatch.setattr("curl_cffi.get", fake_curl_getter(CLOUDFLARE_HTML, status_code=403))
    rc = cli.main(["fetch", URL, "--json"])
    assert rc == 0  # we still return what we got, with the block surfaced
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["source"]["blocked"] is True
    assert data["source"]["block_reason"] == "cloudflare_challenge"
    assert any("blocked" in w for w in data["warnings"])


def test_cli_fetch_404_returns_content_with_warning(httpx_mock, capsys):
    httpx_mock.add_response(status_code=404, html="<html><body><h1>Not found</h1></body></html>")
    rc = cli.main(["fetch", URL, "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["source"]["status"] == 404
    assert any("HTTP 404" in w for w in data["warnings"])
