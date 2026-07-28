"""`websearch init` end to end through the real CLI entry point.

The two subsystems init orchestrates are faked at their module boundary (``searxng_local
.control`` and ``doctor.build_doctor``), so everything between the argv and the Envelope
is the real code: request validation, the step sequence, the in-process wiring of
WEBSEARCH_SEARXNG_URL, the capability derivation, and the exit-code contract.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import INIT_PAYLOAD_REF, INIT_RESPONSE_REF
from websearch import cli
from websearch.envelope import error_envelope, ok_envelope
from websearch.initialize import INIT_CONTRACT_VERSION, InitRequest
from websearch.optional_layers import PROXY_ENV, SEARXNG_ENV, VPN_ENV
from websearch.searxng_local import SEARXNG_CONTRACT_VERSION

URL = "http://127.0.0.1:8888"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (VPN_ENV, PROXY_ENV, SEARXNG_ENV, "NORDVPN_USER", "NORDVPN_PASS"):
        monkeypatch.delenv(var, raising=False)


# --- the two faked boundaries ---------------------------------------------------------


def searxng_up(healthy=True, version="2026.7.25", active=83, available=278, wired=None):
    def control(request):
        return ok_envelope(
            SEARXNG_CONTRACT_VERSION,
            {
                "action": "up",
                "home": "/state/searxng",
                "url": URL,
                "installed": True,
                "pid": 4242,
                "healthy": healthy,
                "wired": wired,
                "version": version if healthy else None,
                "engines_active": active if healthy else None,
                "engines_available": available if healthy else None,
                "log": "/state/searxng/searxng.log",
            },
            layer="searxng",
            backend="searxng-local",
        )

    return control


def searxng_fails(message="cloning SearXNG needs 'git', which is not installed"):
    def control(request):
        return error_envelope(
            SEARXNG_CONTRACT_VERSION,
            code="dependency_missing",
            message=message,
            retriable=False,
            layer="searxng",
            backend="searxng-local",
        )

    return control


def check(name, group, status, summary="fine", detail=None, hint=None):
    return {
        "name": name,
        "group": group,
        "status": status,
        "summary": summary,
        "detail": detail or {},
        "hint": hint,
        "elapsed_ms": 1.0,
        "optional_layer": None,
    }


def layer(name, enabled=False, value=None):
    return {
        "name": name,
        "enabled": enabled,
        "source": name.upper() if enabled else None,
        "value": value,
        "detail": f"{name} detail",
        "error": None,
    }


HEALTHY_CHECKS = [
    check("runtime", "runtime", "ok"),
    check("internet", "egress", "ok"),
    check("proxy", "egress", "skipped"),
    check("tor", "tor", "skipped"),
    check("vpn", "vpn", "skipped"),
    check("searxng", "searxng", "ok", "answering, 83 engines"),
    check(
        "engines",
        "engines",
        "ok",
        "9 of 9 providers answered",
        {"answered": ["google", "brave"], "silent": []},
    ),
    check("tool:arxiv", "tools", "ok"),
    check("tool:github", "tools", "ok"),
    check("fetch:http", "fetch", "ok"),
]


class FakeDoctor:
    """Records the environment it was built with, then returns canned checks."""

    built_with_searxng: str | None = None

    def __init__(self, checks, layers=None):
        self._checks = checks
        self._layers = layers

    def run(self, request):
        self.last_request = request
        counts = {
            s: sum(1 for c in self._checks if c["status"] == s) for s in ("ok", "warn", "fail")
        }
        payload = {
            "checked_at": "2026-07-25T00:00:00+00:00",
            "layers": self._layers
            or {
                "vpn": layer("vpn"),
                "proxy": layer("proxy"),
                "tor": layer("tor"),
                "searxng": layer("searxng", True, URL),
            },
            "checks": self._checks,
            "summary": {
                **counts,
                "skipped": sum(1 for c in self._checks if c["status"] == "skipped"),
                "total": len(self._checks),
            },
            "warnings": [],
        }
        from websearch.doctor import DOCTOR_CONTRACT_VERSION

        return ok_envelope(
            DOCTOR_CONTRACT_VERSION,
            payload,
            layer="doctor",
            backend=None,
            healthy=not counts["fail"],
        )


@pytest.fixture
def wire(monkeypatch):
    """Point init's two boundaries at fakes and hand back the doctor that was built."""

    def _wire(checks=None, layers=None, control=None):
        built: dict = {}

        def build_doctor(**kwargs):
            import os

            built["searxng_env"] = os.environ.get(SEARXNG_ENV)
            built["doctor"] = FakeDoctor(checks if checks is not None else HEALTHY_CHECKS, layers)
            return built["doctor"]

        monkeypatch.setattr("websearch.doctor.build_doctor", build_doctor)
        monkeypatch.setattr("websearch.searxng_local.control", control or searxng_up())
        return built

    return _wire


