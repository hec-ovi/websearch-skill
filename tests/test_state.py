"""State that outlives one command: where it goes, and that web-open can read it back.

Every command is its own process, so the page index has to be on disk by default or
``web-open`` could never resolve a handle ``web-fetch`` produced. These run the real CLI
twice, exactly as a caller would.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from tests.conftest import ARTICLE_HTML
from websearch import cli
from websearch.state import PERSIST_PATH_VAR, STATE_DIR_VAR, default_persist_path, persist_path

FETCH_URL = "https://example.test/article"


# --- where state goes -------------------------------------------------------------------


def test_state_lives_beside_the_configured_env_file(tmp_path, monkeypatch):
    # A container that mounts its config directory keeps its state across runs that way.
    env_file = tmp_path / "config" / "websearch.env"
    monkeypatch.setenv("WEBSEARCH_ENV_FILE", str(env_file))
    monkeypatch.delenv(STATE_DIR_VAR, raising=False)

    assert pathlib.Path(default_persist_path()).parent == env_file.parent


def test_an_explicit_state_dir_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(STATE_DIR_VAR, str(tmp_path / "elsewhere"))
    assert pathlib.Path(default_persist_path()).parent == tmp_path / "elsewhere"


@pytest.mark.parametrize("off", ["off", "none", "no", "false", "0", ""])
def test_the_store_can_be_turned_off(off, monkeypatch):
    assert persist_path(off) is None
    monkeypatch.setenv(PERSIST_PATH_VAR, off)
    assert persist_path() is None


def test_precedence_is_flag_then_variable_then_default(tmp_path, monkeypatch):
    monkeypatch.setenv(PERSIST_PATH_VAR, str(tmp_path / "from-env.json"))
    assert persist_path(str(tmp_path / "from-flag.json")) == str(tmp_path / "from-flag.json")
    assert persist_path() == str(tmp_path / "from-env.json")
    monkeypatch.delenv(PERSIST_PATH_VAR)
    assert persist_path() == default_persist_path()


# --- the reason it is on by default -----------------------------------------------------


def test_web_open_resolves_a_handle_from_a_previous_command(httpx_mock, capsys):
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    assert cli.main(["web-fetch", FETCH_URL, "--page-size-tokens", "40", "--json"]) == 0
    fetched = json.loads(capsys.readouterr().out)
    handle = fetched["data"]["pages"][0]["handle"]

    # A second process, no flags on either side, and no second request to the network.
    assert cli.main(["web-open", handle, "--page", "2", "--page-size-tokens", "40", "--json"]) == 0
    opened = json.loads(capsys.readouterr().out)

    page = opened["data"]["pages"][0]
    assert opened["ok"] is True
    assert page["source"] == "cache"
    assert page["page"] == 2
    assert page["content"]


def test_persist_off_leaves_nothing_on_disk_and_web_open_says_so(httpx_mock, capsys, monkeypatch):
    monkeypatch.setenv(PERSIST_PATH_VAR, "off")
    httpx_mock.add_response(url=FETCH_URL, html=ARTICLE_HTML)
    assert cli.main(["web-fetch", FETCH_URL, "--json"]) == 0
    handle = json.loads(capsys.readouterr().out)["data"]["pages"][0]["handle"]

    assert not pathlib.Path(default_persist_path()).exists()

    assert cli.main(["web-open", handle, "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error"]["code"] == "not_opened"  # actionable, not a crash
