"""The three optional layers: off by default, and never leaking credentials."""

from __future__ import annotations

import pytest

from websearch.optional_layers import (
    PROXY_ENV,
    SEARXNG_ENV,
    VPN_ENV,
    layer_states,
    load_env_file,
    proxy_state,
    redact_url,
    resolved_proxy_url,
    scrub,
    scrub_proxy,
    searxng_state,
    secrets_of,
    vpn_state,
)

ALL_ENV = (VPN_ENV, PROXY_ENV, SEARXNG_ENV, "NORDVPN_USER", "NORDVPN_PASS", "NORDVPN_HOST")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ALL_ENV:
        monkeypatch.delenv(var, raising=False)


# --- default off --------------------------------------------------------------------


def test_every_layer_is_off_with_a_bare_environment():
    states = layer_states()
    assert sorted(states) == ["proxy", "searxng", "tor", "vpn"]
    assert [s.enabled for s in states.values()] == [False] * len(states)
    assert all(s.source is None and s.value is None for s in states.values())


@pytest.mark.parametrize("word", ["", "off", "none", "no", "false", "0", "direct", "OFF", " off "])
def test_off_words_keep_each_layer_off(monkeypatch, word):
    for var in (VPN_ENV, PROXY_ENV, SEARXNG_ENV):
        monkeypatch.setenv(var, word)
    states = layer_states()
    assert [s.enabled for s in states.values()] == [False] * len(states)


def test_an_off_layer_says_how_to_turn_it_on():
    states = layer_states()
    assert VPN_ENV in states["vpn"].detail
    assert PROXY_ENV in states["proxy"].detail
    assert SEARXNG_ENV in states["searxng"].detail


def test_a_cli_value_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "http://from-env:3128")
    state = proxy_state("off")
    assert state.enabled is False
    on = proxy_state("http://from-flag:3128")
    assert on.enabled is True
    assert on.value == "http://from-flag:3128"
    assert on.source == "--proxy"


# --- vpn ------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["nordvpn", "NordVPN"])
def test_vpn_nordvpn_is_recognized(monkeypatch, value):
    monkeypatch.setenv(VPN_ENV, value)
    state = vpn_state()
    assert (state.enabled, state.value, state.source, state.error) == (
        True,
        "nordvpn",
        VPN_ENV,
        None,
    )


@pytest.mark.parametrize("value", ["any", "on"])
def test_vpn_any_aliases_collapse_to_any(monkeypatch, value):
    monkeypatch.setenv(VPN_ENV, value)
    assert vpn_state().value == "any"


def test_an_unknown_vpn_is_enabled_but_flagged(monkeypatch):
    monkeypatch.setenv(VPN_ENV, "mullvad")
    state = vpn_state()
    assert state.enabled is True
    assert state.error and "nordvpn" in state.error and "any" in state.error


# --- proxy ----------------------------------------------------------------------------


def test_proxy_value_is_redacted_not_raw(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "socks5h://alice:s3cret@exit.example:1080")
    state = proxy_state()
    assert state.enabled is True
    assert state.value == "socks5h://***:***@exit.example:1080"
    assert "s3cret" not in str(state.as_dict())


def test_nordvpn_shorthand_without_credentials_is_reported_not_raised(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "nordvpn")
    state = proxy_state()
    assert state.enabled is True
    assert state.value is None
    assert state.error and "NORDVPN_USER" in state.error


def test_nordvpn_shorthand_expands_and_stays_redacted(monkeypatch):
    monkeypatch.setenv(PROXY_ENV, "nordvpn")
    monkeypatch.setenv("NORDVPN_USER", "svc-user")
    monkeypatch.setenv("NORDVPN_PASS", "svc-pass")
    state = proxy_state()
    assert state.value == "socks5h://***:***@nl.socks.nordhold.net:1080"
    assert "svc-pass" not in (state.value or "")
    # The unredacted URL is available separately, for the client that has to dial it.
    assert resolved_proxy_url() == "socks5h://svc-user:svc-pass@nl.socks.nordhold.net:1080"


def test_resolved_proxy_url_is_none_when_off_or_broken(monkeypatch):
    assert resolved_proxy_url() is None
    monkeypatch.setenv(PROXY_ENV, "nordvpn")  # credentials missing
    assert resolved_proxy_url() is None


# --- searxng ---------------------------------------------------------------------------


def test_searxng_url_is_normalized(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, "http://127.0.0.1:8888/")
    state = searxng_state()
    assert (state.enabled, state.value, state.error) == (True, "http://127.0.0.1:8888", None)


def test_a_relative_searxng_url_is_flagged(monkeypatch):
    monkeypatch.setenv(SEARXNG_ENV, "127.0.0.1:8888")
    state = searxng_state()
    assert state.enabled is True
    assert state.error and "absolute http(s) URL" in state.error


# --- .env ------------------------------------------------------------------------------


def test_load_env_file_fills_in_unset_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "\n"
        "WEBSEARCH_PROXY=nordvpn\n"
        "export NORDVPN_USER=svc-user\n"
        'NORDVPN_PASS="quoted secret"\n'
        "NOT A PAIR\n"
    )
    loaded = load_env_file(str(env))
    assert loaded == ["WEBSEARCH_PROXY", "NORDVPN_USER", "NORDVPN_PASS"]
    assert proxy_state().value == "socks5h://***:***@nl.socks.nordhold.net:1080"
    assert resolved_proxy_url() == "socks5h://svc-user:quoted%20secret@nl.socks.nordhold.net:1080"


def test_a_missing_env_file_is_not_an_error(tmp_path):
    assert load_env_file(str(tmp_path / "nope.env")) == []


def test_the_cli_reads_the_env_file(tmp_path, monkeypatch):
    """Every command is its own process, so main() reads the file before anything else."""
    env = tmp_path / "custom.env"
    env.write_text(f"{SEARXNG_ENV}=http://127.0.0.1:7777\n")
    monkeypatch.setenv("WEBSEARCH_ENV_FILE", str(env))
    monkeypatch.delenv(SEARXNG_ENV, raising=False)

    from websearch import cli

    cli.main(["doctor", "--check", "runtime"])
    assert searxng_state().value == "http://127.0.0.1:7777"


# --- redaction helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, None),
        ("", ""),
        ("http://proxy.example:3128", "http://proxy.example:3128"),
        ("socks5h://u:p@h:1080", "socks5h://***:***@h:1080"),
        ("socks5h://u@h:1080", "socks5h://***:***@h:1080"),
    ],
)
def test_redact_url(url, expected):
    assert redact_url(url) == expected


def test_scrub_proxy_drops_credentials_and_keeps_the_host():
    url = "socks5h://alice:s3cret@exit.example:1080"
    message = f"ProxyError: could not connect to {url} for alice with s3cret"
    cleaned = scrub_proxy(message, url)
    assert "s3cret" not in cleaned
    assert "alice" not in cleaned
    assert "exit.example:1080" in cleaned  # the part you need to debug survives


def test_scrub_proxy_is_a_no_op_without_a_proxy():
    assert scrub_proxy("ConnectError: nothing to redact", None) == (
        "ConnectError: nothing to redact"
    )


def test_secrets_of_is_the_userinfo():
    assert secrets_of("socks5h://alice:s3cret@exit.example:1080") == ["alice", "s3cret"]
    assert secrets_of("http://proxy.example:3128") == []
    assert secrets_of(None) == []


def test_scrub_replaces_longest_first():
    # "pw" inside "pw-long" must not leave a readable remainder.
    assert scrub("token pw-long and pw", ["pw", "pw-long"]) == "token *** and ***"