def run_json(args=()) -> dict:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(["init", "--json", *args])
    return {"code": code, "envelope": json.loads(buf.getvalue())}


# --- the happy path -------------------------------------------------------------------


def test_ready_when_everything_answers(wire, assert_valid):
    wire()
    out = run_json()

    assert out["code"] == 0
    env = out["envelope"]
    assert_valid(env, INIT_RESPONSE_REF)
    assert_valid(env["data"], INIT_PAYLOAD_REF)
    assert env["contract_version"] == INIT_CONTRACT_VERSION
    assert env["meta"]["layer"] == "init"
    assert env["meta"]["ready"] is True

    data = env["data"]
    assert data["ready"] is True and data["state"] == "ready"
    assert data["next_actions"] == []
    assert [s["name"] for s in data["steps"]] == ["env", "tor", "searxng", "doctor"]
    assert data["capabilities"] == {
        "web_search": "ok",
        "web_fetch": "ok",
        "arxiv": "ok",
        "github": "ok",
        "searxng": "ok",
        "proxy": "off",
        "vpn": "off",
        "tor": "off",
    }
    assert data["engines"]["searxng_engines_active"] == 83
    assert "search online" in data["headline"]


def test_the_doctor_sweep_comes_back_in_the_same_envelope(wire):
    wire()
    data = run_json()["envelope"]["data"]
    # One call, not two: the caller never needs a second diagnostic.
    assert data["doctor"]["summary"]["total"] == len(HEALTHY_CHECKS)
    assert {c["name"] for c in data["doctor"]["checks"]} == {c["name"] for c in HEALTHY_CHECKS}


# --- the wiring that the MCP face used to get wrong ------------------------------------


def test_searxng_is_wired_into_this_process_before_the_sweep_runs(wire):
    built = wire()
    run_json()
    # The URL has to be live in THIS process by the time the search stack is built, or the
    # sweep measures a fanout the caller was told it has.
    assert built["searxng_env"] == URL


def test_a_variable_written_to_the_env_file_after_startup_is_picked_up(wire, tmp_path, monkeypatch):
    # The case a long-running process gets wrong: another command wrote the variable to
    # the file after this process read it. init re-reads, so the run sees it.
    from websearch import initialize

    env_file = tmp_path / "websearch.env"
    monkeypatch.setenv("WEBSEARCH_ENV_FILE", str(env_file))
    env_file.write_text(f"{SEARXNG_ENV}=http://127.0.0.1:9999\n")
    wire(control=searxng_fails())

    data = initialize.run().model_dump(mode="json")["data"]

    env_step = next(s for s in data["steps"] if s["name"] == "env")
    assert env_step["detail"]["path"] == str(env_file)
    assert SEARXNG_ENV in env_step["detail"]["loaded"]


def test_the_env_step_names_the_file_it_actually_read(wire, tmp_path, monkeypatch):
    # With nothing configured the loader still reads ./.env, so the step has to say which
    # file it read rather than "no env file configured" while having read one.
    from websearch import initialize

    monkeypatch.delenv("WEBSEARCH_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{SEARXNG_ENV}=http://127.0.0.1:9999\n")
    wire()

    step = next(
        s for s in initialize.run().model_dump(mode="json")["data"]["steps"] if s["name"] == "env"
    )
    assert step["detail"]["exists"] is True
    assert step["summary"].startswith("read .env")
    assert SEARXNG_ENV in step["detail"]["loaded"]


