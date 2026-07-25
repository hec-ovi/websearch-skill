"""Contract tests for the self-hosted SearXNG stack in ``docker/searxng/``.

The stack is config, not Python, so these tests hold the config to the promises the
rest of the tool makes about it: the JSON API is on, the container cannot write into
the repo, no secret is committed, and the engine list SearXNG is told to enable is
the generated one. The live end-to-end check at the bottom runs only when
``WEBSEARCH_SEARXNG_LIVE`` names a running instance.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess

import pytest
import yaml

from websearch.layer1_search.models import SearchRequest

ROOT = pathlib.Path(__file__).resolve().parents[1]
STACK = ROOT / "docker" / "searxng"
COMPOSE = STACK / "docker-compose.yml"
SETTINGS = STACK / "core-config" / "settings.yml"
SCRIPT = STACK / "searxng.sh"
PROBE = STACK / "tools" / "probe-engines.py"
RENDER = STACK / "tools" / "render-settings.py"
RUNTIME = STACK / ".runtime" / "settings.yml"

BEGIN = "# >>> BEGIN generated engine list"
END = "# <<< END generated engine list"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


@pytest.fixture(scope="module")
def service(compose) -> dict:
    return compose["services"]["searxng"]


@pytest.fixture(scope="module")
def settings() -> dict:
    return yaml.safe_load(SETTINGS.read_text())


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe_module():
    return _load(PROBE, "probe_engines")


@pytest.fixture(scope="module")
def render_module():
    return _load(RENDER, "render_settings")


# --- the container stays inside its box -------------------------------------------


def test_config_is_mounted_read_only(service):
    """The old config let the entrypoint chown the repo's config dir to a container
    uid. A read-only mount is what stops the container writing into the working tree."""
    mounts = [v for v in service["volumes"] if v.startswith("./")]
    assert mounts, "expected the config bind mount"
    for mount in mounts:
        assert mount.endswith(":ro"), f"{mount} must be read-only"
    assert "FORCE_OWNERSHIP=false" in service["environment"]


def test_container_mounts_the_rendered_settings_not_the_tracked_one(service):
    """core-config/settings.yml is tracked, so it must never hold a proxy URL; the
    container reads the rendered copy instead."""
    assert "./.runtime/:/etc/searxng/:ro" in service["volumes"]
    assert not any(v.startswith("./core-config") for v in service["volumes"])


def test_runtime_state_lives_in_a_named_volume(compose, service):
    """Cache/engine state goes to Docker, never to a path inside the repo."""
    assert "searxng-cache:/var/cache/searxng" in service["volumes"]
    assert "searxng-cache" in compose["volumes"]


def test_port_binds_to_loopback_by_default(service):
    (published,) = service["ports"]
    assert published.startswith("${SEARXNG_HOST:-127.0.0.1}:")
    assert "${SEARXNG_PORT:-8888}" in published, "8080 collides too often to be the default"


def test_privileges_are_dropped(service):
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]


def test_healthcheck_uses_a_binary_the_image_has(service):
    test = service["healthcheck"]["test"]
    assert test[0] == "CMD"
    assert test[1] == "wget", "the image ships wget, not curl"
    assert "/healthz" in test[-1]


# --- no committed secrets ---------------------------------------------------------


def test_secret_has_no_default_and_fails_closed(service):
    (secret,) = [e for e in service["environment"] if e.startswith("SEARXNG_SECRET=")]
    assert ":?" in secret, "an unset secret must abort compose, not fall back to a default"
    assert ":-" not in secret, "a default secret would be shared by everyone who clones this"


def test_settings_declares_no_secret_key(settings):
    assert "secret_key" not in settings.get("server", {})


def test_no_secret_is_committed():
    tracked = subprocess.run(
        ["git", "ls-files", "docker/searxng"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "docker/searxng/.env" not in tracked, ".env is machine-local and gitignored"
    for name in tracked:
        body = (ROOT / name).read_text(errors="ignore")
        assert "ultrasecretkey" not in body
        assert "change-me-local-only-placeholder-secret-key" not in body


# --- the settings SearXNG actually needs ------------------------------------------


def test_json_api_is_enabled(settings):
    """Without this the adapter's format=json request gets HTTP 403."""
    assert "json" in settings["search"]["formats"]


def test_limiter_off_and_instance_private(settings):
    assert settings["server"]["limiter"] is False
    assert settings["server"]["public_instance"] is False
    assert settings["use_default_settings"] is True


def test_engine_timeout_ceiling_stays_under_the_client_timeout(settings):
    """SearXNG must answer before the search adapter gives up on it."""
    ceiling_ms = settings["outgoing"]["max_request_timeout"] * 1000
    assert ceiling_ms < SearchRequest(query="q").timeout_ms


def test_connection_pool_is_sized_for_the_wide_fanout(settings):
    """~50 engines per query would queue behind SearXNG's default pool_maxsize of 20."""
    enabled = settings["engines"] or []
    assert settings["outgoing"]["pool_maxsize"] >= len(enabled) or (
        settings["outgoing"]["pool_maxsize"] >= 100
    )
    assert settings["outgoing"]["pool_connections"] >= settings["outgoing"]["pool_maxsize"]


