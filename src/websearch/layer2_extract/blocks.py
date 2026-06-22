"""Anti-bot block detection for a plain HTTP fetch.

Decides ``(blocked, reason)`` from ``(status, body, headers)`` with a bias toward
few false positives. The recipe (verified June 2026 against FlareSolverr's detection
source and vendor bypass writeups):

1. Definitive *header* markers fire regardless of status (vendor-exclusive, safe).
2. *Body* markers fire only when the body is short or the status is on the
   anti-bot shortlist, EXCEPT Imperva/Incapsula, which serves blocks with HTTP 200,
   so its body strings are scanned unconditionally.
3. Status-only fallbacks (429 rate-limit, suspected 403/503 blocks).

``reason`` uses the recommended vocabulary so the fetch router can branch:
escalatable causes (vendor challenges) warrant a stealthier tier; terminal causes
(rate_limited, auth_required, legal_geo_block) do not.
"""

from __future__ import annotations

import re

# A short body is the gate for generic body-marker scanning (a full-size article that
# merely links to challenges.cloudflare.com must not be misflagged).
_SHORT_BODY_BYTES = 30_000

# Status codes that are anti-bot *candidates* (still need a marker to be conclusive,
# except via the status-only fallbacks below).
_CANDIDATE_STATUS = {403, 429, 503}

# Per-vendor body substrings (matched case-insensitively).
_BODY_MARKERS: dict[str, tuple[str, ...]] = {
    "cloudflare_challenge": (
        "just a moment...",
        "attention required! | cloudflare",
        "checking your browser before you access",
        "verifying you are human",
        "enable javascript and cookies to continue",
        "performance & security by cloudflare",
        "cf-challenge-running",
        "challenges.cloudflare.com",
        "/cdn-cgi/challenge-platform/",
        "cf-please-wait",
    ),
    "cloudflare_firewall": ("error code: 1020", "error 1020", ">1020<"),
    "datadome": ("captcha-delivery.com", '"dd":{', "var dd=", "geo.captcha-delivery.com"),
    "perimeterx": (
        "px-captcha",
        "captcha.px-cdn.net",
        "client.perimeterx.net",
        "perimeterx.net",
        "_pxappid",
        "press & hold",
        "press and hold",
    ),
    "akamai": ("errors.edgesuite.net", "akamaighost"),
    "ddos_guard": ("ddos-guard.net", "ddos-guard"),
}

# Imperva/Incapsula blocks can arrive with HTTP 200, so scan these on any status.
_IMPERVA_MARKERS = (
    "request unsuccessful. incapsula incident id",
    "_incapsula_resource",
    "subject=waf block page",
    "powered by incapsula",
)

# Vendor-exclusive body markers scanned on ANY status and ANY body size: Imperva
# blocks with 200, and these DataDome/PerimeterX strings are effectively zero false
# positive (vendor domains/identifiers), so a large 200 interstitial without a vendor
# response header is still caught.
_ALWAYS_SCAN_MARKERS: dict[str, tuple[str, ...]] = {
    "imperva": _IMPERVA_MARKERS,
    "datadome": ("captcha-delivery.com", "geo.captcha-delivery.com"),
    "perimeterx": ("client.perimeterx.net", "captcha.px-cdn.net", "_pxappid", "px-captcha"),
}

# Specific multi-word block/error phrases: distinctive enough to flag anywhere in a title.
_STRONG_ERROR_PHRASES = (
    "not found",
    "access denied",
    "just a moment",
    "are you a robot",
    "are you human",
    "attention required",
    "request blocked",
    "verify you are human",
    "page unavailable",
    "site unavailable",
)
_STRONG_ERROR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(p) for p in _STRONG_ERROR_PHRASES) + r")\b"
)
# Ambiguous single words and bare HTTP codes ("forbidden", "404"): a legitimate title can
# contain them ("Forbidden City", "Top 500 Companies"), so they count only in a SHORT or
# explicitly-error-shaped title. The quality.py veto additionally corroborates with a low
# word count, so a long real article is never tanked.
_WEAK_ERROR_RE = re.compile(r"\b(?:forbidden|40[0-9]|50[0-9])\b")


def _akamai_deny(body_lc: str) -> bool:
    # Akamai's reference-number deny page (avoid firing on a generic "Access Denied").
    return ("access denied" in body_lc or "you don't have permission to access" in body_lc) and (
        "reference #" in body_lc or "reference&#32;#" in body_lc
    )


def detect_block(status: int, body: str, headers: dict[str, str]) -> tuple[bool, str | None]:
    """Return ``(blocked, reason)``. ``reason`` is None when not blocked."""
    h = {k.lower(): str(v).lower() for k, v in headers.items()}

    # 1. Header markers that correlate with a challenge/block (NOT mere CDN presence).
    if "challenge" in h.get("cf-mitigated", ""):
        return True, "cloudflare_challenge"
    if "x-datadome" in h or "x-dd-b" in h:
        return True, "datadome"
    if "x-px-authorization" in h:
        return True, "perimeterx"
    if "akamaighost" in h.get("server", "") and status in (403, 429):
        return True, "akamai"
    # NOTE: x-iinfo / x-cdn:incapsula are deliberately NOT treated as a block. Imperva
    # adds them to every proxied response (a diagnostics/CDN identifier), not just
    # challenges, so a header-only rule false-positives on all Imperva-fronted sites.
    # Real Imperva blocks are caught by the body markers below (they arrive with 200).

    body_lc = body.lower()

    # 2a. Vendor-exclusive body markers fire on any status and any body size.
    for reason, markers in _ALWAYS_SCAN_MARKERS.items():
        if any(m in body_lc for m in markers):
            return True, reason

    # 2b. Other body markers only when short OR on the candidate-status shortlist.
    is_short = len(body) < _SHORT_BODY_BYTES
    if is_short or status in _CANDIDATE_STATUS:
        for reason, markers in _BODY_MARKERS.items():
            if any(m in body_lc for m in markers):
                return True, reason
        if _akamai_deny(body_lc):
            return True, "akamai"

    # 3. Status-only fallbacks (no marker found).
    if status == 429:
        return True, "rate_limited"
    if status in (401,):
        return False, "auth_required"
    if status in (451,):
        return False, "legal_geo_block"
    if status == 403 and is_short and len(body_lc.split()) < 50:
        return True, "forbidden_suspected_bot"
    if status == 503 and is_short and "retry-after" not in h:
        return True, "unavailable_suspected_block"

    return False, None


def title_looks_like_error(title: str | None) -> bool:
    if not title:
        return False
    t = title.strip().lower()
    if _STRONG_ERROR_RE.search(t):
        return True
    if _WEAK_ERROR_RE.search(t):
        # A weak signal (bare "forbidden"/HTTP code) counts only in a short or explicitly
        # error-shaped title, so "Forbidden City" / "Top 500 Companies" do not trip it.
        return len(t.split()) <= 4 or "error" in t
    return False
