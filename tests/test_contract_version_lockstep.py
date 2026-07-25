"""Every contract schema's x-contract-version must match its Python constant.

The fetch contract drifted once (schema bumped to 1.2.0, FETCH_CONTRACT_VERSION left at
1.1.0) because only the agentio pair had a lockstep assertion. This test walks every
schema/constant pair so a bump on one side without the other fails CI.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from websearch.doctor import DOCTOR_CONTRACT_VERSION
from websearch.envelope import ENVELOPE_CONTRACT_VERSION
from websearch.layer1_search import SEARCH_CONTRACT_VERSION
from websearch.layer2_extract import EXTRACT_CONTRACT_VERSION, FETCH_CONTRACT_VERSION
from websearch.layer2_format.models import FORMAT_CONTRACT_VERSION, STORE_CONTRACT_VERSION
from websearch.layer3_agentio import AGENTIO_CONTRACT_VERSION
from websearch.searxng_local import SEARXNG_CONTRACT_VERSION
from websearch.tool_arxiv import ARXIV_CONTRACT_VERSION
from websearch.tool_github import GITHUB_CONTRACT_VERSION

ROOT = pathlib.Path(__file__).resolve().parents[1]

PAIRS = [
    ("envelope.schema.json", ENVELOPE_CONTRACT_VERSION),
    ("search.schema.json", SEARCH_CONTRACT_VERSION),
    ("fetch.schema.json", FETCH_CONTRACT_VERSION),
    ("extract.schema.json", EXTRACT_CONTRACT_VERSION),
    ("format.schema.json", FORMAT_CONTRACT_VERSION),
    ("store.schema.json", STORE_CONTRACT_VERSION),
    ("agent-io.schema.json", AGENTIO_CONTRACT_VERSION),
    ("arxiv.schema.json", ARXIV_CONTRACT_VERSION),
    ("github.schema.json", GITHUB_CONTRACT_VERSION),
    ("doctor.schema.json", DOCTOR_CONTRACT_VERSION),
    ("searxng.schema.json", SEARXNG_CONTRACT_VERSION),
]


@pytest.mark.parametrize(("schema_file", "constant"), PAIRS)
def test_schema_version_matches_the_constant(schema_file, constant):
    schema = json.loads((ROOT / "contracts" / schema_file).read_text())
    assert schema["x-contract-version"] == constant, (
        f"{schema_file} declares {schema['x-contract-version']} but the Python constant"
        f" says {constant}; bump both together"
    )


def test_every_schema_has_a_lockstep_pair():
    covered = {name for name, _ in PAIRS}
    on_disk = {p.name for p in (ROOT / "contracts").glob("*.schema.json")}
    assert on_disk == covered, (
        f"schemas without a lockstep assertion: {sorted(on_disk - covered)};"
        f" stale pairs: {sorted(covered - on_disk)}"
    )
