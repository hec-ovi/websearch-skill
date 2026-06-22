"""Consumer-driven conformance for the agent-io (Layer 3) contract.

Validates the request models and the AgentPage shape against the frozen schema, and pins
the contract version to the models, so a producer change that breaks the recorded shape
fails CI.
"""

from __future__ import annotations

import json
import pathlib

from tests.conftest import (
    AGENTIO_FETCH_REQUEST_REF,
    AGENTIO_OPEN_REQUEST_REF,
    AGENTIO_PAGE_REF,
    AGENTIO_SEARCH_REQUEST_REF,
    schema_errors,
)
from websearch.layer3_agentio import AGENTIO_CONTRACT_VERSION, fence_untrusted
from websearch.layer3_agentio.models import (
    AgentFetchRequest,
    AgentOpenRequest,
    AgentSearchRequest,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_contract_version_matches_the_models():
    schema = json.loads((ROOT / "contracts" / "agent-io.schema.json").read_text())
    assert schema["x-contract-version"] == AGENTIO_CONTRACT_VERSION == "1.0.0"


def test_search_request_validates(assert_valid):
    req = AgentSearchRequest(query="rust", max_results=5, detail="detailed", site="docs.rs")
    assert_valid(req.model_dump(mode="json"), AGENTIO_SEARCH_REQUEST_REF)


def test_fetch_request_validates(assert_valid):
    req = AgentFetchRequest(url="https://e.com", page=2, page_size_tokens=1000, datamark=True)
    assert_valid(req.model_dump(mode="json"), AGENTIO_FETCH_REQUEST_REF)


def test_open_request_validates(assert_valid):
    req = AgentOpenRequest(handle="e.com~deadbeef", page=3)
    assert_valid(req.model_dump(mode="json"), AGENTIO_OPEN_REQUEST_REF)


def test_agent_page_shape_validates(assert_valid):
    fenced, info = fence_untrusted("body", source_url="https://e.com", nonce="n")
    page = {
        "handle": "e.com~deadbeef",
        "url": "https://e.com",
        "content": fenced,
        "page": 1,
        "total_pages": 1,
        "untrusted": True,
        "fence": info.model_dump(mode="json"),
        "source": "live",
    }
    assert_valid(page, AGENTIO_PAGE_REF)


def test_agent_page_requires_fence():
    # fence is required (every page is fenced); a page without it must fail the contract.
    page = {
        "handle": "e.com~deadbeef",
        "url": "https://e.com",
        "content": "x",
        "page": 1,
        "total_pages": 1,
        "untrusted": True,
    }
    assert schema_errors(page, AGENTIO_PAGE_REF), "AgentPage must require fence"
