"""One call that brings the tool online and reports what works.

An agent handed a search tool it has never used will otherwise find out by hand: a dozen
shell probes, a wrong conclusion from a half-configured environment, and a lot of tokens.
This module is the supported alternative. It runs the same three steps every time, in the
same order:

1. ``env``     read the configured env file, so a URL another process wrote lands here.
2. ``tor``     bring Tor up, but only when the tor layer is on. It is off by default, so
               this step normally reports "off" and costs nothing.
3. ``searxng`` start the local SearXNG (unless skipped) and point THIS process at it.
4. ``doctor``  the full sweep, so the answer is measured rather than assumed.

Then it answers the only question the caller actually has, in one field: ``ready``. What
works and what does not is in ``capabilities``, and anything worth doing about it is in
``next_actions``, already deduplicated.

Nothing here fails the run. A SearXNG that will not start is a warn: the keyless engines
still search, and saying so is more useful than an error the caller has to interpret.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .envelope import Envelope, ok_envelope
from .optional_layers import (
    ENV_FILE_VAR,
    SEARXNG_ENV,
    TOR_ENV,
    env_file_candidates,
    load_env_file,
    user_env_file,
)

INIT_CONTRACT_VERSION = "1.1.0"

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

# Capability vocabulary, wider than a check status: an optional layer nobody turned on is
# "off" (the default, not a problem), and a check that did not run is "unknown" (quick
# mode), which is not the same as "down".
CAP_OK = "ok"
CAP_DEGRADED = "degraded"
CAP_DOWN = "down"
CAP_OFF = "off"
CAP_UNKNOWN = "unknown"

StepName = Literal["env", "tor", "searxng", "doctor"]
Status = Literal["ok", "warn", "fail", "skipped"]
Capability = Literal["ok", "degraded", "down", "off", "unknown"]
State = Literal["ready", "degraded", "broken"]


# --- the contract face ----------------------------------------------------------------


class InitRequest(BaseModel):
    """Mirrors contracts/init.schema.json#/$defs/InitRequest."""

    model_config = ConfigDict(extra="forbid")

    searxng: Literal["up", "skip"] = "up"
    # "auto" starts Tor only when the tor layer is on, which it is not unless you turned
    # it on. There is no "up" here on purpose: bringing up an anonymity layer that nobody
    # asked for would change where every request in the session comes from.
    tor: Literal["auto", "skip"] = "auto"
    quick: bool = False
    timeout_ms: int = Field(default=15000, ge=1000, le=120000)


class InitStep(BaseModel):
    """Mirrors contracts/init.schema.json#/$defs/InitStep."""

    model_config = ConfigDict(extra="forbid")

    name: StepName
    status: Status
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    hint: str | None = None
    elapsed_ms: float = 0.0


class InitCapabilities(BaseModel):
    """Mirrors contracts/init.schema.json#/$defs/InitCapabilities."""

    model_config = ConfigDict(extra="forbid")

    web_search: Capability
    web_fetch: Capability
    arxiv: Capability
    github: Capability
    searxng: Capability
    proxy: Capability
    vpn: Capability
    tor: Capability = CAP_OFF  # type: ignore[assignment]


class InitEngines(BaseModel):
    """Mirrors contracts/init.schema.json#/$defs/InitEngines."""

    model_config = ConfigDict(extra="forbid")

    answered: list[str] = Field(default_factory=list)
    silent: list[str] = Field(default_factory=list)
    reached_via_searxng: list[str] = Field(default_factory=list)
    searxng_engines_active: int | None = Field(default=None, ge=0)


