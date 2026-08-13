"""The tor layer: the switch, the chaining, and the local lifecycle.

Two properties matter more than the rest here and are asserted from several directions:

- off is the default, and stays off for every spelling of off;
- turning Tor on never drops a configured proxy. It becomes Tor's upstream in torrc, so
  the path is you -> proxy -> Tor. A test that only checked "requests go through Tor"
  would pass on an implementation that silently threw the VPN hop away.

The network is never touched: the bundle download, the process, and the check service are
all injected.
"""

from __future__ import annotations

import json

import pytest

from websearch import cli, tor_local
from websearch.optional_layers import (
    PROXY_ENV,
    TOR_ENV,
    layer_states,
    resolved_proxy_url,
    tor_state,
    user_env_file,
)
from websearch.proxy import (
    TOR_PORT_VAR,
    TOR_SOCKS_VAR,
    ProxyConfigError,
    as_fetch_proxy,
    egress_proxy,
    tor_enabled,
    tor_socks_url,
)

SOCKS = "socks5h://127.0.0.1:9050"
PROXY_URL = "socks5h://alice:s3cret@exit.example:1080"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in (TOR_ENV, PROXY_ENV, TOR_SOCKS_VAR, TOR_PORT_VAR, "NORDVPN_USER", "NORDVPN_PASS"):
        monkeypatch.delenv(var, raising=False)


# --- the switch -----------------------------------------------------------------------


def _reachable(value: bool):
    return lambda url=None, timeout_s=2.0: value


def _verdict(is_tor=None, exit_ip=None, error=None):
    return lambda url=None, timeout_s=20.0: {"is_tor": is_tor, "exit_ip": exit_ip, "error": error}


def test_the_layer_is_off_with_a_bare_environment():
    state = tor_state()
    assert state.enabled is False
    assert state.value is None
    assert "websearch tor up" in state.detail
    assert tor_enabled() is False
    assert egress_proxy() is None


def test_an_off_word_keeps_it_off(monkeypatch):
    # The full OFF_WORDS vocabulary is proven once, in test_optional_layers.
    monkeypatch.setenv(TOR_ENV, "off")
    assert tor_enabled() is False
    assert tor_state().enabled is False


@pytest.mark.parametrize("word", ["on", "tor"])
def test_on_words_turn_it_on(monkeypatch, word):
    monkeypatch.setenv(TOR_ENV, word)
    assert tor_enabled() is True
    state = tor_state()
    assert state.enabled and state.value == SOCKS


def test_a_value_that_is_neither_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setenv(TOR_ENV, "socks5h://127.0.0.1:9050")
    with pytest.raises(ProxyConfigError):
        tor_enabled()
    state = tor_state()
    assert state.enabled and state.error and TOR_SOCKS_VAR in state.error


def test_the_socks_address_follows_the_port_and_the_override(monkeypatch):
    monkeypatch.setenv(TOR_PORT_VAR, "9150")
    assert tor_socks_url() == "socks5h://127.0.0.1:9150"
    monkeypatch.setenv(TOR_SOCKS_VAR, "socks5h://tor-sidecar:9050")
    assert tor_socks_url() == "socks5h://tor-sidecar:9050"


def test_a_socks_address_that_is_not_socks_is_refused(monkeypatch):
    monkeypatch.setenv(TOR_SOCKS_VAR, "http://127.0.0.1:9050")
    with pytest.raises(ProxyConfigError):
        tor_socks_url()


# --- tor and the proxy, together ------------------------------------------------------


