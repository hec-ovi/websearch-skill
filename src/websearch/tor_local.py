"""Run Tor on this machine, and route the tool through it.

The tor layer is off until you turn it on, and turning it on means one thing: every
network path this tool opens connects through a Tor SOCKS port, which is also what makes
``.onion`` resolvable and the onion search engines selectable. This module is the process
control behind that: find a Tor, configure it, start it detached, and say whether it is
really carrying the traffic.

Where the Tor comes from, in order:

1. ``WEBSEARCH_TOR_BINARY``, when you point it at one.
2. A Tor already answering on the SOCKS port (a system service, Tor Browser, a container
   sidecar). Never install over something that is already running.
3. ``tor`` on PATH.
4. The official Tor Expert Bundle, downloaded into the state directory and checked
   against the ``sha256sums-signed-build.txt`` published beside it. The binary is not
   GPG-verified: that would need a gpg toolchain and the Tor Browser signing key, so what
   this trusts is TLS to torproject.org plus that published digest.

Detached is the load-bearing word, same as the SearXNG stack: an agent CLI kills the
process group of every shell command it runs, so the child gets its own session and
writes to a log file rather than the inherited pipe.

An egress proxy configured alongside Tor is not overridden. It is written into torrc as
Tor's own upstream (``Socks5Proxy`` / ``HTTPSProxy``), so the path becomes
you -> proxy -> Tor -> destination: the VPN hop still hides Tor use from the ISP, and no
hop is dropped by turning on the layer that was supposed to add one.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from . import errors
from .envelope import Envelope, error_envelope, ok_envelope
from .optional_layers import TOR_ENV, redact_url, scrub_proxy, write_env_setting
from .proxy import ProxyConfigError, resolve_proxy, tor_enabled, tor_socks_url

TOR_CONTRACT_VERSION = "1.0.0"

HOME_VAR = "WEBSEARCH_TOR_HOME"
BINARY_VAR = "WEBSEARCH_TOR_BINARY"
VERSION_VAR = "WEBSEARCH_TOR_VERSION"

# The Tor Browser release the expert bundle is cut from. Pinned so an install is
# reproducible and an upstream outage cannot leave the version unknown; the live release
# index is consulted first and this is the fallback.
DEFAULT_VERSION = "15.0.19"
VERSION_INDEX = "https://aus1.torproject.org/torbrowser/update_3/release/downloads.json"
DIST_BASE = "https://dist.torproject.org/torbrowser"
SUMS_FILE = "sha256sums-signed-build.txt"

# Tor's own check service: the one endpoint that can answer "is this traffic actually
# leaving through Tor" rather than "did something accept my connection". SearXNG asks the
# same question of the same URL before it will use a tor-routed engine.
CHECK_URL = "https://check.torproject.org/api/ip"

BOOTSTRAP_TIMEOUT_S = 120
BOOTSTRAP_DONE = "Bootstrapped 100%"


class TorError(RuntimeError):
    """A step of the local Tor stack failed with something the caller can act on."""


# --- where the bundle for this machine lives -------------------------------------------

# The expert bundle is published per platform, and not for every one: there is no
# glibc aarch64 Linux build. An unsupported host is told to install tor itself rather
# than handed a binary that cannot run.
_BUNDLE_PLATFORMS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "amd64"): "linux-x86_64",
    ("linux", "i686"): "linux-i686",
    ("linux", "i386"): "linux-i686",
    ("darwin", "x86_64"): "macos-x86_64",
    ("darwin", "arm64"): "macos-aarch64",
    ("darwin", "aarch64"): "macos-aarch64",
    ("windows", "x86_64"): "windows-x86_64",
    ("windows", "amd64"): "windows-x86_64",
    ("windows", "i686"): "windows-i686",
    ("windows", "i386"): "windows-i686",
}


def bundle_platform() -> str:
    """The expert-bundle platform tag for this machine."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    tag = _BUNDLE_PLATFORMS.get((system, machine))
    if tag is None:
        raise TorError(
            f"the Tor Expert Bundle has no build for {system}/{machine}; install tor from "
            f"your package manager and re-run, or point {BINARY_VAR} at a tor binary"
        )
    return tag


def bundle_version() -> str:
    """The release to install: the pin when set, else the current one, else the default."""
    pinned = os.environ.get(VERSION_VAR, "").strip()
    if pinned:
        return pinned
    try:
        index = json.loads(_get(VERSION_INDEX, 15.0))
        version = index.get("version")
    except (urllib.error.URLError, OSError, ValueError):
        return DEFAULT_VERSION
    return str(version) if version else DEFAULT_VERSION


