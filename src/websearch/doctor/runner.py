"""The doctor: plan the checks, run them, aggregate.

Checks run in four phases because the later ones read the earlier ones' answers: the
local phase (no network), then the direct-egress baseline, then the optional layers, then
everything that actually searches or fetches. Within a phase the checks are independent
and run concurrently, so a whole run costs about one slow engine, not the sum of them.

Nothing here decides that a broken install is fatal. Statuses are reported as measured;
the CLI turns ``summary.fail`` into an exit code.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from ..envelope import Envelope, ok_envelope
from ..layer1_search import DdgsAdapter, SearchRequest
from ..optional_layers import LayerState, layer_states, resolved_proxy_url
from ..proxy import proxy_for
from . import probes
from .models import (
    DOCTOR_CONTRACT_VERSION,
    FAIL,
    GROUP_ORDER,
    OK,
    SKIPPED,
    WARN,
    CheckResult,
    DoctorPayload,
    DoctorRequest,
    DoctorSummary,
    Group,
    OptionalLayer,
)

# Groups that cost a real query per engine or per page. `--quick` drops them.
SLOW_GROUPS: tuple[Group, ...] = ("engines", "tools", "fetch")

MAX_WORKERS = 8


class Doctor:
    """Runs the checks against one configuration of the three optional layers."""

    def __init__(
        self,
        *,
        layers: dict[str, LayerState],
        proxy_url: str | None,
        net: probes.Net | None = None,
        ddgs_factory: Callable[[str], object] | None = None,
        pipeline_factory: Callable[..., object] | None = None,
        arxiv_factory: Callable[..., object] | None = None,
        github_factory: Callable[..., object] | None = None,
    ):
        self._layers = layers
        self._proxy_url = proxy_url
        self._net = net or probes.HttpxNet()
        # The same seam the ddgs adapter exposes, plus the backend name: ddgs talks to the
        # network through a Rust client, so a fake DDGS is the only way to exercise the
        # engine path offline, and a per-backend fake is the only way to make one
        # provider fail without depending on which thread got there first.
        self._ddgs_factory = ddgs_factory
        self._pipeline_factory = pipeline_factory
        self._arxiv_factory = arxiv_factory
        self._github_factory = github_factory

    # --- plumbing -------------------------------------------------------------------

    def _layer(self, name: str) -> LayerState:
        return self._layers[name]

    @property
    def _proxy_enabled(self) -> bool:
        state = self._layer("proxy")
        return state.enabled and not state.error

    def _effective_proxy(self) -> str | None:
        return self._proxy_url if self._proxy_enabled else None

    def _build_pipeline(self):
        if self._pipeline_factory is not None:
            return self._pipeline_factory(proxy=self._effective_proxy())
        from ..layer2_extract import build_pipeline

        return build_pipeline(proxy=self._effective_proxy())

    def _build_arxiv(self):
        if self._arxiv_factory is not None:
            return self._arxiv_factory(proxy=self._effective_proxy())
        from ..tool_arxiv import build_arxiv_tool

        return build_arxiv_tool(proxy=self._effective_proxy())

    def _build_github(self):
        if self._github_factory is not None:
            return self._github_factory(proxy=self._effective_proxy())
        from ..tool_github import build_github_tool

        return build_github_tool(proxy=self._effective_proxy())

    # --- the plan -------------------------------------------------------------------

    def _plan(self, request: DoctorRequest) -> list[tuple[str, Group, str | None]]:
        """Every check this run will attempt: (name, group, optional_layer)."""
        plan: list[tuple[str, Group, str | None]] = [
            ("runtime", "runtime", None),
            ("dependencies", "runtime", None),
            ("mcp", "mcp", None),
            ("internet", "egress", None),
            ("baseline", "egress", None),
            ("proxy", "egress", "proxy"),
            ("vpn", "vpn", "vpn"),
            ("searxng", "searxng", "searxng"),
        ]
        # The default path first, then the per-provider breakdown. ddgs on `auto` rotates
        # and retries across providers, so it routinely answers when several individual
        # providers do not; the two together say both "does search work" and "which
        # providers are reachable from this IP today".
        plan.append(("engine:ddgs", "engines", None))
        for backend in probes.ddgs_backends():
            plan.append((f"engine:ddgs:{backend}", "engines", None))
        plan.append(("tool:arxiv", "tools", None))
        plan.append(("tool:github", "tools", None))
        plan.append(("fetch:http", "fetch", None))
        plan.append(("fetch:impersonation", "fetch", None))
        return [item for item in plan if self._selected(item, request)]

    @staticmethod
    def _selected(item: tuple[str, Group, str | None], request: DoctorRequest) -> bool:
        name, group, _ = item
        if request.checks:
            # An explicit selection overrides --quick: asking for engine:ddgs:google and
            # being handed nothing would be worse than slow.
            return any(name == s or name.startswith(s) or group == s for s in request.checks)
        return not (request.quick and group in SLOW_GROUPS)

    # --- execution ------------------------------------------------------------------

    def run(self, request: DoctorRequest | None = None) -> Envelope:
        request = request or DoctorRequest()
        t0 = time.perf_counter()
        plan = self._plan(request)
        names = {name for name, _, _ in plan}
        groups = {name: (group, layer) for name, group, layer in plan}
        results: dict[str, CheckResult] = {}

        def record(name: str, outcome: probes.Outcome, elapsed_ms: float) -> None:
            group, layer = groups[name]
            results[name] = CheckResult(
                name=name,
                group=group,
                status=outcome.status,
                summary=outcome.summary,
                detail=outcome.detail,
                hint=outcome.hint,
                elapsed_ms=round(elapsed_ms, 1),
                optional_layer=layer,
            )

        def run_one(name: str, fn: Callable[[], probes.Outcome]) -> None:
            start = time.perf_counter()
            try:
                outcome = fn()
            except Exception as exc:  # a probe must never take the whole run down
                outcome = probes.Outcome(
                    FAIL, f"the check itself raised {type(exc).__name__}: {exc}"
                )
            record(name, outcome, (time.perf_counter() - start) * 1000)

        def run_all(jobs: dict[str, Callable[[], probes.Outcome]]) -> None:
            jobs = {n: f for n, f in jobs.items() if n in names}
            if not jobs:
                return
            if len(jobs) == 1:
                name, fn = next(iter(jobs.items()))
                run_one(name, fn)
                return
            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
                list(pool.map(lambda item: run_one(*item), jobs.items()))

        timeout_s = request.timeout_s

        # Phase 1: local only.
        run_all(
            {
                "runtime": probes.probe_runtime,
                "dependencies": probes.probe_dependencies,
                "mcp": probes.probe_mcp,
            }
        )

        # Phase 2: connectivity along the path the tool actually uses, plus the opt-in
        # direct baseline. With a proxy configured, both of these go through it unless
        # --baseline explicitly allows the one request that does not.
        proxy_url = self._proxy_url
        path_proxy = self._effective_proxy()
        run_all(
            {
                "internet": lambda: probes.probe_internet(
                    self._net, timeout_s=timeout_s, proxy=path_proxy
                ),
                "baseline": lambda: probes.probe_baseline(
                    self._net,
                    timeout_s=timeout_s,
                    # With no proxy configured, "direct" is already the only path, so the
                    # baseline is free and always worth having.
                    requested=request.baseline or not self._proxy_enabled,
                ),
            }
        )
        baseline = results.get("baseline")
        direct_ip = (baseline.detail.get("exit_ip") if baseline else None) or None

        # Phase 3: the three optional layers.
        run_all(
            {
                "proxy": lambda: probes.probe_proxy(
                    self._layer("proxy"),
                    proxy_url,
                    self._net,
                    timeout_s=timeout_s,
                    direct_ip=direct_ip,
                ),
                "vpn": lambda: probes.probe_vpn(
                    self._layer("vpn"),
                    self._net,
                    timeout_s=timeout_s,
                    proxy_url=proxy_url,
                    proxy_enabled=self._proxy_enabled,
                ),
                "searxng": lambda: probes.probe_searxng(
                    self._layer("searxng"),
                    self._net,
                    timeout_s=timeout_s,
                    proxy_url=proxy_url,
                    query=request.query,
                ),
            }
        )

        # Phase 4: everything that issues a real query or fetch.
        run_all(self._work_jobs(request))

        # Phase 5: ask SearXNG about the providers ddgs could not get results from.
        self._cross_check_silent_providers(results, request)

        checks = self._ordered(results, plan)
        engines_aggregate = self._engines_aggregate(checks)
        if engines_aggregate is not None:
            checks.insert(
                min(i for i, c in enumerate(checks) if c.group == "engines"), engines_aggregate
            )

        summary = DoctorSummary(
            ok=sum(1 for c in checks if c.status == OK),
            warn=sum(1 for c in checks if c.status == WARN),
            fail=sum(1 for c in checks if c.status == FAIL),
            skipped=sum(1 for c in checks if c.status == SKIPPED),
            total=len(checks),
        )
        payload = DoctorPayload(
            checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
            layers={name: OptionalLayer(**state.as_dict()) for name, state in self._layers.items()},
            checks=checks,
            summary=summary,
            warnings=self._warnings(),
        )
        return ok_envelope(
            DOCTOR_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer="doctor",
            backend=None,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            trace_id=uuid.uuid4().hex,
            healthy=summary.fail == 0,
        )

    def _work_jobs(self, request: DoctorRequest) -> dict[str, Callable[[], probes.Outcome]]:
        """Phase-4 jobs, each closing over its own adapter so they stay independent."""
        jobs: dict[str, Callable[[], probes.Outcome]] = {}
        proxy = self._effective_proxy()
        search_request = SearchRequest(query=request.query, count=5, timeout_ms=request.timeout_ms)

        def ddgs_job(backend: str) -> Callable[[], probes.Outcome]:
            injected = self._ddgs_factory
            factory = None if injected is None else (lambda: injected(backend))

            def job() -> probes.Outcome:
                adapter = DdgsAdapter(ddgs_factory=factory, backend=backend, proxy=proxy)
                return probes.probe_engine(adapter, search_request)

            return job

        jobs["engine:ddgs"] = ddgs_job("auto")
        for backend in probes.ddgs_backends():
            jobs[f"engine:ddgs:{backend}"] = ddgs_job(backend)

        jobs["tool:arxiv"] = lambda: probes.probe_arxiv(self._build_arxiv(), request.query)
        jobs["tool:github"] = lambda: probes.probe_github(self._build_github(), request.query)
        jobs["fetch:http"] = lambda: probes.probe_fetch(
            self._build_pipeline(), request.fetch_url, timeout_ms=request.timeout_ms
        )
        jobs["fetch:impersonation"] = probes.probe_impersonation
        return jobs

    def _cross_check_silent_providers(
        self, results: dict[str, CheckResult], request: DoctorRequest
    ) -> None:
        """Tell a broken parser apart from a provider that is refusing this IP.

        ddgs says "No results found" for a CAPTCHA, a rate limit, and a 200 whose markup
        it can no longer parse. SearXNG scrapes the same providers with independently
        maintained parsers, so asking it about exactly the silent ones settles which it
        was. Only runs when SearXNG is up, and only for the providers that went quiet.
        """
        state = self._layer("searxng")
        if not state.enabled or not state.value:
            return
        silent = {
            check.name.split(":")[-1]: check
            for name, check in results.items()
            if name.startswith("engine:ddgs:") and check.status == WARN
        }
        if not silent:
            return
        proxy = proxy_for(state.value, self._proxy_url if self._proxy_enabled else None)

        def ask(engine: str):
            return engine, probes.probe_searxng_engine(
                self._net,
                state.value,
                engine,
                query=request.query,
                timeout_s=request.timeout_s,
                proxy=proxy,
            )

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(silent))) as pool:
            answers = dict(pool.map(ask, silent))

        for engine, check in silent.items():
            reached, count, error = answers.get(engine, (False, 0, "not checked"))
            check.detail["searxng_cross_check"] = {
                "reached": reached,
                "results": count,
                "error": error,
            }
            if reached:
                check.summary = f"{check.summary} (SearXNG reached it: {count} results)"
                check.hint = (
                    f"The provider is up: SearXNG's own {engine} parser got {count} results "
                    "for the same query, so this is ddgs's parser, not a block on your IP. "
                    "Query this provider through SearXNG."
                )
            elif error:
                check.hint = (
                    f"No second opinion: SearXNG could not be asked about {engine} ({error})."
                )
            else:
                check.summary = f"{check.summary} (SearXNG got nothing from it either)"
                check.hint = (
                    f"Both parsers came back empty, so {engine} is refusing this IP (CAPTCHA "
                    "or rate limit). A different egress exit is what changes that, not a "
                    "different parser."
                )

    @staticmethod
    def _ordered(
        results: dict[str, CheckResult], plan: list[tuple[str, Group, str | None]]
    ) -> list[CheckResult]:
        by_group: dict[str, list[CheckResult]] = {}
        for name, group, _ in plan:
            check = results.get(name)
            if check is not None:
                by_group.setdefault(group, []).append(check)
        out: list[CheckResult] = []
        for group in GROUP_ORDER:
            out.extend(by_group.get(group, []))
        return out

    def _engines_aggregate(self, checks: list[CheckResult]) -> CheckResult | None:
        """One line for the fanout as a whole.

        A single provider going quiet is a warn, not a failure: ddgs rotates and the
        router fuses whatever answers, so search still works. It is a failure only when
        nothing at all answered, including the default `auto` path.
        """
        engines = [c for c in checks if c.group == "engines" and c.name != "engines"]
        if not engines:
            return None
        # `engine:ddgs` is the default path (auto), not one provider; it is the deciding
        # signal but it is not counted among the providers.
        providers = [c for c in engines if c.name != "engine:ddgs"]
        default_path = next((c for c in engines if c.name == "engine:ddgs"), None)
        answered = [c for c in providers if c.status == OK]
        silent = [c.name.split(":")[-1] for c in providers if c.status not in (OK, SKIPPED)]
        detail = {
            "answered": [c.name.split(":")[-1] for c in answered],
            "silent": silent,
            "providers": len(providers),
            "default_path": default_path.status if default_path else None,
        }
        nothing_answered = not answered and (default_path is None or default_path.status != OK)
        if nothing_answered:
            return CheckResult(
                name="engines",
                group="engines",
                status=FAIL,
                summary=f"no engine answered ({len(engines)} tried)",
                detail=detail,
                hint="Check the internet and proxy results above; if those are fine, the "
                "engines are refusing this IP.",
            )
        counted = f"{len(answered)} of {len(providers)} providers answered"
        if default_path is not None and default_path.status == OK and silent:
            counted += "; the default ddgs path still works"
        rescued = [
            c.name.split(":")[-1]
            for c in providers
            if (c.detail.get("searxng_cross_check") or {}).get("reached")
        ]
        detail["reached_via_searxng"] = rescued
        if rescued:
            counted += f"; SearXNG reaches {len(rescued)} of the silent ones"
        return CheckResult(
            name="engines",
            group="engines",
            status=OK if not silent else WARN,
            summary=counted,
            detail=detail,
            hint=self._silent_hint(silent),
        )

    def _silent_hint(self, silent: list[str]) -> str | None:
        if not silent:
            return None
        hint = f"Silent this run: {', '.join(silent)}."
        if not self._layer("searxng").enabled:
            hint += (
                " Turn on SearXNG (./docker/searxng/searxng.sh up) and re-run: it parses "
                "these providers itself, which says whether they are blocking you or "
                "ddgs just cannot read their pages any more."
            )
        return hint

    def _warnings(self) -> list[str]:
        out: list[str] = []
        off = [name for name, state in self._layers.items() if not state.enabled]
        if off:
            out.append(
                f"optional layer(s) off (the default): {', '.join(sorted(off))}. "
                "Their checks are skipped, not failed."
            )
        return out


def build_doctor(
    *,
    vpn: str | None = None,
    proxy: str | None = None,
    searxng_url: str | None = None,
    net: probes.Net | None = None,
    ddgs_factory: Callable[[str], object] | None = None,
    pipeline_factory: Callable[..., object] | None = None,
    arxiv_factory: Callable[..., object] | None = None,
    github_factory: Callable[..., object] | None = None,
) -> Doctor:
    """Wire a Doctor from the environment, with CLI values overriding it.

    Each argument mirrors one optional layer's switch; passing None means "read the
    environment", and the environment defaults to off.
    """
    layers = layer_states(vpn=vpn, proxy=proxy, searxng_url=searxng_url)
    proxy_url = resolved_proxy_url(proxy)
    return Doctor(
        layers=layers,
        proxy_url=proxy_url,
        net=net,
        ddgs_factory=ddgs_factory,
        pipeline_factory=pipeline_factory,
        arxiv_factory=arxiv_factory,
        github_factory=github_factory,
    )