# --- the generated engine list ----------------------------------------------------


def test_generated_block_is_delimited():
    body = SETTINGS.read_text()
    assert body.count(BEGIN) == 1 and body.count(END) == 1
    assert body.index(BEGIN) < body.index(END)


def test_every_generated_entry_enables_one_named_engine(settings):
    entries = settings["engines"] or []
    assert entries, "the generated list is empty: run ./docker/searxng/searxng.sh engines"
    names = [e["name"] for e in entries]
    assert len(names) == len(set(names)), "duplicate engine names would shadow each other"
    assert names == sorted(names)
    for entry in entries:
        assert set(entry) == {"name", "disabled"}, entry
        assert entry["disabled"] is False


def test_generated_list_widens_the_general_fanout(settings):
    """The point of the exercise: SearXNG's stock defaults reach ~6 general engines."""
    assert len(settings["engines"]) >= 50


def test_generated_list_keeps_torrent_and_shadow_library_engines_out(probe_module, settings):
    enabled = {e["name"] for e in settings["engines"]}
    assert not (enabled & probe_module.RESTRICTED), "restricted engines are opt-in only"


# --- the generator itself ---------------------------------------------------------


ROWS = [
    ("bing", "OK", "10 results, 0 answers (python)"),
    ("mojeek", "OK", "10 results, 0 answers (python)"),
    ("startpage", "ERROR", "startpage Suspended: CAPTCHA"),
    ("yep", "EMPTY", "0 results"),
    ("duckduckgo", "OK", "10 results, 0 answers (python)"),
    ("nyaa", "OK", "20 results, 0 answers (linux)"),
]


def test_render_block_enables_every_engine_that_answered(probe_module):
    block = probe_module.render_block(ROWS)
    parsed = yaml.safe_load(block.replace(BEGIN, "").replace(END, ""))
    assert parsed["engines"] == [
        {"name": "bing", "disabled": False},
        {"name": "duckduckgo", "disabled": False},
        {"name": "mojeek", "disabled": False},
    ]
    assert "startpage: ERROR" in block, "a skipped engine is recorded with its reason"
    assert "yep: EMPTY - 0 results" in block


def test_render_block_is_idempotent_under_its_own_output(probe_module):
    """The bug this guards: the generator used to emit only engines that were off in
    /config. Re-running it read back its own overrides, saw them as already enabled,
    and rewrote settings.yml down to a handful of engines."""
    assert probe_module.render_block(ROWS) == probe_module.render_block(ROWS)
    first = yaml.safe_load(probe_module.render_block(ROWS).replace(BEGIN, "").replace(END, ""))
    assert len(first["engines"]) == 3, "the list must not shrink when the engines are already on"


def test_render_block_holds_back_torrent_and_shadow_library_engines(probe_module):
    block = probe_module.render_block(ROWS)
    parsed = yaml.safe_load(block.replace(BEGIN, "").replace(END, ""))
    assert "nyaa" not in {e["name"] for e in parsed["engines"]}
    assert "Held back" in block and "nyaa" in block


def test_include_restricted_opts_them_back_in(probe_module):
    parsed = yaml.safe_load(
        probe_module.render_block(ROWS, include_restricted=True).replace(BEGIN, "").replace(END, "")
    )
    assert "nyaa" in {e["name"] for e in parsed["engines"]}


def test_splice_replaces_only_the_generated_region(probe_module):
    original = SETTINGS.read_text()
    spliced = probe_module.splice(original, f"{BEGIN}\nengines: []\n{END}")
    assert spliced.split(BEGIN)[0] == original.split(BEGIN)[0], "preamble must survive"
    assert yaml.safe_load(spliced)["engines"] == []
    assert yaml.safe_load(spliced)["search"]["formats"] == ["html", "json"]


def test_splice_refuses_a_file_without_markers(probe_module):
    with pytest.raises(SystemExit):
        probe_module.splice("use_default_settings: true\n", "block")


# --- the rendered runtime settings --------------------------------------------------


def test_tracked_settings_carry_the_proxy_marker_and_no_proxy():
    body = SETTINGS.read_text()
    assert body.count(">>> PROXY MARKER <<<") == 1
    assert "proxies:" not in body, "a proxy URL must never reach a tracked file"


def test_render_without_a_proxy_drops_the_marker(render_module):
    out = render_module.render(SETTINGS.read_text(), None)
    assert "PROXY MARKER" not in out
    assert "proxies" not in yaml.safe_load(out)["outgoing"]
    assert yaml.safe_load(out)["search"]["formats"] == ["html", "json"]


def test_render_with_a_proxy_emits_an_outgoing_proxies_entry(render_module):
    out = render_module.render(SETTINGS.read_text(), "socks5h://u:p@exit.test:1080")
    outgoing = yaml.safe_load(out)["outgoing"]
    assert outgoing["proxies"] == {"all://": "socks5h://u:p@exit.test:1080"}
    assert "PROXY MARKER" not in out
    assert outgoing["max_request_timeout"] == 5.0, "the rest of the config must survive"