def test_tor_answers_first_for_egress_but_the_proxy_layer_stays_on(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    monkeypatch.setenv(TOR_ENV, "on")

    assert egress_proxy() == SOCKS
    assert resolved_proxy_url() == SOCKS
    layers = layer_states()
    # The proxy is not "overridden": it is the hop Tor dials out through, and the layer
    # state has to keep saying so or the doctor would report a dropped VPN as fine.
    assert (
        layers["proxy"].enabled and layers["proxy"].value == "socks5h://***:***@exit.example:1080"
    )
    assert layers["tor"].enabled and layers["tor"].value == SOCKS
    assert "dials out through socks5h://***:***@exit.example:1080" in layers["tor"].detail


def test_with_tor_off_egress_is_the_proxy(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    assert egress_proxy() == PROXY_URL


def test_a_cli_value_overrides_the_environment_both_ways(monkeypatch):
    monkeypatch.setenv(TOR_ENV, "on")
    assert egress_proxy(None, "off") is None
    monkeypatch.setenv(TOR_ENV, "off")
    assert egress_proxy(None, "on") == SOCKS


def test_the_fetch_proxy_carries_the_tor_flag(monkeypatch):
    monkeypatch.setenv(TOR_ENV, "on")
    assert as_fetch_proxy(SOCKS) == {"url": SOCKS, "type": "socks5", "tor": True}
    monkeypatch.setenv(TOR_ENV, "off")
    assert as_fetch_proxy(PROXY_URL)["tor"] is False


# --- torrc ----------------------------------------------------------------------------


def test_torrc_chains_a_socks_proxy_with_its_credentials():
    lines = tor_local.torrc_upstream("socks5h://alice:s3cret@exit.example:1080")
    assert lines == [
        "Socks5Proxy exit.example:1080",
        "Socks5ProxyUsername alice",
        "Socks5ProxyPassword s3cret",
    ]


def test_torrc_chains_an_http_proxy():
    assert tor_local.torrc_upstream("http://proxy.example:3128") == [
        "HTTPSProxy proxy.example:3128"
    ]
    assert tor_local.torrc_upstream("http://bob:pw@proxy.example:3128") == [
        "HTTPSProxy proxy.example:3128",
        "HTTPSProxyAuthenticator bob:pw",
    ]


def test_torrc_percent_encoded_credentials_are_decoded():
    """The proxy URL is escaped for a URL; torrc takes the raw value."""
    lines = tor_local.torrc_upstream("socks5h://user%40host:p%3Ass@exit.example:1080")
    assert lines[1:] == ["Socks5ProxyUsername user@host", "Socks5ProxyPassword p:ss"]


def test_a_proxy_tor_cannot_dial_through_is_refused_loudly():
    with pytest.raises(tor_local.TorError, match="wireguard"):
        tor_local.torrc_upstream("wireguard://exit.example:51820")


def test_no_proxy_means_no_upstream_lines():
    assert tor_local.torrc_upstream(None) == []


def test_the_written_torrc_carries_the_port_the_data_dir_and_the_upstream(tmp_path):
    paths = tor_local.Paths(tmp_path)
    tor_local.write_torrc(paths, socks_url="socks5h://127.0.0.1:9151", proxy_url=PROXY_URL)

    body = paths.torrc.read_text()
    assert "SocksPort 127.0.0.1:9151" in body
    assert f"DataDirectory {paths.data}" in body
    assert "Socks5Proxy exit.example:1080" in body
    assert "ClientOnly 1" in body
    assert paths.torrc.stat().st_mode & 0o077 == 0  # it holds the upstream's password
    assert paths.data.stat().st_mode & 0o077 == 0  # tor refuses a readable DataDirectory


def test_a_torrc_local_file_is_included_so_edits_survive_regeneration(tmp_path):
    paths = tor_local.Paths(tmp_path)
    (tmp_path / "torrc.local").write_text("ExitNodes {de}\n")

    tor_local.write_torrc(paths, proxy_url=None)

    assert f"%include {tmp_path / 'torrc.local'}" in paths.torrc.read_text()


# --- the bundle -----------------------------------------------------------------------


def test_the_bundle_url_is_built_for_this_platform(monkeypatch):
    monkeypatch.setattr(tor_local.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tor_local.platform, "machine", lambda: "x86_64")
    archive, sums, name = tor_local.bundle_urls("15.0.19")
    assert name == "tor-expert-bundle-linux-x86_64-15.0.19.tar.gz"
    assert archive == f"https://dist.torproject.org/torbrowser/15.0.19/{name}"
    assert sums.endswith("/sha256sums-signed-build.txt")


def test_a_platform_with_no_bundle_says_to_install_tor_instead(monkeypatch):
    monkeypatch.setattr(tor_local.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tor_local.platform, "machine", lambda: "aarch64")
    with pytest.raises(tor_local.TorError, match="package manager"):
        tor_local.bundle_platform()


def test_a_checksum_mismatch_refuses_to_install(monkeypatch, tmp_path):
    """The one thing this download must never do is run an unverified binary."""
    name = "tor-expert-bundle-linux-x86_64-15.0.19.tar.gz"
    monkeypatch.setattr(
        tor_local, "bundle_urls", lambda v: ("http://a/archive", "http://a/sums", name)
    )

    sums = f"{'0' * 64}  {name}\n".encode()
    monkeypatch.setattr(
        tor_local, "_get", lambda url, t: sums if "sums" in url else b"not-a-tarball"
    )

    with pytest.raises(tor_local.TorError, match="does not match its published sha256"):
        tor_local.download_bundle(tor_local.Paths(tmp_path), version="15.0.19")
    assert not (tmp_path / "dist").exists()


def test_a_missing_checksum_entry_refuses_to_install():
    with pytest.raises(tor_local.TorError, match="refusing to run an unchecked binary"):
        tor_local._expected_digest("abc  something-else.tar.gz\n", "wanted.tar.gz")


def test_the_binary_source_order_is_explicit_then_bundle_then_system(monkeypatch, tmp_path):
    paths = tor_local.Paths(tmp_path)
    monkeypatch.setattr(tor_local.shutil, "which", lambda name: "/usr/bin/tor")
    assert tor_local.find_binary(paths)[1] == "system"

    paths.binary.parent.mkdir(parents=True)
    paths.binary.write_text("#!/bin/sh\n")
    assert tor_local.find_binary(paths) == (paths.binary, "bundle")

    monkeypatch.setenv(tor_local.BINARY_VAR, str(tmp_path / "mine"))
    (tmp_path / "mine").write_text("#!/bin/sh\n")
    assert tor_local.find_binary(paths) == (tmp_path / "mine", "explicit")


# --- the lifecycle --------------------------------------------------------------------


@pytest.fixture
def offline(monkeypatch):
    """No listener, no network: the shape every 'up' starts from."""
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(False))
    monkeypatch.setattr(tor_local, "verify", _verdict(error="offline"))


def test_status_reports_off_without_starting_anything(monkeypatch, tmp_path, offline):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "start", _must_not_run)

    env = tor_local.control(tor_local.TorRequest(action="status"))

    assert env.ok
    assert env.data["enabled"] is False
    assert env.data["reachable"] is False
    assert env.data["socks_url"] == SOCKS


