"""Shared helpers for the HTTP fetch tiers."""

from __future__ import annotations

# A current, realistic desktop Chrome UA used when the caller does not supply one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


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
