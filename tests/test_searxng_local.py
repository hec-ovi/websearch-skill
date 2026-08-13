"""The Docker-free local SearXNG runner: `websearch searxng`.

The unit tests cover what the runner decides on its own (where state goes, which port,
what settings it writes, how it wires the tool at itself, how it stops something) with a
stub server standing in for SearXNG, so they need no network and no clone. The real
clone-install-start round trip is at the bottom and runs only when
``WEBSEARCH_SEARXNG_LOCAL_LIVE=1`` asks for it, because it downloads a repository and
builds a virtualenv.

The detachment test is the one that matters most: an agent CLI kills the process group
of every shell command it runs, so a server that stays in that group dies with the call
that started it. That is exactly the failure this command exists to avoid.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from websearch import searxng_local as sx
from websearch.cli import main
from websearch.optional_layers import ENV_FILE_VAR, SEARXNG_ENV

ENV_VARS = (sx.HOME_VAR, sx.PORT_VAR, sx.REF_VAR, ENV_FILE_VAR, SEARXNG_ENV)

ENVELOPE_REF = "https://github.com/hec-ovi/websearch-skill/contracts/envelope.schema.json"
SEARXNG_PAYLOAD_REF = (
    "https://github.com/hec-ovi/websearch-skill/contracts/searxng.schema.json#/$defs/SearxngPayload"
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- where state goes ---------------------------------------------------------------


def test_an_explicit_home_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path / "elsewhere"))
    assert sx.home_dir() == (tmp_path / "elsewhere").resolve()


def test_state_lands_beside_the_configured_env_file(monkeypatch, tmp_path):
    """A container that mounts its config directory keeps the install across runs."""
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "conf" / "websearch.env"))
    assert sx.home_dir() == (tmp_path / "conf" / "searxng").resolve()


def test_with_no_env_file_the_state_goes_to_the_xdg_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert sx.home_dir() == (tmp_path / "cache" / "websearch" / "searxng").resolve()


# --- port ---------------------------------------------------------------------------


def test_the_port_is_overridable(monkeypatch):
    monkeypatch.setenv(sx.PORT_VAR, "9001")
    assert sx.port() == 9001
    assert sx.base_url() == "http://127.0.0.1:9001"


def test_an_unusable_port_is_refused_by_name(monkeypatch):
    monkeypatch.setenv(sx.PORT_VAR, "70000")
    with pytest.raises(sx.SearxngError) as exc:
        sx.port()
    assert sx.PORT_VAR in str(exc.value)


# --- settings -----------------------------------------------------------------------


def test_the_generated_settings_turn_on_json_and_bind_to_loopback(tmp_path):
    yaml = pytest.importorskip("yaml")
    paths = sx.Paths(tmp_path)
    written = sx.write_settings(paths, 9002)
    settings = yaml.safe_load(written.read_text())

    assert settings["use_default_settings"] is True
    assert settings["server"]["bind_address"] == "127.0.0.1"
    assert settings["server"]["port"] == 9002
    assert "json" in settings["search"]["formats"]
    assert settings["server"]["limiter"] is False


def test_every_instance_gets_its_own_secret(tmp_path):
    yaml = pytest.importorskip("yaml")
    one = yaml.safe_load(sx.write_settings(sx.Paths(tmp_path / "a"), 9003).read_text())
    two = yaml.safe_load(sx.write_settings(sx.Paths(tmp_path / "b"), 9003).read_text())
    assert one["server"]["secret_key"] != two["server"]["secret_key"]
    assert len(one["server"]["secret_key"]) >= 32


def test_settings_are_not_world_readable(tmp_path):
    written = sx.write_settings(sx.Paths(tmp_path), 9004)
    assert written.stat().st_mode & 0o077 == 0


# --- wiring the tool at the instance ------------------------------------------------


def test_up_records_the_url_in_the_configured_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / "websearch.env"
    env_file.write_text("WEBSEARCH_PROXY=off\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))

    written = sx.wire_env_file("http://127.0.0.1:8888")

    assert written == env_file
    lines = env_file.read_text().splitlines()
    assert "WEBSEARCH_PROXY=off" in lines
    assert f"{SEARXNG_ENV}=http://127.0.0.1:8888" in lines


def test_rewiring_replaces_the_old_url_instead_of_stacking(monkeypatch, tmp_path):
    env_file = tmp_path / "websearch.env"
    env_file.write_text(f"export {SEARXNG_ENV}=http://127.0.0.1:1111\n")
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))

    sx.wire_env_file("http://127.0.0.1:2222")

    body = env_file.read_text()
    assert body.count(SEARXNG_ENV) == 1
    assert "2222" in body and "1111" not in body


def test_with_no_env_file_configured_the_user_config_file_is_used(monkeypatch, tmp_path):
    """The cwd's .env belongs to whatever project the agent is working in.

    So the URL goes to the tool's own file instead, which is the one place a setting
    survives changing directories. XDG_CONFIG_HOME is redirected by the suite-wide
    fixture, so this never touches the developer's real config.
    """
    monkeypatch.chdir(tmp_path)
    from websearch.optional_layers import user_env_file

    written = sx.wire_env_file("http://127.0.0.1:8888")

    assert written == user_env_file()
    assert f"{SEARXNG_ENV}=http://127.0.0.1:8888" in written.read_text().splitlines()
    assert not (tmp_path / ".env").exists()


def test_a_project_env_file_that_already_sets_the_url_is_not_written_under(monkeypatch, tmp_path):
    """Writing under a file that outranks ours would say one thing and do another."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"{SEARXNG_ENV}=http://127.0.0.1:1234\n")

    assert sx.wire_env_file("http://127.0.0.1:8888") is None


