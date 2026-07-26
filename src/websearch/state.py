"""Where this tool keeps state between runs.

Every command is its own process, so anything that has to survive one (the SearXNG
checkout, the page index ``web-open`` reads) needs an agreed place on disk. The rule is
the same for all of it: an explicit setting wins, then the directory holding the
configured env file (a container that mounts its config directory keeps its state across
runs that way), then the XDG cache.
"""

from __future__ import annotations

import os
from pathlib import Path

from .optional_layers import ENV_FILE_VAR, OFF_WORDS

STATE_DIR_VAR = "WEBSEARCH_STATE_DIR"
PERSIST_PATH_VAR = "WEBSEARCH_PERSIST_PATH"

PAGES_FILE = "pages.json"


def state_dir() -> Path:
    """The directory for anything that outlives a single command."""
    explicit = os.environ.get(STATE_DIR_VAR)
    if explicit:
        return Path(explicit).expanduser().resolve()
    env_file = os.environ.get(ENV_FILE_VAR)
    if env_file:
        return Path(env_file).expanduser().resolve().parent
    cache = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return (Path(cache) / "websearch").resolve()


def is_off(value: str | None) -> bool:
    """Whether a CLI value means 'turn this off' (shared vocabulary with the layers)."""
    return value is not None and value.strip().lower() in OFF_WORDS


def default_persist_path() -> str:
    """Where the page index lives when nobody named a file.

    Persisting by default is what makes ``web-fetch`` in one command and ``web-open`` in
    the next resolve the same handle: with no long-running process to hold the index in
    memory, an unshared store would make ``web-open`` useless. Pass ``--persist-path off``
    for a run that should leave nothing behind.
    """
    return str(state_dir() / PAGES_FILE)


def persist_path(cli_value: str | None = None) -> str | None:
    """The page-index file for this run: the flag, then the variable, then the default.

    None means in-memory, which only happens when the caller explicitly asks for it.
    """
    if cli_value is not None:
        return None if is_off(cli_value) else cli_value
    from_env = os.environ.get(PERSIST_PATH_VAR)
    if from_env is not None:
        return None if is_off(from_env) else from_env
    return default_persist_path()