def _must_not_run(*a, **k):
    raise AssertionError("this action must not start a process")


def test_up_installs_starts_verifies_and_turns_the_layer_on(monkeypatch, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("WEBSEARCH_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    started: list[str] = []
    reachable = {"value": False}

    monkeypatch.setattr(
        tor_local, "is_reachable", lambda url=None, timeout_s=2.0: reachable["value"]
    )
    monkeypatch.setattr(tor_local, "find_binary", lambda paths: (None, "none"))

    def fake_download(paths, version=None):
        paths.binary.parent.mkdir(parents=True, exist_ok=True)
        paths.binary.write_text("#!/bin/sh\n")
        return paths.binary

    def fake_start(paths, binary, *, source):
        started.append(str(binary))
        reachable["value"] = True
        return 4242

    monkeypatch.setattr(tor_local, "download_bundle", fake_download)
    monkeypatch.setattr(tor_local, "start", fake_start)
    monkeypatch.setattr(
        tor_local, "wait_bootstrapped", lambda paths, t=120: (True, "Bootstrapped 100%")
    )
    monkeypatch.setattr(tor_local, "verify", _verdict(is_tor=True, exit_ip="171.25.193.79"))

    env = tor_local.control(tor_local.TorRequest(action="up"))

    assert env.ok, env.error
    assert started and env.data["is_tor"] is True
    assert env.data["exit_ip"] == "171.25.193.79"
    assert env.data["bootstrap"] == "Bootstrapped 100%"
    # The switch has to survive the process: the next command is a new one.
    assert env.data["wired"] == str(user_env_file())
    assert f"{TOR_ENV}=on" in user_env_file().read_text()


def test_up_against_a_tor_that_already_answers_never_installs(monkeypatch, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(True))
    monkeypatch.setattr(tor_local, "download_bundle", _must_not_run)
    monkeypatch.setattr(tor_local, "start", _must_not_run)
    monkeypatch.setattr(tor_local, "verify", _verdict(is_tor=True, exit_ip="1.2.3.4"))

    env = tor_local.control(tor_local.TorRequest(action="up"))

    assert env.ok and env.data["bootstrap"] == "already listening"


def test_a_port_that_answers_but_is_not_tor_is_an_error(monkeypatch, tmp_path):
    """Reachable is not anonymous. Something else on 9050 must never read as ready."""
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(True))
    monkeypatch.setattr(tor_local, "verify", _verdict(is_tor=False, exit_ip="9.9.9.9"))

    env = tor_local.control(tor_local.TorRequest(action="up"))

    assert not env.ok
    assert "not leaving through Tor" in env.error.message or "not Tor" in env.error.message


def test_a_failed_bootstrap_stops_what_it_started_and_scrubs_the_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(PROXY_ENV, PROXY_URL)
    stopped: list[bool] = []
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(False))
    monkeypatch.setattr(tor_local, "find_binary", lambda paths: (tmp_path / "tor", "system"))
    monkeypatch.setattr(tor_local, "start", lambda paths, binary, *, source: 1)
    monkeypatch.setattr(
        tor_local,
        "wait_bootstrapped",
        lambda paths, t=120: (False, f"could not connect to {PROXY_URL}"),
    )
    monkeypatch.setattr(tor_local, "stop", lambda paths, timeout_s=10.0: stopped.append(True))

    env = tor_local.control(tor_local.TorRequest(action="up"))

    assert not env.ok and stopped == [True]
    assert "s3cret" not in env.error.message and "alice" not in env.error.message


