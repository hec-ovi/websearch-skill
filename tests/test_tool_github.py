"""End-to-end tests for the keyless GitHub repo tool: parsing, contract, CLI, rate limit.

The HTTP boundary is injected (canned JSON), so nothing hits the network.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.conftest import (
    GITHUB_REPO_REF,
    GITHUB_SEARCH_PAYLOAD_REF,
    GITHUB_SEARCH_REQUEST_REF,
    GITHUB_SEARCH_RESPONSE_REF,
)
from websearch import cli as cli_mod
from websearch import errors
from websearch.cli import main
from websearch.tool_github import GithubSearchRequest, build_github_tool

BODY = json.dumps(
    {
        "total_count": 12345,
        "incomplete_results": False,
        "items": [
            {
                "full_name": "tiangolo/fastapi",
                "html_url": "https://github.com/tiangolo/fastapi",
                "description": "FastAPI framework, high performance",
                "stargazers_count": 70000,
                "forks_count": 6000,
                "open_issues_count": 30,
                "language": "Python",
                "topics": ["python", "api", "async"],
                "owner": {"login": "tiangolo"},
                "updated_at": "2026-06-20T00:00:00Z",
                "pushed_at": "2026-06-21T00:00:00Z",
                "license": {"spdx_id": "MIT"},
            },
            {
                "full_name": "pallets/flask",
                "html_url": "https://github.com/pallets/flask",
                "description": None,
                "stargazers_count": 65000,
                "forks_count": 16000,
                "open_issues_count": 10,
                "language": "Python",
                "topics": [],
                "owner": {"login": "pallets"},
                "updated_at": "2026-06-19T00:00:00Z",
                "license": {"spdx_id": "NOASSERTION"},
            },
        ],
    }
)


class _Resp:
    def __init__(self, text: str = "", status_code: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


def _static_get(text="", status_code=200, resp_headers=None, record=None):
    def _get(url, *, params, headers, timeout_s):
        if record is not None:
            record.append({"url": url, "params": params, "headers": headers})
        return _Resp(text, status_code, resp_headers)

    return _get


def test_parses_repos():
    record: list = []
    env = build_github_tool(http_get=_static_get(BODY, record=record)).search(
        GithubSearchRequest(query="fastapi")
    )
    assert env.ok
    data = env.data
    assert data["total_count"] == 12345
    assert data["incomplete_results"] is False
    assert len(data["repos"]) == 2

    r0 = data["repos"][0]
    assert r0["full_name"] == "tiangolo/fastapi"
    assert r0["stars"] == 70000
    assert r0["forks"] == 6000
    assert r0["language"] == "Python"
    assert r0["topics"] == ["python", "api", "async"]
    assert r0["owner"] == "tiangolo"
    assert r0["license"] == "MIT"

    r1 = data["repos"][1]
    assert r1["description"] is None
    assert r1["license"] is None  # NOASSERTION normalized to null

    # query sent with sort param, no language qualifier
    assert record[0]["params"]["q"] == "fastapi"
    assert record[0]["params"]["sort"] == "stars"
    assert record[0]["headers"]["User-Agent"].startswith("websearch-skill")
    assert record[0]["headers"]["Accept"] == "application/vnd.github+json"


def test_language_qualifier_and_best_match_omits_sort():
    record: list = []
    build_github_tool(http_get=_static_get(BODY, record=record)).search(
        GithubSearchRequest(query="web crawler", language="Rust", sort="best-match")
    )
    assert record[0]["params"]["q"] == "web crawler language:Rust"
    assert "sort" not in record[0]["params"]  # best-match omits the sort param


def test_contract_valid_response_and_payload(assert_valid):
    env = build_github_tool(http_get=_static_get(BODY)).search(GithubSearchRequest(query="x"))
    payload = env.model_dump(mode="json")
    assert_valid(payload, GITHUB_SEARCH_RESPONSE_REF)
    assert_valid(payload["data"], GITHUB_SEARCH_PAYLOAD_REF)
    for repo in payload["data"]["repos"]:
        assert_valid(repo, GITHUB_REPO_REF)
    assert payload["meta"]["layer"] == "github"


def test_request_contract_valid(assert_valid):
    req = GithubSearchRequest(query="x").model_dump(mode="json")
    assert_valid(req, GITHUB_SEARCH_REQUEST_REF)


def test_403_rate_limit_with_reset():
    reset = str(int(time.time()) + 42)
    env = build_github_tool(
        http_get=_static_get("", 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})
    ).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.RATE_LIMITED
    assert env.error.retriable is True
    assert "rate limit" in env.error.message.lower()


def test_429_rate_limit_with_retry_after():
    env = build_github_tool(http_get=_static_get("", 429, {"retry-after": "30"})).search(
        GithubSearchRequest(query="x")
    )
    assert not env.ok
    assert env.error.code == errors.RATE_LIMITED
    assert "30s" in env.error.message


def test_422_is_non_retriable_upstream_error():
    env = build_github_tool(http_get=_static_get("{}", 422)).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR
    assert env.error.retriable is False


def test_500_is_retriable_upstream_error():
    env = build_github_tool(http_get=_static_get("err", 500)).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR
    assert env.error.retriable is True


def test_transport_exception_is_upstream_error():
    def _boom(url, *, params, headers, timeout_s):
        raise TimeoutError("slow")

    env = build_github_tool(http_get=_boom).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR


def test_malformed_json_is_upstream_error():
    env = build_github_tool(http_get=_static_get("{not json", 200)).search(
        GithubSearchRequest(query="x")
    )
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR


def test_cli_github_json(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod, "build_github_tool", lambda **k: build_github_tool(http_get=_static_get(BODY))
    )
    rc = main(["github", "fastapi", "--per-page", "2", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["meta"]["layer"] == "github"
    assert out["data"]["repos"][0]["full_name"] == "tiangolo/fastapi"


def test_cli_github_human(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod, "build_github_tool", lambda **k: build_github_tool(http_get=_static_get(BODY))
    )
    rc = main(["github", "fastapi"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tiangolo/fastapi" in out
    assert "stars=70000" in out


def test_cli_github_invalid_per_page(capsys):
    rc = main(["github", "x", "--per-page", "500", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == errors.INVALID_REQUEST


def test_mcp_github_search(monkeypatch):
    import pytest

    pytest.importorskip("fastmcp")
    from websearch.layer3_agentio import mcp_server

    monkeypatch.setattr(mcp_server, "_GITHUB", build_github_tool(http_get=_static_get(BODY)))
    out = mcp_server.github_search(query="fastapi", per_page=2)
    assert out["ok"] is True
    assert out["meta"]["layer"] == "github"
    assert out["data"]["repos"][0]["full_name"] == "tiangolo/fastapi"

    bad = mcp_server.github_search(query="x", per_page=999)
    assert bad["ok"] is False
    assert bad["error"]["code"] == errors.INVALID_REQUEST


def test_cli_github_rate_limited_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod,
        "build_github_tool",
        lambda **k: build_github_tool(
            http_get=_static_get("", 403, {"x-ratelimit-remaining": "0"})
        ),
    )
    rc = main(["github", "x", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == errors.RATE_LIMITED


# --- regression coverage for the gate findings ------------------------------------


def test_non_rate_limit_403_is_non_retriable_upstream_error():
    body = json.dumps({"message": "Repository access blocked"})
    env = build_github_tool(
        http_get=_static_get(body, 403, {"x-ratelimit-remaining": "57"})
    ).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR  # not mislabeled as rate_limited
    assert env.error.retriable is False
    assert "Repository access blocked" in env.error.message  # GitHub's own message surfaced


def test_403_secondary_limit_with_retry_after_is_rate_limited():
    env = build_github_tool(
        http_get=_static_get("", 403, {"x-ratelimit-remaining": "57", "retry-after": "60"})
    ).search(GithubSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.RATE_LIMITED  # secondary limit still recognized


def test_malformed_star_count_does_not_crash():
    body = json.dumps(
        {
            "total_count": 1,
            "items": [{"full_name": "a/b", "html_url": "u", "stargazers_count": "x"}],
        }
    )
    env = build_github_tool(http_get=_static_get(body, 200)).search(GithubSearchRequest(query="x"))
    assert env.ok  # a non-numeric count is coerced, search() still returns an Envelope
    assert env.data["repos"][0]["stars"] == 0


def test_incomplete_results_is_surfaced_as_warning():
    body = json.dumps({"total_count": 1, "incomplete_results": True, "items": []})
    env = build_github_tool(http_get=_static_get(body, 200)).search(GithubSearchRequest(query="x"))
    assert env.ok
    assert env.data["incomplete_results"] is True
    assert any("incomplete" in w.lower() for w in env.data["warnings"])


def test_whitespace_only_query_rejected():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        GithubSearchRequest(query="   ")


def test_cli_github_whitespace_query_invalid(capsys):
    rc = main(["github", "   ", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == errors.INVALID_REQUEST
