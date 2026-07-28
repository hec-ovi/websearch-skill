"""Where settings come from, and where a bring-up writes them.

The complaint these cover: the tool reads real environment variables first, but with no
user-level file the only fallback was a `.env` in the working directory, so "set it once"
meant re-exporting in every shell or keeping a file per project. The chain now ends at
$XDG_CONFIG_HOME/websearch/.env, and precedence has to stay exactly this: an exported
variable, then WEBSEARCH_ENV_FILE, then ./.env, then the user config file.
"""

from __future__ import annotations

import pytest

from websearch.optional_layers import (
    ENV_FILE_VAR,
    SEARXNG_ENV,
    env_file_candidates,
    load_env_file,
    user_config_dir,
    user_env_file,
    write_env_setting,
)
from websearch.state import PAGES_FILE, default_persist_path, state_dir

PROXY = "WEBSEARCH_PROXY"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # The suite-wide fixture pins WEBSEARCH_ENV_FILE at an absent path; these tests are
    # about the chain itself, so they start from nothing configured.
    monkeypatch.delenv(ENV_FILE_VAR, raising=False)
    for var in (PROXY, SEARXNG_ENV):
        monkeypatch.delenv(var, raising=False)


# --- the chain ------------------------------------------------------------------------


def test_the_user_config_file_is_in_the_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert user_config_dir() == tmp_path / "cfg" / "websearch"
    assert user_env_file() == tmp_path / "cfg" / "websearch" / ".env"
    assert env_file_candidates()[-1] == user_env_file()


def test_an_explicit_env_file_is_the_whole_chain(monkeypatch, tmp_path):
    """Naming a file means that file: nothing else gets read behind it."""
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "one.env"))
    assert env_file_candidates() == [tmp_path / "one.env"]


def test_the_working_directory_outranks_the_user_config_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    user_env_file().parent.mkdir(parents=True)
    user_env_file().write_text(f"{PROXY}=socks5h://user-config:1080\n")
    (tmp_path / ".env").write_text(f"{PROXY}=socks5h://project:1080\n")

    loaded = load_env_file()

    import os

    assert os.environ[PROXY] == "socks5h://project:1080"
    assert loaded.count(PROXY) == 1  # the second file must not report a set it did not do


def test_an_exported_variable_beats_every_file(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv(PROXY, "socks5h://exported:1080")
    user_env_file().parent.mkdir(parents=True)
    user_env_file().write_text(f"{PROXY}=socks5h://user-config:1080\n")

    load_env_file()

    import os

    assert os.environ[PROXY] == "socks5h://exported:1080"


def test_each_file_fills_only_what_the_earlier_ones_left_unset(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / ".env").write_text(f"{PROXY}=socks5h://project:1080\n")
    user_env_file().parent.mkdir(parents=True)
    user_env_file().write_text(
        f"{PROXY}=socks5h://user:1080\n{SEARXNG_ENV}=http://127.0.0.1:8888\n"
    )

    load_env_file()

    import os

    assert os.environ[PROXY] == "socks5h://project:1080"
    assert os.environ[SEARXNG_ENV] == "http://127.0.0.1:8888"


# --- writing back ---------------------------------------------------------------------


def test_a_setting_is_written_to_the_user_config_file_by_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))

    written = write_env_setting(SEARXNG_ENV, "http://127.0.0.1:8888")

    assert written == user_env_file()
    assert written.read_text().strip() == f"{SEARXNG_ENV}=http://127.0.0.1:8888"
    assert written.stat().st_mode & 0o077 == 0  # it sits next to proxy credentials
    assert not (tmp_path / ".env").exists()  # never the project's file


def test_the_configured_env_file_wins_as_the_write_target(monkeypatch, tmp_path):
    target = tmp_path / "conf" / "websearch.env"
    monkeypatch.setenv(ENV_FILE_VAR, str(target))

    assert write_env_setting(SEARXNG_ENV, "http://127.0.0.1:8888") == target


def test_nothing_is_written_under_a_file_that_already_outranks_the_target(monkeypatch, tmp_path):
    """A write the environment would ignore is worse than no write at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / ".env").write_text(f"{SEARXNG_ENV}=http://127.0.0.1:1111\n")

    assert write_env_setting(SEARXNG_ENV, "http://127.0.0.1:8888") is None
    assert not user_env_file().exists()


def test_rewriting_replaces_the_line_instead_of_stacking(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the repo's own .env would outrank the target
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    write_env_setting(SEARXNG_ENV, "http://127.0.0.1:1111")

    write_env_setting(SEARXNG_ENV, "http://127.0.0.1:2222")

    body = user_env_file().read_text()
    assert body.count(SEARXNG_ENV) == 1
    assert "2222" in body and "1111" not in body


# --- state stays out of the config directory ------------------------------------------


def test_writing_a_settings_file_does_not_move_the_state(monkeypatch, tmp_path):
    """Settings are yours; state is regenerable. Moving an install because a settings
    file appeared would strand the SearXNG checkout and the page index of anyone who
    turns a layer on for the first time."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cache = (tmp_path / "cache" / "websearch").resolve()
    assert state_dir() == cache

    user_env_file().parent.mkdir(parents=True)
    user_env_file().write_text(f"{PROXY}=off\n")

    assert state_dir() == cache
    assert default_persist_path() == str(cache / PAGES_FILE)


def test_a_configured_env_file_still_keeps_state_beside_it(monkeypatch, tmp_path):
    """The container case: one mounted directory holds the config and the install."""
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "conf" / "websearch.env"))
    assert state_dir() == (tmp_path / "conf").resolve()
