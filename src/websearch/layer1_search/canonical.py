"""Deterministic URL canonicalization, used as the dedup key and the emitted url.

The L1 finding left the exact rule to this layer's owner. The chosen rule, applied
to both the dedup key and ``ResultItem.url``:

- lowercase the scheme and host; strip a leading ``www.``
- drop the fragment
- drop known tracking params (utm_*, gclid, fbclid, ...)
- keep remaining query params, sorted by key for a stable key
- drop a trailing slash except on the root path

http and https are kept distinct (a host may legitimately differ), and redirector
unwrapping is deliberately out of scope here. ``display_url`` preserves the original.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Tracking / analytics params that never change which document is addressed.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "utm_reader",
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "mc_eid",
        "mc_cid",
        "_hsenc",
        "_hsmi",
        "igshid",
        "ref",
        "ref_src",
        "ref_url",
        "spm",
        "vero_id",
        "oly_anon_id",
        "oly_enc_id",
    }
)


def canonicalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for dedup and handoff. Falls back to the
    stripped input if parsing fails."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw

    scheme = parts.scheme.lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if host.startswith("www."):
        host = host[4:]

    # Reattach a non-default port and any userinfo (userinfo is rare but preserved).
    netloc = host
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path
    if not path:
        path = "/"  # normalize bare domain so https://host and https://host/ dedupe
    elif len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in _TRACKING_PARAMS
    ]
    kept.sort(key=lambda kv: (kv[0], kv[1]))
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))
