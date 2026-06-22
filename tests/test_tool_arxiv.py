"""End-to-end tests for the keyless arXiv tool: parsing, contract, CLI, 429 backoff.

The HTTP boundary is injected (canned Atom XML), so nothing hits the network.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import (
    ARXIV_PAPER_REF,
    ARXIV_SEARCH_PAYLOAD_REF,
    ARXIV_SEARCH_REQUEST_REF,
    ARXIV_SEARCH_RESPONSE_REF,
)
from websearch import cli as cli_mod
from websearch import errors
from websearch.cli import main
from websearch.tool_arxiv import ArxivSearchRequest, build_arxiv_tool

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <opensearch:totalResults>42</opensearch:totalResults>
  <opensearch:startIndex>0</opensearch:startIndex>
  <opensearch:itemsPerPage>2</opensearch:itemsPerPage>
  <entry>
    <id>http://arxiv.org/abs/2401.01234v2</id>
    <updated>2026-05-02T00:00:00Z</updated>
    <published>2026-05-01T00:00:00Z</published>
    <title>Attention Is Possibly
    All You Need</title>
    <summary>  We study transformers
    and their many uses.  </summary>
    <author><name>Jane Dev</name></author>
    <author><name>John Researcher</name></author>
    <arxiv:doi>10.1234/foo.bar</arxiv:doi>
    <arxiv:comment>10 pages, 3 figures</arxiv:comment>
    <link href="http://arxiv.org/abs/2401.01234v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2401.01234v2"
          rel="related" type="application/pdf"/>
    <arxiv:primary_category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2402.05678v1</id>
    <updated>2026-04-10T00:00:00Z</updated>
    <published>2026-04-10T00:00:00Z</published>
    <title>Another Paper Title</title>
    <summary>Second abstract.</summary>
    <author><name>Alice Smith</name></author>
    <link href="http://arxiv.org/abs/2402.05678v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2402.05678v1"
          rel="related" type="application/pdf"/>
    <arxiv:primary_category term="stat.ML" scheme="http://arxiv.org/schemas/atom"/>
    <category term="stat.ML"/>
  </entry>
</feed>"""


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


def _sequence_get(responses):
    it = iter(responses)

    def _get(url, *, params, headers, timeout_s):
        return next(it)

    return _get


def test_parses_atom_into_papers():
    record: list = []
    tool = build_arxiv_tool(http_get=_static_get(ATOM, record=record))
    env = tool.search(ArxivSearchRequest(query="transformers", max_results=2))
    assert env.ok
    data = env.data
    assert data["total_results"] == 42
    assert len(data["papers"]) == 2

    p0 = data["papers"][0]
    assert p0["arxiv_id"] == "2401.01234v2"
    assert p0["title"] == "Attention Is Possibly All You Need"  # whitespace collapsed
    assert p0["authors"] == ["Jane Dev", "John Researcher"]
    assert p0["summary"] == "We study transformers and their many uses."
    assert p0["abs_url"] == "https://arxiv.org/abs/2401.01234v2"  # http -> https
    assert p0["pdf_url"] == "https://arxiv.org/pdf/2401.01234v2"
    assert p0["primary_category"] == "cs.LG"
    assert p0["categories"] == ["cs.LG", "cs.CL"]
    assert p0["doi"] == "10.1234/foo.bar"
    assert p0["comment"] == "10 pages, 3 figures"
    # the request was sent with the field-prefixed search_query
    assert record[0]["params"]["search_query"] == "all:transformers"
    assert record[0]["headers"]["User-Agent"].startswith("websearch-skill")


def test_field_prefix_maps_to_arxiv_syntax():
    for field, prefix in [("title", "ti"), ("author", "au"), ("abstract", "abs"), ("all", "all")]:
        req = ArxivSearchRequest(query="neural", field=field)
        assert req.search_query() == f"{prefix}:neural"


