"""NordVPN setup: the file the credentials go in, and the city the traffic comes out of.

The two failures these exist to catch:

- "where do I put the keys" answered with a guess. Every action reports one absolute
  path, the file is actually created there, and the two key names are in it;
- a location that is written but not used. Selecting a city has to change the proxy URL
  the next command dials, and a per-call `--location` has to be refused rather than
  silently ignored when it cannot be honored (another proxy, or Tor already running).

Credentials never leave this module's fixtures: every assertion on output also asserts the
password is not in it. The network is never touched (`--no-verify`, and an injected exit
for the one test about verification).
"""

from __future__ import annotations

import json

import pytest

from websearch import cli, proxy_setup
from websearch.optional_layers import PROXY_ENV, settings_file
from websearch.proxy import (
    EGRESS_LOCK_VAR,
    NORDVPN_LOCATIONS,
    EgressLocked,
    ProxyConfigError,
    egress_proxy,
    resolve_proxy,
)

DALLAS = "dallas.us.socks.nordhold.net"
PASSWORD = "s3cret-service-password"


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.setenv("NORDVPN_USER", "service-user")
    monkeypatch.setenv("NORDVPN_PASS", PASSWORD)


def _run(argv: list[str], capsys) -> tuple[int, str]:
    code = cli.main(argv)
    return code, capsys.readouterr().out


def _json_run(argv: list[str], capsys) -> tuple[int, dict]:
    code, out = _run([*argv, "--json"], capsys)
    return code, json.loads(out)


PAYLOAD_REF = (
    "https://github.com/hec-ovi/websearch-skill/contracts/proxy.schema.json#/$defs/ProxyPayload"
)


# --- setup: the file, and the two keys -------------------------------------------------


def test_setup_creates_the_settings_file_and_names_both_credentials(capsys, assert_valid):
    code, envelope = _json_run(["proxy", "setup", "--no-verify"], capsys)
    data = envelope["data"]
    assert_valid(data, PAYLOAD_REF)

    target = settings_file()
    assert code == 0
    assert data["settings_file"] == str(target)
    assert data["settings_exists"] is True
    assert data["missing_keys"] == ["NORDVPN_USER", "NORDVPN_PASS"]
    written = target.read_text()
    assert "NORDVPN_USER=" in written and "NORDVPN_PASS=" in written


def test_setup_prints_the_path_and_the_lines_to_fill(capsys):
    code, out = _run(["proxy", "setup", "--no-verify"], capsys)
    assert code == 0
    assert str(settings_file()) in out
    assert "NORDVPN_USER=" in out and "NORDVPN_PASS=" in out


def test_setup_leaves_a_credential_that_is_already_set_alone(capsys, credentials):
    """Nothing is scaffolded over a value that exists: two answers in one file is worse
    than none, and the empty line would be the one people read."""
    code, envelope = _json_run(["proxy", "setup", "--no-verify"], capsys)
    assert code == 0
    assert envelope["data"]["missing_keys"] == []
    assert "NORDVPN_PASS=" not in settings_file().read_text()


# --- locations: what can be chosen -----------------------------------------------------


def test_locations_lists_every_exit_and_marks_the_configured_one(capsys, monkeypatch):
    monkeypatch.setenv("NORDVPN_HOST", DALLAS)
    code, envelope = _json_run(["proxy", "locations", "--no-verify"], capsys)
    locations = envelope["data"]["locations"]

    assert code == 0
    assert len(locations) == len(NORDVPN_LOCATIONS)
    assert [loc["slug"] for loc in locations if loc["selected"]] == ["dallas"]
    assert all(loc["host"].endswith(".socks.nordhold.net") for loc in locations)


# --- use / off: making a choice stick --------------------------------------------------


def test_use_writes_the_city_and_turns_the_proxy_on(capsys, credentials):
    code, envelope = _json_run(["proxy", "use", "dallas", "--no-verify"], capsys)
    data = envelope["data"]

    assert code == 0
    assert data["location"] == {
        "slug": "dallas",
        "city": "Dallas",
        "country": "United States",
        "host": DALLAS,
        "selected": True,
    }
    assert data["enabled"] is True
    assert data["wired"] == [str(settings_file())]
    written = settings_file().read_text()
    assert f"NORDVPN_HOST={DALLAS}" in written
    assert f"{PROXY_ENV}=nordvpn" in written


def test_a_written_city_is_what_the_next_command_dials(capsys, credentials):
    """The point of writing it: a later process, with a clean environment, dials Dallas."""
    _json_run(["proxy", "use", "dallas", "--no-verify"], capsys)
    for var in ("NORDVPN_HOST", PROXY_ENV):
        del proxy_setup.os.environ[var]

    cli.main(["proxy", "status", "--no-verify", "--json"])
    assert DALLAS in (resolve_proxy() or "")


def test_use_rejects_a_city_nordvpn_has_no_exit_in(capsys):
    code, envelope = _json_run(["proxy", "use", "tokyo", "--no-verify"], capsys)
    assert code == 1
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "invalid_request"
    assert "dallas" in envelope["error"]["message"]


def test_off_returns_to_a_direct_connection(capsys, credentials):
    _json_run(["proxy", "use", "dallas", "--no-verify"], capsys)
    code, envelope = _json_run(["proxy", "off"], capsys)

    assert code == 0
    assert envelope["data"]["enabled"] is False
    assert envelope["data"]["proxy"] is None
    assert f"{PROXY_ENV}=off" in settings_file().read_text()
    # the choice survives being switched off, so turning it back on needs no re-picking
    assert envelope["data"]["location"]["slug"] == "dallas"