# --- health, status, and stopping ---------------------------------------------------


def _stub_server(tmp_path: Path, port: int) -> subprocess.Popen:
    """A loopback HTTP server that answers /healthz and /config like SearXNG does."""
    script = tmp_path / "stub.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path.startswith("/healthz"):
                        body = b"OK"
                    elif self.path.startswith("/config"):
                        body = json.dumps({{
                            "version": "1.2.3",
                            "engines": [
                                {{"name": "a", "enabled": True}},
                                {{"name": "b", "enabled": False}},
                            ],
                        }}).encode()
                    else:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *a):
                    pass

            HTTPServer(("127.0.0.1", {port}), H).serve_forever()
            """
        )
    )
    child = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if sx.is_healthy(f"http://127.0.0.1:{port}"):
            return child
        if child.poll() is not None:
            raise AssertionError("the stub server exited before answering")
        time.sleep(0.05)
    child.kill()
    raise AssertionError("the stub server never came up")


@pytest.fixture
def stub(tmp_path, free_port):
    child = _stub_server(tmp_path, free_port)
    yield child
    try:
        os.killpg(os.getpgid(child.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@pytest.fixture
def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_health_is_false_when_nothing_listens(free_port):
    assert sx.is_healthy(f"http://127.0.0.1:{free_port}") is False


def test_a_live_instance_reports_its_version_and_engine_counts(stub, free_port):
    info = sx.instance_info(f"http://127.0.0.1:{free_port}")
    assert info == {"version": "1.2.3", "engines_active": 1, "engines_available": 2}


def test_the_local_hop_ignores_a_configured_proxy(monkeypatch, stub, free_port):
    """A loopback instance can never be reached through a remote exit node."""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    assert sx.is_healthy(f"http://127.0.0.1:{free_port}") is True


def test_status_describes_an_install_that_was_never_made(tmp_path, free_port):
    state = sx.status(sx.Paths(tmp_path), free_port)
    assert state["installed"] is False
    assert state["pid"] is None
    assert state["healthy"] is False
    assert state["url"] == f"http://127.0.0.1:{free_port}"


def test_status_reports_a_live_instance(tmp_path, stub, free_port):
    paths = sx.Paths(tmp_path)
    paths.pidfile.write_text(f"{stub.pid}\n")
    state = sx.status(paths, free_port)
    assert state["healthy"] is True
    assert state["pid"] == stub.pid
    assert state["version"] == "1.2.3"


def test_a_stale_pidfile_does_not_claim_a_running_server(tmp_path):
    paths = sx.Paths(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    paths.pidfile.write_text(f"{dead.pid}\n")
    assert sx.running_pid(paths) is None


def test_down_stops_the_process_and_clears_the_pidfile(tmp_path, stub, free_port):
    paths = sx.Paths(tmp_path)
    paths.pidfile.write_text(f"{stub.pid}\n")

    assert sx.stop(paths) is True

    assert not paths.pidfile.exists()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and sx.is_healthy(f"http://127.0.0.1:{free_port}"):
        time.sleep(0.1)
    assert sx.is_healthy(f"http://127.0.0.1:{free_port}") is False


# --- detachment: the reason this command exists -------------------------------------


def test_a_started_server_survives_a_kill_of_the_callers_process_group(tmp_path, free_port):
    """Agent CLIs group-kill each shell command. The child must be in its own session.

    This drives the real start(), with a stub granian, from inside a process group that
    is then killed the way a tool call kills one.
    """
    paths = sx.Paths(tmp_path)
    paths.venv.joinpath("bin").mkdir(parents=True)
    paths.src.mkdir(parents=True)
    paths.granian.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            from http.server import BaseHTTPRequestHandler, HTTPServer

            class H(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"OK")

                def log_message(self, *a):
                    pass

            HTTPServer(("127.0.0.1", {free_port}), H).serve_forever()
            """
        )
    )
    paths.granian.chmod(0o755)
    sx.write_settings(paths, free_port)

    # A stand-in for the agent's shell: its own process group, which then gets the
    # SIGKILL that a finished tool call sends to the whole group.
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        textwrap.dedent(
            f"""
            from websearch import searxng_local as sx
            print(sx.start(sx.Paths({str(tmp_path)!r}), {free_port}), flush=True)
            """
        )
    )
    shell = subprocess.Popen(
        [sys.executable, str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(Path(sx.__file__).resolve().parents[2])},
    )
    out, err = shell.communicate(timeout=30)
    assert shell.returncode == 0, err.decode()
    server_pid = int(out.decode().strip())

    assert sx.wait_healthy(f"http://127.0.0.1:{free_port}", timeout_s=15), "never came up"
    # start_new_session made the launcher its own group leader, so its pid is the pgid
    # the tool call would SIGKILL once the command returned.
    try:
        os.killpg(shell.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # the group is already empty, which is the point
    time.sleep(0.5)

    try:
        assert sx.is_healthy(f"http://127.0.0.1:{free_port}"), "the group kill took the server"
        assert os.getpgid(server_pid) != shell.pid
    finally:
        try:
            os.killpg(os.getpgid(server_pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_the_servers_environment_drops_the_clients_proxy(tmp_path, free_port, monkeypatch):
    """SearXNG's own engine egress is configured on the stack, not inherited by accident."""
    monkeypatch.setenv("WEBSEARCH_PROXY", "socks5h://example.invalid:1080")
    monkeypatch.setenv("WEBSEARCH_VPN", "nordvpn")
    paths = sx.Paths(tmp_path)
    paths.venv.joinpath("bin").mkdir(parents=True)
    paths.src.mkdir(parents=True)
    dump = tmp_path / "env.json"
    paths.granian.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os
            json.dump(dict(os.environ), open({str(dump)!r}, "w"))
            """
        )
    )
    paths.granian.chmod(0o755)
    sx.write_settings(paths, free_port)

    sx.start(paths, free_port)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not dump.exists():
        time.sleep(0.05)

    import json

    child_env = json.loads(dump.read_text())
    assert "WEBSEARCH_PROXY" not in child_env
    assert "WEBSEARCH_VPN" not in child_env
    assert child_env["GRANIAN_HOST"] == "127.0.0.1"
    assert child_env["GRANIAN_PORT"] == str(free_port)
    assert child_env["SEARXNG_SETTINGS_PATH"] == str(paths.settings)


# --- the CLI face -------------------------------------------------------------------


def test_url_prints_the_base_url(capsys, monkeypatch):
    monkeypatch.setenv(sx.PORT_VAR, "9009")
    assert main(["searxng", "url"]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:9009"


def test_status_exits_nonzero_when_it_is_not_answering(capsys, monkeypatch, tmp_path, free_port):
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    assert main(["searxng", "status"]) == 1
    out = capsys.readouterr().out
    assert "not answering" in out
    assert "websearch searxng up" in out


def test_status_json_is_an_envelope(capsys, monkeypatch, tmp_path, free_port, assert_valid):
    import json

    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    main(["searxng", "status", "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert_valid(envelope, ENVELOPE_REF)
    assert envelope["ok"] is True
    assert envelope["meta"]["layer"] == "searxng"
    assert envelope["data"]["url"] == f"http://127.0.0.1:{free_port}"
    assert envelope["data"]["installed"] is False


# --- the contract -------------------------------------------------------------------


def test_the_payload_of_every_action_validates(monkeypatch, tmp_path, free_port, assert_valid):
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    for action in ("status", "down"):
        envelope = sx.control(sx.SearxngRequest(action=action))
        assert envelope.ok, envelope.error
        assert_valid(envelope.model_dump(mode="json"), ENVELOPE_REF)
        assert_valid(envelope.data, SEARXNG_PAYLOAD_REF)


def test_a_live_instance_validates_too(monkeypatch, tmp_path, stub, free_port, assert_valid):
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    monkeypatch.setenv(ENV_FILE_VAR, str(tmp_path / "websearch.env"))

    envelope = sx.control(sx.SearxngRequest(action="up"))

    assert envelope.ok, envelope.error
    assert_valid(envelope.data, SEARXNG_PAYLOAD_REF)
    assert envelope.data["healthy"] is True
    assert envelope.data["version"] == "1.2.3"
    assert envelope.data["wired"] == str(tmp_path / "websearch.env")


@pytest.mark.parametrize("bad", [{"action": "restart"}, {"action": "up", "nope": 1}])
def test_an_off_contract_request_is_refused(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        sx.SearxngRequest(**bad)


def test_down_with_nothing_running_says_so_and_succeeds(capsys, monkeypatch, tmp_path, free_port):
    # A pinned free port, not the default: on a machine that really runs an instance on
    # 8888, the default would make this test read that one and report it as stopped.
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    assert main(["searxng", "down"]) == 0
    assert "nothing was running" in capsys.readouterr().out


def test_up_against_an_already_running_instance_only_wires_it(
    capsys, monkeypatch, tmp_path, stub, free_port
):
    env_file = tmp_path / "websearch.env"
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path / "state"))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))

    assert main(["searxng", "up"]) == 0

    out = capsys.readouterr().out
    assert "already running" in out
    assert f"{SEARXNG_ENV}=http://127.0.0.1:{free_port}" in env_file.read_text()
    assert not (tmp_path / "state" / "src").exists(), "it must not install over a live one"


def test_a_bad_port_is_reported_before_anything_is_touched(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path))
    monkeypatch.setenv(sx.PORT_VAR, "potato")
    assert main(["searxng", "up"]) == 2
    assert sx.PORT_VAR in capsys.readouterr().err
    assert not (tmp_path / "src").exists()


# --- the real thing (opt-in) --------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("WEBSEARCH_SEARXNG_LOCAL_LIVE") != "1",
    reason="set WEBSEARCH_SEARXNG_LOCAL_LIVE=1 to clone and install upstream SearXNG",
)
def test_up_installs_and_serves_json_results(tmp_path, monkeypatch, free_port):
    env_file = tmp_path / "websearch.env"
    monkeypatch.setenv(ENV_FILE_VAR, str(env_file))
    monkeypatch.setenv(sx.HOME_VAR, str(tmp_path / "state"))
    monkeypatch.setenv(sx.PORT_VAR, str(free_port))
    paths = sx.Paths(sx.home_dir())

    try:
        assert main(["searxng", "up"]) == 0
        url = f"http://127.0.0.1:{free_port}"
        assert sx.is_healthy(url)
        assert sx.instance_info(url)["engines_active"] > 50
        assert f"{SEARXNG_ENV}={url}" in env_file.read_text()

        import json
        import urllib.parse

        query = urllib.parse.urlencode({"q": "rust ownership", "format": "json"})
        payload = json.loads(sx._get(f"{url}/search?{query}", 60))
        assert payload["results"], "a live instance returned nothing"
    finally:
        sx.stop(paths)


# --- settings that follow the tor layer ---------------------------------------------


def test_the_generated_settings_carry_no_tor_block_with_the_layer_off(tmp_path):
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888)

    body = paths.settings.read_text()
    assert "using_tor_proxy" not in body
    assert sx.MANAGED_MARKER in body
    assert not sx.settings_stale(paths, 8888, None, None)


def test_turning_tor_on_rewrites_the_settings_and_points_the_onion_engines_at_it(tmp_path):
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888)
    assert sx.settings_stale(paths, 8888, "socks5h://127.0.0.1:9050", None) is True

    sx.write_settings(paths, 8888, tor_socks="socks5h://127.0.0.1:9050")

    body = paths.settings.read_text()
    assert "- name: ahmia" in body and "- name: torch" in body
    assert body.count("socks5h://127.0.0.1:9050") == 2
    # Per engine, never globally: the clearnet engines must not be pushed through Tor.
    assert "using_tor_proxy: false" in body


def test_a_rewrite_keeps_the_secret_key(tmp_path):
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888)
    secret = sx._existing_secret(paths.settings.read_text())

    sx.write_settings(paths, 8888, tor_socks="socks5h://127.0.0.1:9050")

    assert sx._existing_secret(paths.settings.read_text()) == secret


def test_an_operator_file_is_never_rewritten(tmp_path):
    """Removing the marker is how you take the file over."""
    paths = sx.Paths(tmp_path)
    paths.settings.write_text("use_default_settings: true\nserver:\n  port: 9999\n")

    sx.write_settings(paths, 8888, tor_socks="socks5h://127.0.0.1:9050")

    assert "9999" in paths.settings.read_text()
    assert "ahmia" not in paths.settings.read_text()
    assert sx.settings_stale(paths, 8888, "socks5h://127.0.0.1:9050", None) is False


def test_a_settings_file_from_before_the_marker_is_adopted_if_unedited(tmp_path):
    """Otherwise every install that predates the tor layer keeps a file that can never
    gain the onion engines, and `websearch tor up` silently does nothing for SearXNG."""
    paths = sx.Paths(tmp_path)
    paths.settings.write_text(sx.LEGACY_SETTINGS_TEMPLATE.format(port=8888, secret="abc123"))

    assert sx.settings_stale(paths, 8888, "socks5h://127.0.0.1:9050", None) is True
    sx.write_settings(paths, 8888, tor_socks="socks5h://127.0.0.1:9050")

    body = paths.settings.read_text()
    assert sx.MANAGED_MARKER in body and "- name: ahmia" in body
    assert 'secret_key: "abc123"' in body


def test_an_edited_file_from_before_the_marker_is_left_alone(tmp_path):
    paths = sx.Paths(tmp_path)
    edited = (
        sx.LEGACY_SETTINGS_TEMPLATE.format(port=8888, secret="abc123") + "  autocomplete: ddg\n"
    )
    paths.settings.write_text(edited)

    sx.write_settings(paths, 8888, tor_socks="socks5h://127.0.0.1:9050")

    assert paths.settings.read_text() == edited


# --- where the instance's own engine requests leave from ------------------------------

PROXY_URL = "socks5h://svc:pw@dallas.us.socks.nordhold.net:1080"


def test_the_settings_send_the_instances_own_requests_through_the_proxy(tmp_path, monkeypatch):
    """The failure this exists to catch: the CLI proxied and the instance not, so every
    search still left from this machine's address while the fetches that followed did not.
    That is also what gets a home IP CAPTCHAd by the engines it queries."""
    import yaml

    monkeypatch.setenv("WEBSEARCH_PROXY", PROXY_URL)
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888, proxy=sx.outgoing_proxy())

    settings = yaml.safe_load(paths.settings.read_text())
    assert settings["outgoing"]["proxies"] == {"all://": [PROXY_URL]}
    assert settings["outgoing"]["extra_proxy_timeout"] == sx.EXTRA_PROXY_TIMEOUT_S
    assert sx.settings_egress(paths) == PROXY_URL


def test_an_unproxied_install_stays_direct(tmp_path):
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888, proxy=sx.outgoing_proxy())

    assert "proxies" not in paths.settings.read_text()
    assert sx.settings_egress(paths) is None


def test_changing_the_exit_makes_the_running_instance_stale(tmp_path, monkeypatch):
    """Settings are read once at startup, so 'stale' is what turns a new city into a
    restart instead of an instance quietly searching from the old one."""
    monkeypatch.setenv("WEBSEARCH_PROXY", PROXY_URL)
    paths = sx.Paths(tmp_path)
    sx.write_settings(paths, 8888, proxy=PROXY_URL)

    moved = PROXY_URL.replace("dallas", "stockholm")
    assert sx.settings_stale(paths, 8888, None, moved) is True
    assert sx.settings_stale(paths, 8888, None, PROXY_URL) is False


def test_the_override_forces_direct_egress(monkeypatch):
    monkeypatch.setenv("WEBSEARCH_PROXY", PROXY_URL)
    monkeypatch.setenv(sx.OUTGOING_PROXY_VAR, "off")
    assert sx.outgoing_proxy() is None


def test_a_locked_bring_up_will_not_configure_an_instance_that_leaks(monkeypatch):
    from websearch.proxy import EGRESS_LOCK_VAR, EgressLocked

    monkeypatch.setenv(EGRESS_LOCK_VAR, "on")
    with pytest.raises(EgressLocked):
        sx.outgoing_proxy()