def test_contract_valid_response_and_payload(assert_valid):
    tool = build_arxiv_tool(http_get=_static_get(ATOM))
    env = tool.search(ArxivSearchRequest(query="x", max_results=2))
    payload = env.model_dump(mode="json")
    assert_valid(payload, ARXIV_SEARCH_RESPONSE_REF)
    assert_valid(payload["data"], ARXIV_SEARCH_PAYLOAD_REF)
    for paper in payload["data"]["papers"]:
        assert_valid(paper, ARXIV_PAPER_REF)
    assert payload["meta"]["layer"] == "arxiv"


def test_request_contract_valid(assert_valid):
    req = ArxivSearchRequest(query="x").model_dump(mode="json")
    assert_valid(req, ARXIV_SEARCH_REQUEST_REF)


def test_429_retries_then_succeeds_honoring_retry_after():
    slept: list[float] = []
    responses = [_Resp("", 429, {"retry-after": "1"}), _Resp(ATOM, 200)]
    tool = build_arxiv_tool(http_get=_sequence_get(responses), sleep=slept.append, max_retries=3)
    env = tool.search(ArxivSearchRequest(query="x"))
    assert env.ok
    assert len(env.data["papers"]) == 2
    assert slept == [1.0]  # honored Retry-After, one retry


def test_429_backoff_without_retry_after_is_exponential():
    slept: list[float] = []
    responses = [_Resp("", 429), _Resp(ATOM, 200)]
    tool = build_arxiv_tool(
        http_get=_sequence_get(responses), sleep=slept.append, base_backoff_s=0.5
    )
    env = tool.search(ArxivSearchRequest(query="x"))
    assert env.ok
    assert slept == [0.5]  # base * 2**0