def test_down_turns_the_layer_off_once_nothing_is_listening(monkeypatch, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("WEBSEARCH_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(TOR_ENV, "on")
    monkeypatch.setattr(tor_local, "stop", lambda paths, timeout_s=10.0: True)
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(False))

    env = tor_local.control(tor_local.TorRequest(action="down"))

    assert env.ok
    assert f"{TOR_ENV}=off" in user_env_file().read_text()


def test_down_leaves_the_switch_alone_when_someone_elses_tor_still_answers(monkeypatch, tmp_path):
    """An external Tor is not ours to stop, so claiming the layer is off would be a lie."""
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("WEBSEARCH_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tor_local, "stop", lambda paths, timeout_s=10.0: True)
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(True))

    env = tor_local.control(tor_local.TorRequest(action="down"))

    assert env.ok and env.data["wired"] is None
    assert not user_env_file().exists()


# --- the CLI face ---------------------------------------------------------------------


def test_the_cli_tor_command_prints_the_state_and_exits_zero(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(True))
    monkeypatch.setattr(tor_local, "verify", _verdict(is_tor=True, exit_ip="1.2.3.4"))

    code = cli.main(["tor", "status"])

    out = capsys.readouterr().out
    assert code == 0
    assert "is tor  yes" in out and "1.2.3.4" in out


def test_the_cli_tor_json_face_is_the_envelope(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(False))

    code = cli.main(["tor", "status", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1  # nothing is listening
    assert payload["contract_version"] == tor_local.TOR_CONTRACT_VERSION
    assert payload["meta"]["layer"] == "tor"
    assert payload["data"]["reachable"] is False


def test_the_tor_payload_matches_its_contract(monkeypatch, tmp_path, assert_valid):
    monkeypatch.setenv(tor_local.HOME_VAR, str(tmp_path))
    monkeypatch.setattr(tor_local, "is_reachable", _reachable(False))

    env = tor_local.control(tor_local.TorRequest(action="status"))

    ref = "https://github.com/hec-ovi/websearch-skill/contracts/tor.schema.json#/$defs/TorPayload"
    assert_valid(env.model_dump(mode="json")["data"], ref)