def bundle_urls(version: str) -> tuple[str, str, str]:
    """(archive url, checksums url, file name) for this machine and release."""
    name = f"tor-expert-bundle-{bundle_platform()}-{version}.tar.gz"
    return f"{DIST_BASE}/{version}/{name}", f"{DIST_BASE}/{version}/{SUMS_FILE}", name


# --- state on disk ----------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    home: Path

    def __post_init__(self) -> None:
        if not isinstance(self.home, Path):
            object.__setattr__(self, "home", Path(self.home))

    @property
    def dist(self) -> Path:
        """The unpacked expert bundle: ``tor/`` (binary and its libraries) and ``data/``."""
        return self.home / "dist"

    @property
    def binary(self) -> Path:
        suffix = ".exe" if platform.system().lower() == "windows" else ""
        return self.dist / "tor" / f"tor{suffix}"

    @property
    def lib_dir(self) -> Path:
        # The bundle ships its own OpenSSL and libevent beside the binary; without this on
        # the loader path the binary starts on no machine but the one that built it.
        return self.dist / "tor"

    @property
    def geoip(self) -> Path:
        return self.dist / "data" / "geoip"

    @property
    def geoip6(self) -> Path:
        return self.dist / "data" / "geoip6"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def torrc(self) -> Path:
        return self.home / "torrc"

    @property
    def log(self) -> Path:
        return self.home / "tor.log"

    @property
    def pidfile(self) -> Path:
        return self.home / "tor.pid"


def home_dir() -> Path:
    """Where the bundle, the Tor data directory, and the runtime state live."""
    explicit = os.environ.get(HOME_VAR)
    if explicit:
        return Path(explicit).expanduser().resolve()
    from .state import state_dir

    return (state_dir() / "tor").resolve()


# --- reachability and verification ------------------------------------------------------


def socks_endpoint(url: str | None = None) -> tuple[str, int]:
    parts = urlsplit(url or tor_socks_url())
    return parts.hostname or "127.0.0.1", parts.port or 9050