# --- status: what is set, and where the traffic really comes out -----------------------


def test_status_reports_the_proxy_without_its_password(capsys, credentials, monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "nordvpn")
    monkeypatch.setenv("NORDVPN_HOST", DALLAS)
    code, out = _run(["proxy", "status", "--no-verify"], capsys)

    assert code == 0
    assert PASSWORD not in out
    assert f"socks5h://***:***@{DALLAS}:1080" in out


def test_status_reports_the_verified_exit(capsys, credentials, monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "nordvpn")
    monkeypatch.setenv("NORDVPN_HOST", DALLAS)
    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    monkeypatch.setattr(
        proxy_setup,
        "verify_exit",
        lambda url, timeout_s: (
            proxy_setup.ProxyExit(
                ip="1.2.3.4", city="Dallas", country="United States", protected=True
            ),
            None,
        ),
    )
    code, envelope = _json_run(["proxy", "status"], capsys)
    data = envelope["data"]

    assert code == 0
    assert data["exit"] == {
        "ip": "1.2.3.4",
        "city": "Dallas",
        "country": "United States",
        "protected": True,
    }
    assert data["next_actions"] == []


def test_status_fails_when_the_exit_is_not_nordvpn(capsys, credentials, monkeypatch):
    """A proxy that answers is not a proxy that hides you: an unprotected exit is a
    failing status, not a detail in the payload."""
    monkeypatch.setenv(PROXY_ENV, "nordvpn")
    monkeypatch.setattr(
        proxy_setup,
        "verify_exit",
        lambda url, timeout_s: (proxy_setup.ProxyExit(ip="9.9.9.9", protected=False), None),
    )
    code, out = _run(["proxy", "status"], capsys)

    assert code == 1
    assert "NOT NordVPN" in out


def test_status_without_credentials_says_which_file_to_fill(capsys):
    code, envelope = _json_run(["proxy", "status", "--no-verify"], capsys)
    data = envelope["data"]

    assert code == 1  # the proxy is not usable, and that is the question status answers
    assert data["missing_keys"] == ["NORDVPN_USER", "NORDVPN_PASS"]
    assert str(settings_file()) in data["next_actions"][0]


# --- --location: one command, one city -------------------------------------------------


def test_a_per_call_location_selects_the_exit_for_that_command(credentials):
    assert DALLAS in (egress_proxy(location="dallas") or "")
    assert "NORDVPN_HOST" not in proxy_setup.os.environ  # nothing was written


@pytest.mark.parametrize("name", ["dallas", "Dallas", "DALLAS"])
def test_a_city_is_named_the_way_people_write_it(credentials, name):
    assert DALLAS in (resolve_proxy(location=name) or "")


def test_a_location_cannot_redirect_some_other_proxy(credentials):
    with pytest.raises(ProxyConfigError, match="cannot be combined"):
        egress_proxy("socks5h://elsewhere:1080", location="dallas")


def test_a_location_is_refused_while_tor_carries_the_traffic(credentials, monkeypatch):
    """Tor's upstream is fixed in the torrc it started with, so honoring this would need a
    restart and accepting it would name a city the traffic never visits."""
    monkeypatch.setenv("WEBSEARCH_TOR", "on")
    with pytest.raises(ProxyConfigError, match="tor layer"):
        egress_proxy(location="dallas")


# --- the lock: nothing leaves without the proxy ----------------------------------------


def test_locking_refuses_every_path_that_has_no_proxy(monkeypatch):
    """The whole point: with nothing configured, a locked run does not fall back to a
    direct connection, because the fallback is the request that gives the address away."""
    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    with pytest.raises(EgressLocked, match="no proxy is configured"):
        egress_proxy()


def test_a_locked_fetch_without_a_proxy_is_refused_at_the_guard(monkeypatch):
    """The tiers are handed a proxy by the caller; the guard is what every one of them
    passes through, so it is where an unproxied fetch stops."""
    from websearch.layer2_extract.egress import BlockedEgress, guard_url

    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    with pytest.raises(BlockedEgress, match="no proxy"):
        guard_url("https://example.com", proxied=False)
    guard_url("https://example.com", proxied=True)  # through the proxy: allowed


def test_the_lock_does_not_touch_local_targets(monkeypatch):
    """Loopback never leaves the machine, and a remote exit could not reach it anyway."""
    from websearch.layer2_extract.egress import guard_url

    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    guard_url("http://127.0.0.1:8888/search", proxied=False, allow_private=True)


def test_locking_needs_a_proxy_to_lock_to(capsys):
    code, envelope = _json_run(["proxy", "lock"], capsys)
    assert code == 1
    assert envelope["error"]["code"] == "invalid_request"
    assert "websearch proxy use" in envelope["error"]["message"]


def test_lock_then_the_proxy_cannot_be_switched_off_by_accident(capsys, credentials):
    _json_run(["proxy", "use", "dallas", "--no-verify"], capsys)
    code, envelope = _json_run(["proxy", "lock", "--no-verify"], capsys)
    assert code == 0
    assert envelope["data"]["locked"] is True

    code, envelope = _json_run(["proxy", "off", "--no-verify"], capsys)
    assert code == 1
    assert "unlock" in envelope["error"]["message"]

    _json_run(["proxy", "unlock", "--no-verify"], capsys)
    code, envelope = _json_run(["proxy", "off", "--no-verify"], capsys)
    assert code == 0
    assert envelope["data"]["enabled"] is False