def test_render_keeps_the_engine_list_intact(render_module):
    out = render_module.render(SETTINGS.read_text(), "http://exit.test:3128")
    assert yaml.safe_load(out)["engines"] == yaml.safe_load(SETTINGS.read_text())["engines"]


def test_render_is_idempotent_in_content(render_module):
    once = render_module.render(SETTINGS.read_text(), "http://exit.test:3128")
    twice = render_module.render(SETTINGS.read_text(), "http://exit.test:3128")
    assert once == twice


@pytest.mark.skipif(not RUNTIME.exists(), reason="no rendered settings; run searxng.sh up")
def test_rendered_settings_are_readable_by_the_capability_dropped_container():
    """The trap this guards: rendering at 0600 makes the entrypoint fail with 'is not a
    valid file'. The container drops ALL capabilities, so its root has no
    CAP_DAC_OVERRIDE and cannot read a file owned by the host user."""
    assert RUNTIME.stat().st_mode & 0o004, "the rendered settings must be world-readable"
    assert RUNTIME.parent.stat().st_mode & 0o001, "and its directory traversable"


def test_runtime_directory_is_gitignored():
    tracked = subprocess.run(
        ["git", "ls-files", "docker/searxng/.runtime"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert not tracked, "the rendered settings may hold credentials and must stay untracked"


# --- the management script --------------------------------------------------------


def test_script_is_executable_and_parses():
    assert os.access(SCRIPT, os.X_OK), "searxng.sh must be executable"
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_script_usage_lists_the_documented_subcommands():
    out = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert out.returncode == 1
    for sub in ("up", "down", "status", "verify", "egress", "engines", "restart", "logs"):
        assert sub in out.stdout


# --- live instance (opt-in) -------------------------------------------------------

LIVE_URL = os.environ.get("WEBSEARCH_SEARXNG_LIVE")


@pytest.mark.skipif(not LIVE_URL, reason="set WEBSEARCH_SEARXNG_LIVE to a running instance")
def test_live_instance_serves_the_wide_fanout():
    import httpx

    from websearch.layer1_search.adapters.searxng import SearxngAdapter

    config = httpx.get(f"{LIVE_URL}/config", timeout=15).json()
    active = [e for e in config["engines"] if e.get("enabled")]
    generated = yaml.safe_load(SETTINGS.read_text())["engines"]
    active_names = {e["name"] for e in active}
    missing = [e["name"] for e in generated if e["name"] not in active_names]
    assert not missing, f"settings.yml enables engines the instance did not activate: {missing[:5]}"

    out = SearxngAdapter(LIVE_URL).search(SearchRequest(query="rust ownership", timeout_ms=20000))
    assert out.error is None, out.error
    assert len(out.results) >= 20, f"expected a wide fanout, got {len(out.results)}"
    assert all(r.url.startswith("http") for r in out.results)


@pytest.mark.skipif(not LIVE_URL, reason="set WEBSEARCH_SEARXNG_LIVE to a running instance")
def test_live_instance_reaches_engines_that_are_off_upstream():
    """bing, mojeek, yandex and friends only answer because the generated list enables
    them; on stock defaults this query would come back from a handful of engines."""
    import httpx

    payload = httpx.get(
        f"{LIVE_URL}/search",
        params={"q": "rust ownership", "format": "json"},
        timeout=30,
    ).json()
    answered = {e for r in payload["results"] for e in r.get("engines", [])}
    assert len(answered) >= 10, f"only {len(answered)} engines answered: {sorted(answered)}"
    off_upstream = {"bing", "mojeek", "yandex", "yahoo", "seznam", "dogpile", "wikipedia"}
    assert answered & off_upstream, (
        f"none of the newly enabled engines answered: {sorted(answered)}"
    )


@pytest.mark.skipif(not LIVE_URL, reason="set WEBSEARCH_SEARXNG_LIVE to a running instance")
def test_live_instance_rejects_a_write_to_its_config():
    """The read-only mount is the guarantee; prove the container cannot break it."""
    out = subprocess.run(
        ["docker", "exec", "websearch-searxng", "sh", "-c", "touch /etc/searxng/probe.tmp"],
        capture_output=True,
        text=True,
    )
    assert out.returncode != 0, "the config mount is writable from inside the container"
    assert not (STACK / "core-config" / "probe.tmp").exists()


@pytest.mark.skipif(not LIVE_URL, reason="set WEBSEARCH_SEARXNG_LIVE to a running instance")
def test_live_router_fuses_searxng_with_ddgs_shape():
    """The envelope the CLI emits must still match the search contract with SearXNG on."""
    from websearch.layer1_search import build_router

    router = build_router(searxng_url=LIVE_URL, enable_ddgs=False)
    envelope = router.search(SearchRequest(query="rust ownership", timeout_ms=20000))
    payload = json.loads(envelope.model_dump_json())
    assert payload["ok"] is True
    assert payload["meta"]["backend"] == "searxng"
    assert payload["data"]["results"]