def is_reachable(url: str | None = None, timeout_s: float = 2.0) -> bool:
    """Whether something accepts connections on the SOCKS port. Not proof it is Tor."""
    host, port = socks_endpoint(url)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def verify(url: str | None = None, timeout_s: float = 20.0) -> dict[str, Any]:
    """Ask Tor's own check service whether this SOCKS port really carries Tor traffic.

    ``is_tor`` is None when the question could not be asked at all (no network, the check
    service down), which is not the same answer as False: False means something answered
    and it was not Tor, and that is the one case worth failing on.
    """
    socks = url or tor_socks_url()
    try:
        import httpx

        with httpx.Client(proxy=socks, timeout=timeout_s, follow_redirects=True) as client:
            body = client.get(CHECK_URL).json()
    except Exception as exc:
        return {"is_tor": None, "exit_ip": None, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(body, dict):
        return {"is_tor": None, "exit_ip": None, "error": "the check service returned no object"}
    return {"is_tor": bool(body.get("IsTor")), "exit_ip": body.get("IP"), "error": None}


# --- installing the bundle --------------------------------------------------------------


def _get(url: str, timeout_s: float) -> bytes:
    # urllib on purpose, and with the proxy handlers emptied: this is the one path that
    # must not go through the tool's own egress setting, since it runs to set that egress
    # up. A configured proxy still applies to Tor itself, through torrc.
    request = urllib.request.Request(url, headers={"User-Agent": "websearch-tor"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout_s) as response:
        return response.read()


def _expected_digest(sums: str, name: str) -> str:
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            return parts[0].lower()
    raise TorError(f"{SUMS_FILE} has no entry for {name}; refusing to run an unchecked binary")


def download_bundle(paths: Paths, *, version: str | None = None) -> Path:
    """Fetch, verify, and unpack the expert bundle. Returns the tor binary."""
    version = version or bundle_version()
    archive_url, sums_url, name = bundle_urls(version)
    paths.home.mkdir(parents=True, exist_ok=True)
    try:
        sums = _get(sums_url, 30.0).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        raise TorError(f"could not read {sums_url}: {exc}") from exc
    expected = _expected_digest(sums, name)
    try:
        blob = _get(archive_url, 300.0)
    except (urllib.error.URLError, OSError) as exc:
        raise TorError(f"could not download {archive_url}: {exc}") from exc
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise TorError(
            f"{name} does not match its published sha256 ({actual} vs {expected}); "
            "the download was corrupted or tampered with, and was not installed"
        )
    archive = paths.home / name
    archive.write_bytes(blob)
    if paths.dist.exists():
        shutil.rmtree(paths.dist)
    paths.dist.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, paths.dist)
    finally:
        archive.unlink(missing_ok=True)
    if not paths.binary.exists():
        raise TorError(f"{name} unpacked without a tor binary at {paths.binary}")
    paths.binary.chmod(0o700)
    (paths.home / "VERSION").write_text(f"{version}\n", encoding="utf-8")
    return paths.binary


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract without letting a member escape ``dest`` (path traversal, links out)."""
    root = dest.resolve()
    for member in tar.getmembers():
        target = (root / member.name).resolve()
        if not str(target).startswith(str(root)):
            raise TorError(f"the archive tried to write outside {dest}: {member.name}")
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname).resolve()
            if not str(link).startswith(str(root)):
                raise TorError(f"the archive linked outside {dest}: {member.name}")
    # filter="data" (3.12+) drops device nodes and unsafe modes; older runtimes get the
    # member walk above, which is the part that matters here.
    try:
        tar.extractall(dest, filter="data")  # noqa: S202 - members validated above
    except TypeError:
        tar.extractall(dest)  # noqa: S202 - members validated above


def installed_version(paths: Paths) -> str | None:
    try:
        return (paths.home / "VERSION").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def find_binary(paths: Paths) -> tuple[Path | None, str]:
    """The tor to run and where it came from: explicit, bundle, or the system."""
    explicit = os.environ.get(BINARY_VAR, "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise TorError(f"{BINARY_VAR}={explicit} does not exist")
        return path, "explicit"
    if paths.binary.exists():
        return paths.binary, "bundle"
    system = shutil.which("tor")
    if system:
        return Path(system), "system"
    return None, "none"


# --- configuration ----------------------------------------------------------------------


def torrc_upstream(proxy_url: str | None) -> list[str]:
    """The torrc lines that make Tor dial out through an egress proxy.

    Tor speaks to one upstream of its own, which is what keeps "proxy" and "tor" from
    being a choice: the proxy stays in front of the entry guard instead of being replaced
    by it. Only the forms Tor implements are accepted; anything else is refused loudly
    rather than quietly dropped, because a silently ignored VPN hop is the failure mode
    this whole path exists to avoid.
    """
    if not proxy_url:
        return []
    parts = urlsplit(proxy_url)
    scheme = (parts.scheme or "").lower()
    host, port = parts.hostname, parts.port
    if not host:
        raise TorError(f"the configured proxy {redact_url(proxy_url)} has no host")
    user = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    if scheme in ("socks5", "socks5h"):
        lines = [f"Socks5Proxy {host}:{port or 1080}"]
        if user:
            lines.append(f"Socks5ProxyUsername {user}")
            lines.append(f"Socks5ProxyPassword {password or ''}")
        return lines
    if scheme in ("http", "https"):
        lines = [f"HTTPSProxy {host}:{port or (443 if scheme == 'https' else 80)}"]
        if user:
            lines.append(f"HTTPSProxyAuthenticator {user}:{password or ''}")
        return lines
    if scheme in ("socks4", "socks4a"):
        if user:
            raise TorError("Tor's Socks4Proxy takes no credentials; use a socks5 proxy")
        return [f"Socks4Proxy {host}:{port or 1080}"]
    raise TorError(
        f"Tor cannot dial out through a '{scheme}' proxy; it supports http(s), socks4, "
        "and socks5. Change WEBSEARCH_PROXY or turn it off to use Tor on its own."
    )


def write_torrc(paths: Paths, *, socks_url: str | None = None, proxy_url: str | None) -> Path:
    """Write the torrc for this install. Always rewritten: it is generated, not yours.

    An operator file would be ``%include``d from here, which is why this one is safe to
    regenerate: the upstream proxy and the SOCKS port both come from settings that change
    between runs, and a stale torrc would route traffic the settings say it should not.
    """
    host, port = socks_endpoint(socks_url)
    lines = [
        "# Generated by `websearch tor up` on every run. Edit torrc.local instead:",
        "# anything in that file is included below and survives a regeneration.",
        f"SocksPort {host}:{port}",
        f"DataDirectory {paths.data}",
        f"Log notice file {paths.log}",
        "ClientOnly 1",
        "AvoidDiskWrites 1",
        # Tor's own default, restated because it is what keeps the log free of the
        # hostnames this tool visits.
        "SafeLogging 1",
    ]
    if paths.geoip.exists():
        lines.append(f"GeoIPFile {paths.geoip}")
    if paths.geoip6.exists():
        lines.append(f"GeoIPv6File {paths.geoip6}")
    lines.extend(torrc_upstream(proxy_url))
    local = paths.home / "torrc.local"
    if local.exists():
        lines.append(f"%include {local}")
    paths.home.mkdir(parents=True, exist_ok=True)
    paths.data.mkdir(parents=True, exist_ok=True)
    # Tor refuses to start if its DataDirectory is group- or world-readable.
    paths.data.chmod(0o700)
    paths.torrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths.torrc.chmod(0o600)  # it carries the upstream proxy's credentials
    return paths.torrc


# --- the process ------------------------------------------------------------------------


def running_pid(paths: Paths) -> int | None:
    """The pid from the pidfile when that process is still alive, else None."""
    try:
        pid = int(paths.pidfile.read_text().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


def start(paths: Paths, binary: Path, *, source: str) -> int:
    """Spawn tor in its own session, detached from the caller's process group."""
    env = dict(os.environ)
    if source == "bundle":
        existing = env.get("LD_LIBRARY_PATH")
        env["LD_LIBRARY_PATH"] = f"{paths.lib_dir}:{existing}" if existing else str(paths.lib_dir)
    # The tool's proxy setting belongs to Tor's torrc, not to its process environment;
    # a lingering ALL_PROXY here would be a second, unmanaged hop.
    for leaked in ("WEBSEARCH_PROXY", "WEBSEARCH_TOR", "ALL_PROXY", "all_proxy"):
        env.pop(leaked, None)

    paths.log.parent.mkdir(parents=True, exist_ok=True)
    # Tor appends to the log itself (Log notice file). Truncating here keeps the
    # bootstrap watcher from matching a previous run's success line.
    paths.log.write_text("", encoding="utf-8")
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [str(binary), "-f", str(paths.torrc)],
        cwd=str(paths.home),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    paths.pidfile.write_text(f"{child.pid}\n", encoding="utf-8")
    return child.pid


_LOG_ERROR = re.compile(r"\[(err|warn)\] (.+)")


def wait_bootstrapped(paths: Paths, timeout_s: int = BOOTSTRAP_TIMEOUT_S) -> tuple[bool, str]:
    """Watch the log until Tor reports a finished bootstrap. (ok, last interesting line)."""
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        try:
            text = paths.log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            if BOOTSTRAP_DONE in line:
                return True, line.strip()
            match = _LOG_ERROR.search(line)
            if match:
                last = match.group(2).strip()
                if match.group(1) == "err":
                    return False, last
        if running_pid(paths) is None and paths.pidfile.exists():
            return False, last or "the tor process exited during bootstrap"
        time.sleep(0.5)
    return False, last or f"no bootstrap within {timeout_s}s"


def stop(paths: Paths, timeout_s: float = 10.0) -> bool:
    """Signal the whole session and wait for it to go. False when nothing was running."""
    pid = running_pid(paths)
    if pid is None:
        paths.pidfile.unlink(missing_ok=True)
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError):
            break
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if running_pid(paths) is None:
                paths.pidfile.unlink(missing_ok=True)
                return True
            time.sleep(0.2)
    paths.pidfile.unlink(missing_ok=True)
    return True