class InitPayload(BaseModel):
    """Mirrors contracts/init.schema.json#/$defs/InitPayload."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    state: State
    headline: str
    steps: list[InitStep]
    capabilities: InitCapabilities
    engines: InitEngines
    next_actions: list[str] = Field(default_factory=list)
    doctor: dict[str, Any]


# --- the run --------------------------------------------------------------------------


def run(
    request: InitRequest | None = None,
    *,
    searxng_control: Callable[[Any], Envelope] | None = None,
    tor_control: Callable[[Any], Envelope] | None = None,
    doctor_factory: Callable[..., Any] | None = None,
) -> Envelope:
    """Bring everything online and report the result. Never raises for a broken layer."""
    request = request or InitRequest()
    started = time.perf_counter()

    steps = [_step_env(), _step_tor(request, tor_control), _step_searxng(request, searxng_control)]
    doctor_step, doctor_payload = _step_doctor(request, doctor_factory)
    steps.append(doctor_step)

    checks = {c.get("name"): c for c in (doctor_payload.get("checks") or [])}
    layers = doctor_payload.get("layers") or {}
    capabilities = _capabilities(checks)
    engines = _engines(checks, steps)
    state = _state(request, capabilities, doctor_payload, steps)
    payload = InitPayload(
        ready=state == "ready",
        state=state,
        headline=_headline(state, capabilities, engines, layers, checks),
        steps=steps,
        capabilities=capabilities,
        engines=engines,
        next_actions=_next_actions(state, steps, checks),
        doctor=doctor_payload,
    )
    return ok_envelope(
        INIT_CONTRACT_VERSION,
        payload.model_dump(mode="json"),
        layer="init",
        backend=None,
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
        ready=payload.ready,
    )


def _timed(fn: Callable[[], InitStep]) -> InitStep:
    start = time.perf_counter()
    step = fn()
    return step.model_copy(update={"elapsed_ms": round((time.perf_counter() - start) * 1000, 1)})


# --- step 1: the env file -------------------------------------------------------------


def _step_env() -> InitStep:
    def go() -> InitStep:
        configured = os.environ.get(ENV_FILE_VAR)
        # Every file the loader would read, in its own precedence order, so the step
        # answers "where do my settings go" rather than only "did one file exist".
        candidates = env_file_candidates()
        # Re-reading is the point, not a no-op: a file may have gained variables since
        # this process started, and exported variables still win, so nothing already set
        # can be overwritten from disk.
        loaded = load_env_file()
        present = [p for p in candidates if p.exists()]
        # Names only. These values are credentials often enough that printing them is
        # never worth it.
        detail = {
            "path": str(present[0]) if present else str(candidates[0]),
            "exists": bool(present),
            "candidates": [{"path": str(p), "exists": p.exists()} for p in candidates],
            "loaded": loaded,
        }
        if not present:
            return InitStep(
                name="env",
                status=OK,
                summary=f"no settings file yet; using the environment as it is "
                f"(looked in {', '.join(str(p) for p in candidates)})",
                detail=detail,
                hint=(
                    f"Put proxy, Tor, and SearXNG settings in {user_env_file()} to keep "
                    f"them out of your shell history and out of any one project. "
                    f"{ENV_FILE_VAR} overrides that path."
                ),
            )
        read = ", ".join(str(p) for p in present)
        return InitStep(
            name="env",
            status=OK,
            summary=f"read {read}" + (f" ({len(loaded)} new variables)" if loaded else ""),
            detail=detail,
            hint=None
            if configured or present[0] == user_env_file()
            else f"These settings come from {present[0]}, which is tied to this working "
            f"directory; {user_env_file()} applies wherever you run the command.",
        )

    return _timed(go)


# --- step 2: Tor --------------------------------------------------------------------


def _step_tor(request: InitRequest, tor_control: Callable[[Any], Envelope] | None) -> InitStep:
    def go() -> InitStep:
        from .optional_layers import tor_state

        layer = tor_state()
        if not layer.enabled:
            return InitStep(
                name="tor",
                status=SKIPPED,
                summary=f"off ({TOR_ENV} is not set); egress is not going through Tor",
                detail={"socks_url": None},
            )
        if layer.error:
            return InitStep(
                name="tor",
                status=WARN,
                summary=f"the tor layer is on but unusable: {layer.error}",
                detail={"socks_url": None},
                hint=f"Fix {TOR_ENV} or set it to off; everything else still works.",
            )
        if request.tor == "skip":
            return InitStep(
                name="tor",
                status=SKIPPED,
                summary=f"not touched (tor=skip); {TOR_ENV} says egress goes through {layer.value}",
                detail={"socks_url": layer.value},
            )

        from .tor_local import TorRequest, control

        envelope = (tor_control or control)(TorRequest(action="up"))
        if not envelope.ok:
            message = envelope.error.message if envelope.error else "the bring-up failed"
            return InitStep(
                name="tor",
                status=FAIL,
                summary=f"Tor did not come up: {message}",
                detail={"error": envelope.error.code if envelope.error else None},
                # A failed Tor with the layer on is not a degradation to work around: the
                # requests it was meant to anonymize would leave the machine unprotected,
                # so the honest move is to say so and let the caller decide.
                hint="Search still works, but NOT through Tor: with the layer on and no "
                "Tor, .onion is unreachable and onion search is unavailable. Run "
                "`websearch tor up` for the full output, or set WEBSEARCH_TOR=off to "
                "search the clearnet deliberately.",
            )
        data = envelope.data or {}
        verified = data.get("is_tor")
        summary = f"Tor answering at {data.get('socks_url')}"
        if data.get("version"):
            summary += f" (expert bundle {data['version']})"
        if data.get("upstream"):
            summary += f", dialing out through {data['upstream']}"
        if verified is False:
            return InitStep(
                name="tor",
                status=FAIL,
                summary=f"{data.get('socks_url')} answers but the traffic is not Tor",
                detail=dict(data),
                hint="Something else is bound to that port. Stop it, or move Tor with "
                "WEBSEARCH_TOR_PORT.",
            )
        return InitStep(
            name="tor",
            status=OK if verified else WARN,
            summary=summary + ("" if verified else "; could not confirm with check.torproject.org"),
            detail=dict(data),
            hint=None
            if verified
            else "The SOCKS port answers and Tor bootstrapped, but the check service could "
            "not be reached to confirm the exit. Re-run `websearch tor status`.",
        )

    return _timed(go)


# --- step 3: SearXNG ------------------------------------------------------------------


def _step_searxng(
    request: InitRequest, searxng_control: Callable[[Any], Envelope] | None
) -> InitStep:
    def go() -> InitStep:
        from .searxng_local import SearxngRequest, control

        configured = (os.environ.get(SEARXNG_ENV) or "").strip()
        if request.searxng == "skip":
            if configured:
                return InitStep(
                    name="searxng",
                    status=SKIPPED,
                    summary=f"not touched (searxng=skip); {SEARXNG_ENV} already points at "
                    f"{configured}",
                    detail={"url": configured},
                )
            return InitStep(
                name="searxng",
                status=SKIPPED,
                summary="not requested (searxng=skip); searching on the keyless engines only",
                detail={"url": None},
            )

        envelope = (searxng_control or control)(SearxngRequest(action="up"))
        if not envelope.ok:
            message = envelope.error.message if envelope.error else "the bring-up failed"
            return InitStep(
                name="searxng",
                status=WARN,
                summary=f"SearXNG did not come up: {message}",
                detail={"error": envelope.error.code if envelope.error else None},
                hint="Search still works on the keyless engines. Fix this only if results "
                "keep coming back thin: `websearch searxng up` repeats the step with its "
                "own output, and the log path is in the message above.",
            )
        data = envelope.data or {}
        if not data.get("healthy"):
            return InitStep(
                name="searxng",
                status=WARN,
                summary=f"SearXNG is installed but not answering at {data.get('url')}",
                detail=dict(data),
                hint=f"Search still works on the keyless engines. Its log is at {data.get('log')}.",
            )
        url = str(data["url"])
        # This process is the one that has to use it. Recording the URL in the env file
        # only helps the next command; setting it here is what makes the doctor sweep
        # below actually fan out to it.
        os.environ[SEARXNG_ENV] = url
        version = data.get("version")
        active, available = data.get("engines_active"), data.get("engines_available")
        engines = f", {active} of {available} engines" if active else ""
        return InitStep(
            name="searxng",
            status=OK,
            summary=f"SearXNG{f' {version}' if version else ''} answering at {url}{engines}",
            detail=dict(data),
        )

    return _timed(go)


# --- step 4: the sweep ----------------------------------------------------------------


def _step_doctor(
    request: InitRequest, doctor_factory: Callable[..., Any] | None
) -> tuple[InitStep, dict[str, Any]]:
    from .doctor import DoctorRequest, build_doctor

    start = time.perf_counter()
    doctor = (doctor_factory or build_doctor)()
    envelope = doctor.run(DoctorRequest(quick=request.quick, timeout_ms=request.timeout_ms))
    elapsed = round((time.perf_counter() - start) * 1000, 1)
    payload = envelope.model_dump(mode="json").get("data") or {}
    summary = payload.get("summary") or {}
    counts = (
        f"{summary.get('ok', 0)} ok, {summary.get('warn', 0)} warn, "
        f"{summary.get('fail', 0)} fail, {summary.get('skipped', 0)} skipped"
    )
    if summary.get("fail"):
        status: Status = FAIL
    elif summary.get("warn"):
        status = WARN
    else:
        status = OK
    step = InitStep(
        name="doctor",
        status=status,
        summary=counts + (" (quick sweep)" if request.quick else ""),
        detail={"quick": request.quick, **summary},
        # No hint: a failed check carries its own, and next_actions already collects them.
        elapsed_ms=elapsed,
    )
    return step, payload


# --- deriving the answer --------------------------------------------------------------


_FROM_CHECK: dict[str, str] = {
    OK: CAP_OK,
    WARN: CAP_DEGRADED,
    FAIL: CAP_DOWN,
    SKIPPED: CAP_OFF,
}


def _cap(checks: dict[str, Any], name: str) -> str:
    check = checks.get(name)
    if check is None:
        return CAP_UNKNOWN
    return _FROM_CHECK.get(check.get("status"), CAP_UNKNOWN)


def _capabilities(checks: dict[str, Any]) -> InitCapabilities:
    # A silent provider or two is the normal state of keyless search: the fanout rotates
    # and fuses whatever answers, so the aggregate's warn still means "search works".
    engines = checks.get("engines")
    if engines is not None:
        web_search = CAP_DOWN if engines.get("status") == FAIL else CAP_OK
    elif (checks.get("internet") or {}).get("status") == FAIL:
        web_search = CAP_DOWN
    else:
        web_search = CAP_UNKNOWN
    return InitCapabilities(
        web_search=web_search,  # type: ignore[arg-type]
        web_fetch=_cap(checks, "fetch:http"),  # type: ignore[arg-type]
        arxiv=_cap(checks, "tool:arxiv"),  # type: ignore[arg-type]
        github=_cap(checks, "tool:github"),  # type: ignore[arg-type]
        searxng=_cap(checks, "searxng"),  # type: ignore[arg-type]
        proxy=_cap(checks, "proxy"),  # type: ignore[arg-type]
        vpn=_cap(checks, "vpn"),  # type: ignore[arg-type]
        tor=_cap(checks, "tor"),  # type: ignore[arg-type]
    )


def _engines(checks: dict[str, Any], steps: list[InitStep]) -> InitEngines:
    detail = (checks.get("engines") or {}).get("detail") or {}
    searxng_step = next((s for s in steps if s.name == "searxng"), None)
    active = (searxng_step.detail or {}).get("engines_active") if searxng_step else None
    return InitEngines(
        answered=list(detail.get("answered") or []),
        silent=list(detail.get("silent") or []),
        reached_via_searxng=list(detail.get("reached_via_searxng") or []),
        searxng_engines_active=active,
    )


def _state(
    request: InitRequest,
    capabilities: InitCapabilities,
    doctor_payload: dict[str, Any],
    steps: list[InitStep],
) -> State:
    if capabilities.web_search == CAP_DOWN:
        return "broken"
    # A bring-up step that warned is a degradation even when the sweep looks fine, or its
    # hint would be dropped from next_actions and the caller told everything is online.
    # The doctor step is excluded: its warns are the measurements, already in capabilities
    # (a couple of silent providers is the normal state of keyless search, not a fault).
    if any(step.status in (WARN, FAIL) for step in steps if step.name != "doctor"):
        return "degraded"
    if (doctor_payload.get("summary") or {}).get("fail"):
        return "degraded"
    if request.searxng == "up" and capabilities.searxng != CAP_OK:
        return "degraded"
    if CAP_DOWN in (capabilities.web_fetch, capabilities.arxiv, capabilities.github):
        return "degraded"
    return "ready"


def _headline(
    state: State,
    capabilities: InitCapabilities,
    engines: InitEngines,
    layers: dict[str, Any],
    checks: dict[str, Any],
) -> str:
    if capabilities.web_search == CAP_DOWN:
        why = (checks.get("engines") or {}).get("summary") or "no engine answered"
        return f"broken: web search is down ({why})"

    answered, silent = len(engines.answered), len(engines.silent)
    if answered or silent:
        search = f"search online ({answered} of {answered + silent} keyless providers"
    else:
        search = "search online (keyless providers not probed"
    if capabilities.searxng == CAP_OK:
        count = engines.searxng_engines_active
        search += f" + SearXNG{f', {count} engines' if count else ''})"
    else:
        search += ")"

    named = (
        ("fetch", capabilities.web_fetch),
        ("arxiv", capabilities.arxiv),
        ("github", capabilities.github),
    )
    working = [name for name, cap in named if cap == CAP_OK]
    broken = [name for name, cap in named if cap in (CAP_DOWN, CAP_DEGRADED)]
    parts = [search]
    if working:
        parts.append(f"{', '.join(working)} ok")
    if broken:
        parts.append(f"{', '.join(broken)} not working")
    proxy = layers.get("proxy") or {}
    tor = layers.get("tor") or {}
    proxied = proxy.get("enabled") and capabilities.proxy == CAP_OK
    if tor.get("enabled") and capabilities.tor in (CAP_OK, CAP_DEGRADED):
        # Name both hops when both are in the path: "through Tor" alone would read as the
        # proxy having been dropped, which is exactly what did not happen.
        via = f"egress through Tor at {tor.get('value')}"
        parts.append(f"{via} via {proxy.get('value')}" if proxied else via)
    else:
        parts.append(f"egress through {proxy.get('value')}" if proxied else "direct egress")
    return f"{state}: " + "; ".join(parts)


def _next_actions(state: State, steps: list[InitStep], checks: dict[str, Any]) -> list[str]:
    if state == "ready":
        return []
    actions: list[str] = []

    def add(text: str | None) -> None:
        if text and text not in actions:
            actions.append(text)

    for step in steps:
        if step.status in (WARN, FAIL):
            add(step.hint)
    for check in checks.values():
        if check.get("status") == FAIL:
            add(check.get("hint"))
    return actions
