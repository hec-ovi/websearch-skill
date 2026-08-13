"""Shared helpers for the HTTP fetch tiers."""

from __future__ import annotations

from ..models import Cookie

MAX_REDIRECTS = 10
REDIRECT_STATUS = (301, 302, 303, 307, 308)


def header(headers: dict[str, str], name: str) -> str | None:
    """Case-insensitive header read over a plain dict of response headers."""
    name = name.lower()
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return None

# A current, realistic desktop Chrome UA used when the caller does not supply one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def hop_headers(headers: dict[str, str], origin_host: str, host: str | None) -> dict[str, str]:
    """The caller headers safe to send on one redirect hop.

    Credentials are scoped to the host the caller addressed: a redirect to a different
    host must not receive the Authorization header or a raw Cookie header, or a
    malicious/compromised origin could exfiltrate them cross-origin.
    """
    if (host or "").lower() == origin_host:
        return dict(headers)
    return {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie")}


def hop_cookies(cookies: list[Cookie], origin_host: str, host: str | None) -> dict[str, str]:
    """The caller cookies that match ``host`` for one redirect hop.

    A cookie without a domain belongs to the host the caller addressed and is dropped on
    any cross-origin redirect; a domain-scoped cookie goes only to matching hosts
    (the domain itself or a subdomain of it).
    """
    h = (host or "").lower()
    out: dict[str, str] = {}
    for c in cookies:
        if c.domain is None:
            if h == origin_host:
                out[c.name] = c.value
            continue
        d = c.domain.lstrip(".").lower()
        if h == d or h.endswith("." + d):
            out[c.name] = c.value
    return out


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip("\"'") or None
    return None


def read_body(content: bytes, content_type: str | None, max_bytes: int | None) -> str:
    """Decode a response body to text, honoring ``max_bytes`` as a transport guard.

    ``max_bytes`` bounds how much we hand downstream (a defense against multi-GB
    transfers); it is NOT a content/LLM cap. The charset is taken from the declared
    Content-Type, then detected from the bytes (via charset_normalizer, a trafilatura
    dependency), and only then does it fall back to UTF-8, so a page that omits its
    charset is not turned into mojibake by a blind UTF-8 decode.
    """
    if max_bytes is not None and len(content) > max_bytes:
        content = content[:max_bytes]

    declared = _charset_from_content_type(content_type)
    if declared:
        try:
            return content.decode(declared, errors="replace")
        except LookupError:
            pass

    try:
        from charset_normalizer import from_bytes

        best = from_bytes(content).best()
        if best is not None:
            return str(best)
    except Exception:
        pass

    return content.decode("utf-8", errors="replace")