def status(paths: Paths, *, socks_url: str, check: bool) -> dict[str, Any]:
    binary, source = find_binary(paths)
    reachable = is_reachable(socks_url)
    state: dict[str, Any] = {
        "home": str(paths.home),
        "socks_url": socks_url,
        "installed": bool(binary),
        "binary": str(binary) if binary else None,
        "source": source if binary else None,
        "version": installed_version(paths),
        "pid": running_pid(paths),
        "reachable": reachable,
        "is_tor": None,
        "exit_ip": None,
        "log": str(paths.log) if paths.log.exists() else None,
    }
    if reachable and check:
        state.update({k: v for k, v in verify(socks_url).items() if k != "error"})
    return state


# --- the contract face --------------------------------------------------------------


class TorRequest(BaseModel):
    """Mirrors contracts/tor.schema.json#/$defs/TorRequest."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["up", "status", "down"]
    reinstall: bool = False
    verify: bool = True
    timeout_s: int = Field(default=BOOTSTRAP_TIMEOUT_S, ge=5, le=600)


class TorPayload(BaseModel):
    """Mirrors contracts/tor.schema.json#/$defs/TorPayload."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["up", "status", "down"]
    enabled: bool
    home: str
    socks_url: str
    installed: bool
    binary: str | None = None
    source: str | None = None
    version: str | None = None
    pid: int | None = None
    reachable: bool
    is_tor: bool | None = None
    exit_ip: str | None = None
    upstream: str | None = None
    wired: str | None = None
    bootstrap: str | None = None
    log: str | None = None