def test_429_exhausted_is_rate_limited_error():
    slept: list[float] = []
    tool = build_arxiv_tool(
        http_get=_static_get("", 429), sleep=slept.append, max_retries=2, base_backoff_s=0.1
    )
    env = tool.search(ArxivSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.RATE_LIMITED
    assert env.error.retriable is True
    assert len(slept) == 2  # tried max_retries times before giving up


def test_http_500_is_retriable_upstream_error():
    tool = build_arxiv_tool(http_get=_static_get("oops", 500))
    env = tool.search(ArxivSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR
    assert env.error.retriable is True


def test_transport_exception_is_upstream_error():
    def _boom(url, *, params, headers, timeout_s):
        raise ConnectionError("dns")

    env = build_arxiv_tool(http_get=_boom).search(ArxivSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR


def test_malformed_xml_is_upstream_error():
    tool = build_arxiv_tool(http_get=_static_get("<feed><broken>", 200))
    env = tool.search(ArxivSearchRequest(query="x"))
    assert not env.ok
    assert env.error.code == errors.UPSTREAM_ERROR


def test_cli_arxiv_json(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod, "build_arxiv_tool", lambda **k: build_arxiv_tool(http_get=_static_get(ATOM))
    )
    rc = main(["arxiv", "transformers", "--max-results", "2", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["meta"]["layer"] == "arxiv"
    assert len(out["data"]["papers"]) == 2


def test_cli_arxiv_human(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod, "build_arxiv_tool", lambda **k: build_arxiv_tool(http_get=_static_get(ATOM))
    )
    rc = main(["arxiv", "transformers"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Attention Is Possibly All You Need" in out
    assert "arxiv.org/abs/2401.01234v2" in out


def test_cli_arxiv_invalid_max_results(capsys):
    rc = main(["arxiv", "x", "--max-results", "999", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert out["error"]["code"] == errors.INVALID_REQUEST


def test_cli_arxiv_rate_limited_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_mod,
        "build_arxiv_tool",
        lambda **k: build_arxiv_tool(
            http_get=_static_get("", 429), sleep=lambda s: None, max_retries=1
        ),
    )
    rc = main(["arxiv", "x", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == errors.RATE_LIMITED


def test_mcp_arxiv_search(monkeypatch):
    pytest.importorskip("fastmcp")
    from websearch.layer3_agentio import mcp_server

    monkeypatch.setattr(mcp_server, "_ARXIV", build_arxiv_tool(http_get=_static_get(ATOM)))
    out = mcp_server.arxiv_search(query="transformers", max_results=2)
    assert out["ok"] is True
    assert out["meta"]["layer"] == "arxiv"
    assert len(out["data"]["papers"]) == 2

    bad = mcp_server.arxiv_search(query="x", max_results=999)
    assert bad["ok"] is False
    assert bad["error"]["code"] == errors.INVALID_REQUEST


@pytest.mark.parametrize("field", ["title", "author", "abstract"])
def test_cli_arxiv_field_passthrough(monkeypatch, capsys, field):
    record: list = []
    monkeypatch.setattr(
        cli_mod,
        "build_arxiv_tool",
        lambda **k: build_arxiv_tool(http_get=_static_get(ATOM, record=record)),
    )
    rc = main(["arxiv", "graphs", "--field", field, "--json"])
    assert rc == 0
    prefix = {"title": "ti", "author": "au", "abstract": "abs"}[field]
    assert record[0]["params"]["search_query"] == f"{prefix}:graphs"


# --- regression coverage for the gate findings ------------------------------------

ATOM_ERROR = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/api/errors#incorrect_id_format</id>
    <title>Error</title>
    <summary>incorrect id format for query</summary>
    <link href="http://arxiv.org/api/errors" rel="alternate" type="text/html"/>
  </entry>
</feed>"""

ATOM_AUTHOR_NO_NAME = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2501.00001v1</id>
    <title>Good Paper</title>
    <summary>ok</summary>
    <author><name>Real Author</name></author>
    <author><affiliation>No Name Here</affiliation></author>
    <link href="http://arxiv.org/abs/2501.00001v1" rel="alternate" type="text/html"/>
  </entry>
</feed>"""


def test_multiword_query_is_quoted_but_single_word_and_operators_are_not():
    # multi-word topic -> phrase quote, so a date sort stays on-topic
    assert (
        ArxivSearchRequest(query="retrieval augmented generation").search_query()
        == 'all:"retrieval augmented generation"'
    )
    # single word -> no quoting
    assert ArxivSearchRequest(query="transformers").search_query() == "all:transformers"
    # explicit boolean operator -> passed through unchanged
    assert ArxivSearchRequest(query="ti:foo AND bar").search_query() == "all:ti:foo AND bar"


def test_whitespace_only_query_is_rejected():
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        ArxivSearchRequest(query="   ")


def test_cli_arxiv_whitespace_query_invalid(capsys):
    rc = main(["arxiv", "   ", "--json"])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["code"] == errors.INVALID_REQUEST


def test_arxiv_error_entry_dropped_and_surfaced_as_warning():
    env = build_arxiv_tool(http_get=_static_get(ATOM_ERROR)).search(ArxivSearchRequest(query="x"))
    assert env.ok  # HTTP 200; not an error envelope
    assert env.data["papers"] == []  # the error sentinel is not a fake paper
    assert any("arXiv rejected" in w for w in env.data["warnings"])


def test_author_without_name_is_dropped():
    env = build_arxiv_tool(http_get=_static_get(ATOM_AUTHOR_NO_NAME)).search(
        ArxivSearchRequest(query="x")
    )
    assert env.ok
    assert len(env.data["papers"]) == 1
    assert env.data["papers"][0]["authors"] == ["Real Author"]


def test_429_http_date_retry_after_is_honored_and_clamped():
    slept: list[float] = []
    responses = [_Resp("", 429, {"retry-after": "Wed, 21 Oct 2099 07:28:00 GMT"}), _Resp(ATOM, 200)]
    tool = build_arxiv_tool(http_get=_sequence_get(responses), sleep=slept.append)
    env = tool.search(ArxivSearchRequest(query="x"))
    assert env.ok
    assert len(slept) == 1
    assert 0.0 <= slept[0] <= 60.0  # far-future HTTP-date clamped to 60s
