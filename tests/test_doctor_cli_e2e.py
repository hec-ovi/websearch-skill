"""`websearch doctor` end to end through the real CLI entry point.

The doctor itself is injected (``cli.build_doctor``), so these exercise argument parsing,
the human and --json faces, and the exit-code contract without any network.
"""

from __future__ import annotations

import json

import pytest

from websearch import cli
from websearch.doctor import DOCTOR_CONTRACT_VERSION, DoctorRequest
from websearch.envelope import ok_envelope
from websearch.optional_layers import PROXY_ENV, SEARXNG_ENV, VPN_ENV


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (VPN_ENV, PROXY_ENV, SEARXNG_ENV, "NORDVPN_USER", "NORDVPN_PASS"):
        monkeypatch.delenv(var, raising=False)


def layer(name, enabled=False, value=None, source=None, error=None):
    return {
        "name": name,
        "enabled": enabled,
        "source": source,
        "value": value,
        "detail": f"{name} detail",
        "error": error,
    }


def check(name, group, status, summary="fine", hint=None):
    return {
        "name": name,
        "group": group,
        "status": status,
        "summary": summary,
        "detail": {},
        "hint": hint,
        "elapsed_ms": 1.0,
        "optional_layer": None,
    }


def payload(checks, layers=None):
    counts = {s: sum(1 for c in checks if c["status"] == s) for s in ("ok", "warn", "fail")}
    return {
        "checked_at": "2026-07-25T00:00:00+00:00",
        "layers": layers
        or {
            "vpn": layer("vpn"),
            "proxy": layer("proxy", True, "socks5h://***:***@exit.example:1080", PROXY_ENV),
            "searxng": layer("searxng"),
        },
        "checks": checks,
        "summary": {
            **counts,
            "skipped": sum(1 for c in checks if c["status"] == "skipped"),
            "total": len(checks),
        },
        "warnings": ["optional layer(s) off (the default): searxng, vpn."],
    }


class FakeDoctor:
    def __init__(self, checks, layers=None):
        self._payload = payload(checks, layers)
        self.request: DoctorRequest | None = None

    def run(self, request):
        self.request = request
        return ok_envelope(DOCTOR_CONTRACT_VERSION, self._payload, layer="doctor", healthy=True)


def install(monkeypatch, doctor):
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return doctor

    monkeypatch.setattr(cli, "build_doctor", build, raising=False)
    return captured


HEALTHY = [
    check("runtime", "runtime", "ok", "Python 3.12.13, websearch-skill 0.3.0"),
    check("internet", "egress", "ok", "direct exit 203.0.113.7"),
    check("proxy", "egress", "ok", "exit 198.51.100.4"),
    check("vpn", "vpn", "skipped", "off"),
]


def test_a_healthy_run_exits_zero_and_prints_the_layer_table(monkeypatch, capsys):
    install(monkeypatch, FakeDoctor(HEALTHY))
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "optional layers" in out
    assert "proxy     on" in out
    assert "socks5h://***:***@exit.example:1080" in out
    assert "internet" in out and "direct exit 203.0.113.7" in out
    assert "3 ok, 0 warn, 0 fail, 1 skipped" in out


def test_a_failed_check_exits_one(monkeypatch, capsys):
    checks = [*HEALTHY, check("searxng", "searxng", "fail", "not responding", hint="start it")]
    install(monkeypatch, FakeDoctor(checks))
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "-> start it" in out


def test_warnings_and_skips_do_not_fail_the_run(monkeypatch):
    checks = [check("engines", "engines", "warn"), check("vpn", "vpn", "skipped")]
    install(monkeypatch, FakeDoctor(checks))
    assert cli.main(["doctor"]) == 0


def test_json_emits_the_raw_envelope(monkeypatch, capsys):
    install(monkeypatch, FakeDoctor(HEALTHY))
    assert cli.main(["doctor", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["contract_version"] == DOCTOR_CONTRACT_VERSION
    assert env["meta"]["layer"] == "doctor"
    assert env["data"]["summary"]["total"] == 4


def test_flags_reach_the_request_and_the_builder(monkeypatch):
    doctor = FakeDoctor(HEALTHY)
    captured = install(monkeypatch, doctor)
    cli.main(
        [
            "doctor",
            "--check",
            "proxy",
            "--check",
            "engines",
            "--quick",
            "--baseline",
            "--timeout-ms",
            "5000",
            "--query",
            "probe me",
            "--fetch-url",
            "https://probe.test",
            "--vpn",
            "nordvpn",
            "--proxy",
            "http://p:3128",
            "--searxng-url",
            "http://127.0.0.1:8888",
        ]
    )
    assert doctor.request.checks == ["proxy", "engines"]
    assert doctor.request.quick is True
    assert doctor.request.baseline is True
    assert doctor.request.timeout_ms == 5000
    assert doctor.request.query == "probe me"
    assert doctor.request.fetch_url == "https://probe.test"
    assert captured == {
        "vpn": "nordvpn",
        "proxy": "http://p:3128",
        "tor": None,
        "searxng_url": "http://127.0.0.1:8888",
    }


def test_defaults_leave_every_layer_to_the_environment(monkeypatch):
    from websearch.doctor.models import DEFAULT_FETCH_URL, DEFAULT_QUERY, DEFAULT_TIMEOUT_MS

    doctor = FakeDoctor(HEALTHY)
    captured = install(monkeypatch, doctor)
    cli.main(["doctor"])
    assert captured == {"vpn": None, "proxy": None, "tor": None, "searxng_url": None}
    assert doctor.request.checks is None
    assert doctor.request.quick is False
    # The one request that would leave the egress proxy is opt-in.
    assert doctor.request.baseline is False
    # Omitted flags fall through to the contract's defaults rather than a copy in the parser.
    assert doctor.request.timeout_ms == DEFAULT_TIMEOUT_MS
    assert doctor.request.query == DEFAULT_QUERY
    assert doctor.request.fetch_url == DEFAULT_FETCH_URL


def test_an_out_of_range_timeout_is_a_clean_invalid_request(monkeypatch, capsys):
    install(monkeypatch, FakeDoctor(HEALTHY))
    assert cli.main(["doctor", "--timeout-ms", "10", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False
    assert env["error"]["code"] == "invalid_request"
    assert env["meta"]["layer"] == "doctor"


def test_a_doctor_that_explodes_becomes_an_internal_error_envelope(monkeypatch, capsys):
    class Exploding:
        def run(self, request):
            raise RuntimeError("boom")

    monkeypatch.setattr(cli, "build_doctor", lambda **_: Exploding(), raising=False)
    assert cli.main(["doctor", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "internal_error"
    assert env["contract_version"] == DOCTOR_CONTRACT_VERSION


def test_doctor_is_listed_in_the_cli_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    assert "doctor" in capsys.readouterr().out