def _layer_is_on() -> bool:
    """Whether the switch is on right now, treating a malformed value as off.

    The lifecycle payload reports state; a typo in the variable is the layer-state code's
    error to raise, not a reason for `tor status` to fail instead of answering.
    """
    try:
        return tor_enabled()
    except ProxyConfigError:
        return False


def _payload(action: str, paths: Paths, state: dict[str, Any], **extra: Any) -> TorPayload:
    return TorPayload(
        action=action,  # type: ignore[arg-type]
        enabled=_layer_is_on(),
        home=state["home"],
        socks_url=state["socks_url"],
        installed=state["installed"],
        binary=state["binary"],
        source=state["source"],
        version=state["version"],
        pid=state["pid"],
        reachable=state["reachable"],
        is_tor=state["is_tor"],
        exit_ip=state["exit_ip"],
        log=state["log"],
        **extra,
    )


def _error(code: str, message: str, *, retriable: bool) -> Envelope:
    return error_envelope(
        TOR_CONTRACT_VERSION,
        code=code,
        message=message,
        retriable=retriable,
        layer="tor",
        backend="tor-local",
    )


def _ok(payload: TorPayload, started: float) -> Envelope:
    return ok_envelope(
        TOR_CONTRACT_VERSION,
        payload.model_dump(mode="json"),
        layer="tor",
        backend="tor-local",
        elapsed_ms=round((time.monotonic() - started) * 1000, 3),
    )


def control(request: TorRequest) -> Envelope:
    """Run one lifecycle action and describe the resulting state.

    The single entry point behind the ``tor`` CLI command and the ``init`` step, so the
    two faces cannot drift. Nothing here raises for an unusable configuration: it comes
    back as an Envelope error the caller can read.
    """
    started = time.monotonic()
    try:
        socks_url = tor_socks_url()
        upstream = resolve_proxy()
    except ProxyConfigError as exc:
        return _error(errors.INVALID_REQUEST, str(exc), retriable=False)
    paths = Paths(home_dir())

    if request.action == "status":
        state = status(paths, socks_url=socks_url, check=request.verify)
        return _ok(
            _payload("status", paths, state, upstream=redact_url(upstream)),
            started,
        )

    if request.action == "down":
        stopped = stop(paths)
        # Only claim the layer is off once nothing is listening: an externally managed Tor
        # (system service, Tor Browser) is not ours to stop, and saying "down" while it
        # still answers would be a lie the next command notices.
        wired = None
        if stopped and not is_reachable(socks_url):
            wired = write_env_setting(TOR_ENV, "off")
            os.environ[TOR_ENV] = "off"
        state = status(paths, socks_url=socks_url, check=False)
        return _ok(
            _payload(
                "down",
                paths,
                state,
                upstream=redact_url(upstream),
                wired=str(wired) if wired else None,
            ),
            started,
        )

    # --- up ---------------------------------------------------------------------------
    already = is_reachable(socks_url)
    if not already:
        try:
            binary, source = find_binary(paths)
            if request.reinstall or binary is None:
                binary = download_bundle(paths)
                source = "bundle"
            write_torrc(paths, socks_url=socks_url, proxy_url=upstream)
            start(paths, binary, source=source)
        except TorError as exc:
            return _error(
                errors.DEPENDENCY_MISSING,
                scrub_proxy(str(exc), upstream),
                retriable=False,
            )
        ok, detail = wait_bootstrapped(paths, request.timeout_s)
        if not ok:
            stop(paths)
            return _error(
                errors.UPSTREAM_ERROR,
                scrub_proxy(
                    f"Tor did not finish bootstrapping within {request.timeout_s}s: "
                    f"{detail}. Its log is at {paths.log}",
                    upstream,
                ),
                retriable=True,
            )
    else:
        detail = "already listening"

    state = status(paths, socks_url=socks_url, check=request.verify)
    if state["is_tor"] is False:
        return _error(
            errors.UPSTREAM_ERROR,
            f"{socks_url} answers but {CHECK_URL} says the traffic is not leaving through "
            "Tor. Something else is bound to that port; stop it or move Tor with "
            "WEBSEARCH_TOR_PORT.",
            retriable=False,
        )
    # Turn the layer on for this process and for the next command, the same way the
    # SearXNG bring-up records its URL: a variable exported into this process dies here.
    os.environ[TOR_ENV] = "on"
    wired = write_env_setting(TOR_ENV, "on")
    return _ok(
        _payload(
            "up",
            paths,
            state,
            upstream=redact_url(upstream),
            wired=str(wired) if wired else None,
            bootstrap=detail,
        ),
        started,
    )
