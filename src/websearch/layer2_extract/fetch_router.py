"""The FETCH router: try the cheapest eligible tier, escalate only on a real block.

Escalation policy (per the block-detection research):
- clean success (ok and not blocked) -> return immediately;
- an escalatable block (vendor challenge, suspected-bot 403/503) -> try the next, more
  capable tier (plain httpx -> curl_cffi impersonation -> opt-in browser/stealth);
- a terminal block (rate_limited, auth_required, legal_geo_block) -> stop, a stealthier
  tier from the same egress will not help;
- a transport error (no response) -> escalate (a different client may connect);
- a genuine HTTP error (404/410/500, not blocked) -> stop, escalation cannot fix it.

``tier_hint`` and ``render_js`` filter which tiers are eligible. Requesting a browser
or stealth tier (or render_js) with no such adapter installed yields a clear,
non-crashing failure result that the pipeline reports as ``fetch_failed``.
"""

from __future__ import annotations

from .models import TERMINAL_BLOCKS, FetchRequest, FetchResult
from .ports import FetchAdapter

_TIER_CLASSES: dict[str, tuple[str, ...]] = {
    "auto": ("http", "browser", "stealth"),
    "http": ("http",),
    "browser": ("browser", "stealth"),
    "stealth": ("stealth",),
}
_TIER_VIA: dict[str, str] = {
    "auto": "http",
    "http": "http",
    "browser": "browser",
    "stealth": "nodriver",
}


class FetchRouter:
    def __init__(self, fetchers: list[FetchAdapter]):
        self._fetchers = sorted(fetchers, key=lambda f: f.escalation_order)

    @property
    def fetchers(self) -> list[FetchAdapter]:
        return list(self._fetchers)

    def _eligible(self, request: FetchRequest) -> list[FetchAdapter]:
        allowed = set(_TIER_CLASSES[request.tier_hint])
        if request.render_js is True:
            allowed &= {"browser", "stealth"}  # JS rendering needs a browser tier
        return [f for f in self._fetchers if f.tier_class in allowed and f.available()]

    def fetch(self, request: FetchRequest) -> FetchResult:
        eligible = self._eligible(request)
        if not eligible:
            return self._unavailable(request)

        attempts: list[str] = []
        last: FetchResult | None = None
        for fetcher in eligible:
            res = fetcher.fetch(request)
            attempts.append(fetcher.name)
            res.tier_attempts = list(attempts)
            last = res

            if res.ok and not res.blocked:
                return res
            if res.blocked:
                if res.block_reason in TERMINAL_BLOCKS:
                    return res
                continue  # escalatable block -> next tier
            if res.status == 0:
                continue  # transport error -> a different client may connect
            return res  # genuine HTTP error (not blocked) -> escalation cannot help

        assert last is not None  # eligible was non-empty
        return last

    def _unavailable(self, request: FetchRequest) -> FetchResult:
        if request.render_js is True or request.tier_hint in ("browser", "stealth"):
            reason = (
                f"the '{request.tier_hint}' fetch tier (JS rendering) is not installed in "
                "this build; browser/stealth tiers are opt-in adapters."
            )
        else:
            reason = "no fetch adapters are available."
        return FetchResult(
            url=request.url,
            status=0,
            ok=False,
            fetched_via=_TIER_VIA[request.tier_hint],
            error=reason,
        )