def test_the_env_step_says_so_when_there_is_no_file(wire, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_ENV_FILE", str(tmp_path / "absent.env"))
    wire()

    from websearch import initialize

    step = next(
        s for s in initialize.run().model_dump(mode="json")["data"]["steps"] if s["name"] == "env"
    )
    assert step["detail"]["exists"] is False
    assert step["summary"].startswith("no settings file yet")


def test_the_env_step_reports_variable_names_and_never_their_values(wire, tmp_path, monkeypatch):
    from websearch import initialize

    env_file = tmp_path / "websearch.env"
    env_file.write_text(f"{PROXY_ENV}=socks5h://user:hunter2@exit.example:1080\n")
    monkeypatch.setenv("WEBSEARCH_ENV_FILE", str(env_file))
    wire()

    raw = json.dumps(initialize.run().model_dump(mode="json"))
    assert PROXY_ENV in raw  # the name is useful
    assert "hunter2" not in raw  # the value is a credential


# --- degraded and broken ---------------------------------------------------------------


def test_searxng_failing_to_start_is_degraded_not_broken(wire):
    checks = [c for c in HEALTHY_CHECKS if c["name"] != "searxng"]
    checks.append(check("searxng", "searxng", "skipped", "off"))
    wire(checks=checks, control=searxng_fails())
    out = run_json()

    assert out["code"] == 0  # search still works; a non-zero exit would be a lie
    data = out["envelope"]["data"]
    assert data["ready"] is False and data["state"] == "degraded"
    step = next(s for s in data["steps"] if s["name"] == "searxng")
    assert step["status"] == "warn"
    assert "git" in step["summary"]
    assert data["next_actions"], "a degraded run must say what to do"


def test_no_engine_answering_is_broken_and_exits_1(wire):
    checks = [
        check("internet", "egress", "ok"),
        check("searxng", "searxng", "skipped"),
        check("engines", "engines", "fail", "no engine answered (9 tried)"),
        check("fetch:http", "fetch", "ok"),
        check("tool:arxiv", "tools", "ok"),
        check("tool:github", "tools", "ok"),
    ]
    wire(checks=checks)
    out = run_json()

    assert out["code"] == 1
    data = out["envelope"]["data"]
    assert data["state"] == "broken" and data["ready"] is False
    assert data["capabilities"]["web_search"] == "down"
    assert "no engine answered" in data["headline"]


def test_a_failed_check_carries_its_hint_into_next_actions(wire):
    checks = [
        *[c for c in HEALTHY_CHECKS if c["name"] != "fetch:http"],
        check("fetch:http", "fetch", "fail", "no tier fetched", hint="Check DNS and the proxy."),
    ]
    wire(checks=checks)
    data = run_json()["envelope"]["data"]

    assert data["state"] == "degraded"
    assert data["capabilities"]["web_fetch"] == "down"
    assert "Check DNS and the proxy." in data["next_actions"]
    assert "fetch not working" in data["headline"]


def test_silent_providers_are_reported_but_still_ready(wire):
    checks = [
        *[c for c in HEALTHY_CHECKS if c["name"] != "engines"],
        check(
            "engines",
            "engines",
            "warn",
            "5 of 9 providers answered",
            {
                "answered": ["google", "bing", "duckduckgo", "mojeek", "yandex"],
                "silent": ["brave", "yahoo", "startpage", "wikipedia"],
                "reached_via_searxng": ["brave"],
            },
        ),
    ]
    wire(checks=checks)
    data = run_json()["envelope"]["data"]

    # A rotating fanout with a few blocked providers is the normal state of keyless
    # search, not a degradation: the router fuses whatever answered.
    assert data["state"] == "ready"
    assert data["capabilities"]["web_search"] == "ok"
    assert data["engines"]["silent"] == ["brave", "yahoo", "startpage", "wikipedia"]
    assert data["engines"]["reached_via_searxng"] == ["brave"]


# --- request options -------------------------------------------------------------------


def test_skip_searxng_leaves_it_alone_and_still_reads_ready(wire, monkeypatch):
    called = []

    def control(request):
        called.append(request)
        raise AssertionError("searxng=skip must not touch the instance")

    checks = [c for c in HEALTHY_CHECKS if c["name"] != "searxng"]
    checks.append(check("searxng", "searxng", "skipped", "off"))
    wire(checks=checks, control=control)

    data = run_json(["--skip-searxng"])["envelope"]["data"]

    assert not called
    assert data["state"] == "ready"
    assert data["capabilities"]["searxng"] == "off"
    assert next(s for s in data["steps"] if s["name"] == "searxng")["status"] == "skipped"


def test_quick_reports_unprobed_capabilities_as_unknown(wire):
    quick_checks = [
        check("runtime", "runtime", "ok"),
        check("internet", "egress", "ok"),
        check("searxng", "searxng", "ok"),
        check("proxy", "egress", "skipped"),
        check("vpn", "vpn", "skipped"),
    ]
    built = wire(checks=quick_checks)
    data = run_json(["--quick"])["envelope"]["data"]

    assert built["doctor"].last_request.quick is True
    caps = data["capabilities"]
    # "unknown" is not "down": these checks did not run, so nothing was measured.
    assert caps["web_search"] == "unknown"
    assert caps["arxiv"] == "unknown" and caps["github"] == "unknown"
    assert caps["searxng"] == "ok"


def test_timeout_reaches_the_doctor_request(wire):
    built = wire()
    run_json(["--timeout-ms", "30000"])
    assert built["doctor"].last_request.timeout_ms == 30000


def test_an_out_of_range_timeout_is_a_clean_invalid_request(wire, capsys):
    wire()
    code = cli.main(["init", "--timeout-ms", "999", "--json"])
    env = json.loads(capsys.readouterr().out)
    assert code == 1
    assert env["ok"] is False and env["error"]["code"] == "invalid_request"
    assert "Traceback" not in env["error"]["message"]


@pytest.mark.parametrize("bad", [{"searxng": "maybe"}, {"quick": "yes please"}, {"nope": 1}])
def test_an_off_contract_request_is_refused(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        InitRequest(**bad)


# --- the human face ---------------------------------------------------------------------


def test_human_output_lists_steps_capabilities_and_the_headline(wire, capsys):
    wire()
    code = cli.main(["init"])
    out = capsys.readouterr().out

    assert code == 0
    assert "initializing websearch" in out  # said before the slow part, not after
    assert "ok    searxng" in out
    assert "capabilities" in out and "web_search  ok" in out
    assert out.rstrip().endswith(
        next(line for line in out.splitlines() if line.startswith("ready:"))
    )


def test_human_output_prints_next_actions_when_degraded(wire, capsys):
    checks = [c for c in HEALTHY_CHECKS if c["name"] != "searxng"]
    checks.append(check("searxng", "searxng", "skipped", "off"))
    wire(checks=checks, control=searxng_fails())
    cli.main(["init"])
    out = capsys.readouterr().out

    assert "warn  searxng" in out
    assert "degraded:" in out
    assert "  - " in out  # the action list


# --- the tor step -------------------------------------------------------------------


def test_the_tor_step_is_skipped_and_costs_nothing_with_the_layer_off(wire, monkeypatch):
    """Off is the default, and init must not quietly start an anonymity layer."""
    wire()
    monkeypatch.setattr(
        "websearch.tor_local.control",
        lambda request: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    data = run_json([])["envelope"]["data"]

    step = next(s for s in data["steps"] if s["name"] == "tor")
    assert step["status"] == "skipped"
    assert "WEBSEARCH_TOR" in step["summary"]


def test_the_tor_step_brings_tor_up_when_the_layer_is_on(wire, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_TOR", "on")
    called: list = []

    def tor_control(request):
        called.append(request.action)
        return ok_envelope(
            "1.0.0",
            {
                "action": "up",
                "enabled": True,
                "home": "/state/tor",
                "socks_url": "socks5h://127.0.0.1:9050",
                "installed": True,
                "reachable": True,
                "is_tor": True,
                "exit_ip": "171.25.193.79",
                "version": "15.0.19",
                "upstream": None,
            },
            layer="tor",
            backend="tor-local",
        )

    wire()
    monkeypatch.setattr("websearch.tor_local.control", tor_control)

    data = run_json([])["envelope"]["data"]

    step = next(s for s in data["steps"] if s["name"] == "tor")
    assert called == ["up"]
    assert step["status"] == "ok"
    assert "socks5h://127.0.0.1:9050" in step["summary"]


def test_a_tor_that_will_not_come_up_is_a_failure_not_a_shrug(wire, monkeypatch):
    """With the layer on, requests that were meant to be anonymous would go out bare."""
    monkeypatch.setenv("WEBSEARCH_TOR", "on")
    wire()
    monkeypatch.setattr(
        "websearch.tor_local.control",
        lambda request: error_envelope(
            "1.0.0",
            code="dependency_missing",
            message="no tor binary",
            retriable=False,
            layer="tor",
            backend="tor-local",
        ),
    )

    data = run_json([])["envelope"]["data"]

    step = next(s for s in data["steps"] if s["name"] == "tor")
    assert step["status"] == "fail"
    assert data["state"] == "degraded"
    assert any("NOT through Tor" in a for a in data["next_actions"])


def test_skip_tor_leaves_a_configured_layer_alone(wire, monkeypatch):
    monkeypatch.setenv("WEBSEARCH_TOR", "on")
    wire()
    monkeypatch.setattr(
        "websearch.tor_local.control",
        lambda request: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    data = run_json(["--skip-tor"])["envelope"]["data"]

    step = next(s for s in data["steps"] if s["name"] == "tor")
    assert step["status"] == "skipped"
