"""The doctor: every check answered from injected fakes, never the real network.

The network boundary is the ``Net`` port, ddgs is faked through the adapter's own
``ddgs_factory`` seam, and the tools/pipeline come in as factories. Nothing here opens a
socket, so the suite still passes on a machine with no internet, which is exactly the
machine most likely to run it.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import DOCTOR_PAYLOAD_REF, DOCTOR_RESPONSE_REF
from websearch.doctor import DoctorRequest, build_doctor, probes
from websearch.envelope import ok_envelope
from websearch.optional_layers import PROXY_ENV, SEARXNG_ENV, VPN_ENV

DIRECT_IP = "203.0.113.7"
PROXY_IP = "198.51.100.4"
PROXY_URL = "socks5h://alice:s3cret@exit.example:1080"
REDACTED = "socks5h://***:***@exit.example:1080"
SEARXNG_URL = "http://127.0.0.1:8888"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (VPN_ENV, PROXY_ENV, SEARXNG_ENV, "NORDVPN_USER", "NORDVPN_PASS"):
        monkeypatch.delenv(var, raising=False)


# --- fakes ---------------------------------------------------------------------------


class FakeNet:
    """A ``Net`` backed by a routing table. Unrouted URLs raise, like the real thing."""

    def __init__(self, routes: dict | None = None, proxy_routes: dict | None = None):
        self.routes = routes or {}
        self.proxy_routes = proxy_routes or {}
        self.calls: list[tuple[str, str | None]] = []

    def _lookup(self, url: str, proxy: str | None):
        self.calls.append((url, proxy))
        table = self.proxy_routes if proxy else self.routes
        for prefix, value in table.items():
            if url.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ConnectionError(f"no route for {url} (proxy={proxy})")

    def text(self, url, *, proxy=None, timeout_s=10.0, params=None):
        value = self._lookup(url, proxy)
        return value if isinstance(value, str) else json.dumps(value)

    def json(self, url, *, proxy=None, timeout_s=10.0, params=None):
        value = self._lookup(url, proxy)
        return json.loads(value) if isinstance(value, str) else value


class FakeDdgs:
    def __init__(self, rows=None, error=None):
        self._rows = rows or []
        self._error = error

    def text(self, query, **kwargs):
        if self._error:
            raise self._error
        return list(self._rows)


DDGS_ROWS = [{"title": "Rust", "href": "https://example.com/rust", "body": "ownership"}]


def net_with(*, direct=DIRECT_IP, proxied=PROXY_IP, extra=None, proxy_extra=None) -> FakeNet:
    routes = {echo: direct for echo in probes.IP_ECHOES}
    routes.update(extra or {})
    proxy_routes = {echo: proxied for echo in probes.IP_ECHOES}
    proxy_routes.update(proxy_extra or {})
    return FakeNet(routes, proxy_routes)


def doctor_with(net=None, **kwargs):
    return build_doctor(
        net=net or net_with(),
        ddgs_factory=lambda backend: FakeDdgs(DDGS_ROWS),
        arxiv_factory=lambda **_: FakeTool("papers", [{"arxiv_id": "1"}]),
        github_factory=lambda **_: FakeTool("repos", [{"full_name": "a/b"}]),
        pipeline_factory=lambda **_: FakePipeline(),
        **kwargs,
    )


class FakeTool:
    def __init__(self, key, items, envelope=None):
        self._key = key
        self._items = items
        self._envelope = envelope

    def search(self, request):
        if self._envelope is not None:
            return self._envelope
        return ok_envelope("1.1.0", {self._key: self._items}, layer=self._key)


class FakePipeline:
    def run(self, request, **kwargs):
        return ok_envelope(
            "1.2.0",
            {
                "source": {"status": 200, "fetched_via": "httpx", "blocked": False},
                "result": {"word_count": 120, "title": "Example"},
            },
            layer="extract",
        )


def checks_by_name(envelope) -> dict:
    return {c["name"]: c for c in envelope.model_dump(mode="json")["data"]["checks"]}


# --- default off ----------------------------------------------------------------------


def test_all_three_layers_are_skipped_by_default():
    env = doctor_with().run(DoctorRequest(quick=True))
    data = env.model_dump(mode="json")["data"]
    assert [layer["enabled"] for layer in data["layers"].values()] == [False, False, False]
    checks = checks_by_name(env)
    for name in ("vpn", "proxy", "searxng"):
        assert checks[name]["status"] == "skipped", name
    assert env.ok is True
    assert env.meta.model_extra["healthy"] is True
    assert data["summary"]["fail"] == 0


def test_a_skipped_layer_never_touches_the_network():
    net = net_with()
    doctor_with(net=net).run(DoctorRequest(quick=True))
    # Only the direct IP echo: no proxy call, no nordvpn call, no searxng call.
    assert [url for url, proxy in net.calls if proxy] == []
    assert all("nordvpn" not in url and "8888" not in url for url, _ in net.calls)


def test_quick_drops_the_slow_groups():
    names = set(checks_by_name(doctor_with().run(DoctorRequest(quick=True))))
    assert not any(n.startswith(("engine", "tool:", "fetch:")) for n in names)
    assert {"runtime", "dependencies", "internet", "mcp"} <= names


# --- internet -------------------------------------------------------------------------


def test_internet_reports_the_direct_exit_ip():
    check = checks_by_name(doctor_with().run(DoctorRequest(quick=True)))["internet"]
    assert check["status"] == "ok"
    assert check["detail"]["exit_ip"] == DIRECT_IP


def test_internet_fails_when_every_echo_is_down():
    net = FakeNet({echo: ConnectionError("down") for echo in probes.IP_ECHOES})
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["internet"]
    assert check["status"] == "fail"
    assert len(check["detail"]["errors"]) == len(probes.IP_ECHOES)


# --- proxy ----------------------------------------------------------------------------


def test_proxy_is_ok_when_the_exit_ip_moves(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    env = doctor_with().run(DoctorRequest(quick=True))
    check = checks_by_name(env)["proxy"]
    assert check["status"] == "ok"
    assert check["detail"]["exit_ip"] == PROXY_IP
    assert check["detail"]["proxy"] == REDACTED


def test_proxy_warns_when_the_exit_ip_is_unchanged(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    check = checks_by_name(
        doctor_with(net=net_with(proxied=DIRECT_IP)).run(DoctorRequest(quick=True))
    )["proxy"]
    assert check["status"] == "warn"
    assert "unchanged" in check["summary"]


def test_proxy_fails_when_nothing_gets_through(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    net = net_with(proxy_extra={echo: ConnectionError("refused") for echo in probes.IP_ECHOES})
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["proxy"]
    assert check["status"] == "fail"
    assert check["hint"]


def test_a_broken_proxy_config_is_a_finding_not_a_crash(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "nordvpn")  # no NORDVPN_USER/PASS
    env = doctor_with().run(DoctorRequest(quick=True))
    data = env.model_dump(mode="json")["data"]
    assert data["layers"]["proxy"]["enabled"] is True
    assert "NORDVPN_USER" in data["layers"]["proxy"]["error"]
    assert checks_by_name(env)["proxy"]["status"] == "fail"


def test_no_check_output_ever_carries_the_proxy_password(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    # An error whose text quotes the whole proxy URL, which is what httpx actually does.
    boom = {echo: ConnectionError(f"failed to connect to {PROXY_URL}") for echo in probes.IP_ECHOES}
    env = doctor_with(net=net_with(proxy_extra=boom)).run(DoctorRequest(quick=True))
    dumped = json.dumps(env.model_dump(mode="json"))
    assert "s3cret" not in dumped
    assert "alice" not in dumped
    assert "exit.example" in dumped


# --- vpn ------------------------------------------------------------------------------


def test_vpn_nordvpn_ok_when_protected(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "nordvpn")
    net = net_with(
        extra={probes.NORDVPN_INSIGHTS: {"protected": True, "ip": DIRECT_IP, "country": "Sweden"}}
    )
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "ok"
    assert check["detail"]["country"] == "Sweden"
    assert check["detail"]["path"] == "the host connection"


def test_vpn_nordvpn_fails_when_unprotected(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "nordvpn")
    net = net_with(
        extra={probes.NORDVPN_INSIGHTS: {"protected": False, "ip": DIRECT_IP, "isp": "Home ISP"}}
    )
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "fail"
    assert "NOT behind NordVPN" in check["summary"]
    assert "Home ISP" in check["summary"]


def test_vpn_checks_the_path_the_tool_uses_not_the_host(monkeypatch):
    """With an egress proxy on, the question is whether the PROXY exits through the VPN."""
    monkeypatch.setenv(VPN_ENV, "nordvpn")
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    net = net_with(
        extra={probes.NORDVPN_INSIGHTS: {"protected": False, "ip": DIRECT_IP}},
        proxy_extra={probes.NORDVPN_INSIGHTS: {"protected": True, "ip": PROXY_IP, "country": "NL"}},
    )
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "ok"
    assert check["detail"]["path"] == "the egress proxy"
    assert check["detail"]["exit_ip"] == PROXY_IP


def test_vpn_warns_when_the_status_endpoint_is_unreachable(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "nordvpn")
    net = net_with(extra={probes.NORDVPN_INSIGHTS: ConnectionError("timeout")})
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "warn"


def test_vpn_any_uses_tunnel_interfaces(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "any")
    monkeypatch.setattr(probes, "_tunnel_interfaces", lambda: ["tun0"])
    assert checks_by_name(doctor_with().run(DoctorRequest(quick=True)))["vpn"]["status"] == "ok"

    monkeypatch.setattr(probes, "_tunnel_interfaces", lambda: [])
    check = checks_by_name(doctor_with().run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "fail"

    monkeypatch.setattr(probes, "_tunnel_interfaces", lambda: None)
    assert checks_by_name(doctor_with().run(DoctorRequest(quick=True)))["vpn"]["status"] == "warn"


def test_an_unknown_vpn_name_fails_with_the_accepted_values(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "mullvad")
    check = checks_by_name(doctor_with().run(DoctorRequest(quick=True)))["vpn"]
    assert check["status"] == "fail"
    assert "nordvpn" in check["summary"]


# --- searxng --------------------------------------------------------------------------


def searxng_net(*, health="ok", config=None, search=None) -> FakeNet:
    return net_with(
        extra={
            f"{SEARXNG_URL}/healthz": health,
            f"{SEARXNG_URL}/config": config
            if config is not None
            else {"version": "2026.1.1", "engines": [{"enabled": True}, {"enabled": False}]},
            f"{SEARXNG_URL}/search": search
            if search is not None
            else {"results": [{"url": "https://a.test", "engines": ["google", "bing"]}]},
        }
    )


def test_searxng_ok_reports_version_engines_and_results(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    check = checks_by_name(doctor_with(net=searxng_net()).run(DoctorRequest(quick=True)))["searxng"]
    assert check["status"] == "ok"
    assert check["detail"]["version"] == "2026.1.1"
    assert check["detail"]["engines_active"] == 1
    assert check["detail"]["engines_answered"] == ["bing", "google"]


def test_searxng_fails_when_unreachable(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    net = net_with(extra={f"{SEARXNG_URL}/healthz": ConnectionError("refused")})
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["searxng"]
    assert check["status"] == "fail"
    assert "searxng.sh up" in check["hint"]


def test_searxng_fails_when_the_json_api_is_off(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    net = searxng_net(search=ValueError("403 Forbidden"))
    check = checks_by_name(doctor_with(net=net).run(DoctorRequest(quick=True)))["searxng"]
    assert check["status"] == "fail"
    assert "search.formats" in check["hint"]


def test_searxng_warns_on_zero_results(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    check = checks_by_name(
        doctor_with(net=searxng_net(search={"results": []})).run(DoctorRequest(quick=True))
    )["searxng"]
    assert check["status"] == "warn"


def test_a_loopback_searxng_is_never_sent_through_the_proxy(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    net = searxng_net()
    doctor_with(net=net).run(DoctorRequest(quick=True))
    searxng_calls = [(url, proxy) for url, proxy in net.calls if SEARXNG_URL in url]
    assert searxng_calls and all(proxy is None for _, proxy in searxng_calls)


# --- engines --------------------------------------------------------------------------


def test_every_ddgs_backend_gets_its_own_check():
    env = doctor_with().run(DoctorRequest(checks=["engines"]))
    names = set(checks_by_name(env))
    assert "engine:ddgs" in names  # the default auto path
    for backend in probes.ddgs_backends():
        assert f"engine:ddgs:{backend}" in names


def ddgs_where(silent: set[str]):
    """A backend-keyed fake: named backends raise, everything else answers."""
    return lambda backend: (
        FakeDdgs(error=RuntimeError("no results")) if backend in silent else FakeDdgs(DDGS_ROWS)
    )


def test_one_silent_provider_is_a_warn_not_a_failure():
    dead = probes.ddgs_backends()[0]
    env = build_doctor(net=net_with(), ddgs_factory=ddgs_where({dead})).run(
        DoctorRequest(checks=["engines"])
    )
    checks = checks_by_name(env)
    assert checks["engines"]["status"] == "warn"
    assert checks["engines"]["detail"]["silent"] == [dead]
    assert checks[f"engine:ddgs:{dead}"]["status"] == "warn"
    assert env.model_dump(mode="json")["data"]["summary"]["fail"] == 0


def test_the_aggregate_fails_only_when_nothing_answers():
    everything = {"auto", *probes.ddgs_backends()}
    env = build_doctor(net=net_with(), ddgs_factory=ddgs_where(everything)).run(
        DoctorRequest(checks=["engines"])
    )
    aggregate = checks_by_name(env)["engines"]
    assert aggregate["status"] == "fail"
    assert env.model_dump(mode="json")["data"]["summary"]["fail"] >= 1


def test_the_default_path_working_is_called_out_when_providers_are_silent():
    # Every named provider silent, but `auto` (which rotates internally) still answers.
    env = build_doctor(net=net_with(), ddgs_factory=ddgs_where(set(probes.ddgs_backends()))).run(
        DoctorRequest(checks=["engines"])
    )
    aggregate = checks_by_name(env)["engines"]
    assert aggregate["status"] == "warn"
    assert "default ddgs path still works" in aggregate["summary"]
    assert aggregate["detail"]["default_path"] == "ok"


def test_searxng_joins_the_engine_list_only_when_enabled(monkeypatch):
    assert "engine:searxng" not in checks_by_name(
        doctor_with().run(DoctorRequest(checks=["engines"]))
    )
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    names = checks_by_name(doctor_with(net=searxng_net()).run(DoctorRequest(checks=["engines"])))
    assert "engine:searxng" in names


# --- tools and fetch ------------------------------------------------------------------


def test_tools_and_fetch_tiers_report_their_counts():
    checks = checks_by_name(doctor_with().run(DoctorRequest(checks=["tools", "fetch"])))
    assert checks["tool:arxiv"]["status"] == "ok"
    assert checks["tool:github"]["status"] == "ok"
    assert checks["fetch:http"]["status"] == "ok"
    assert checks["fetch:http"]["detail"]["words"] == 120
    assert checks["fetch:impersonation"]["status"] == "ok"


def test_a_rate_limited_github_is_a_warn_not_a_failure():
    from websearch.envelope import error_envelope

    limited = error_envelope(
        "1.1.0", code="rate_limited", message="try later", retriable=True, layer="github"
    )
    doctor = build_doctor(
        net=net_with(), github_factory=lambda **_: FakeTool("repos", [], envelope=limited)
    )
    check = checks_by_name(doctor.run(DoctorRequest(checks=["tool:github"])))["tool:github"]
    assert check["status"] == "warn"


def test_a_failing_fetch_is_reported_not_raised():
    from websearch.envelope import error_envelope

    class Broken:
        def run(self, request, **kwargs):
            return error_envelope(
                "1.2.0", code="fetch_failed", message="no response", retriable=True, layer="extract"
            )

    doctor = build_doctor(net=net_with(), pipeline_factory=lambda **_: Broken())
    check = checks_by_name(doctor.run(DoctorRequest(checks=["fetch:http"])))["fetch:http"]
    assert check["status"] == "fail"
    assert "fetch_failed" in check["summary"]


def test_a_probe_that_raises_becomes_a_failed_check_not_a_crash():
    class Exploding:
        def run(self, request, **kwargs):
            raise RuntimeError("boom")

    doctor = build_doctor(net=net_with(), pipeline_factory=lambda **_: Exploding())
    check = checks_by_name(doctor.run(DoctorRequest(checks=["fetch:http"])))["fetch:http"]
    assert check["status"] == "fail"
    assert "boom" in check["summary"]


# --- selection ------------------------------------------------------------------------


def test_check_selection_matches_a_name_a_prefix_or_a_group():
    doctor = doctor_with()
    assert set(checks_by_name(doctor.run(DoctorRequest(checks=["proxy"])))) == {"proxy"}
    assert set(checks_by_name(doctor.run(DoctorRequest(checks=["runtime"])))) == {
        "runtime",
        "dependencies",
    }
    only_google = checks_by_name(doctor.run(DoctorRequest(checks=["engine:ddgs:google"])))
    assert set(only_google) == {"engine:ddgs:google", "engines"}


def test_an_explicit_selection_overrides_quick():
    checks = checks_by_name(doctor_with().run(DoctorRequest(quick=True, checks=["tool:arxiv"])))
    assert "tool:arxiv" in checks


# --- contract -------------------------------------------------------------------------


def test_the_envelope_matches_the_doctor_contract(assert_valid, monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    monkeypatch.setenv(VPN_ENV, "nordvpn")
    monkeypatch.setenv(SEARXNG_ENV, SEARXNG_URL)
    net = searxng_net()
    net.routes[probes.NORDVPN_INSIGHTS] = {"protected": True, "ip": DIRECT_IP, "country": "NL"}
    net.proxy_routes[probes.NORDVPN_INSIGHTS] = {"protected": True, "ip": PROXY_IP, "country": "NL"}
    net.proxy_routes[f"{SEARXNG_URL}/"] = ConnectionError("must not be proxied")
    payload = doctor_with(net=net).run(DoctorRequest()).model_dump(mode="json")
    assert_valid(payload, DOCTOR_RESPONSE_REF)
    assert_valid(payload["data"], DOCTOR_PAYLOAD_REF)
    assert payload["meta"]["layer"] == "doctor"


def test_the_summary_counts_every_check():
    payload = doctor_with().run(DoctorRequest()).model_dump(mode="json")["data"]
    summary = payload["summary"]
    assert summary["total"] == len(payload["checks"])
    assert (
        summary["ok"] + summary["warn"] + summary["fail"] + summary["skipped"] == summary["total"]
    )
