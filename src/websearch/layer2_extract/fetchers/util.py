"""Shared helpers for the HTTP fetch tiers."""

from __future__ import annotations

# A current, realistic desktop Chrome UA used when the caller does not supply one.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def cap_body(content: bytes, text: str, encoding: str | None, max_bytes: int | None) -> str:
    """Return the body text, honoring ``max_bytes`` as a transport guard.

    ``max_bytes`` bounds how much we hand downstream (a defense against multi-GB
    transfers); it is NOT a content/LLM cap and only triggers when a response actually
    exceeds it. Below the limit (the common case) the already-decoded ``text`` is used.
    """
    if max_bytes is None or len(content) <= max_bytes:
        return text
    return content[:max_bytes].decode(encoding or "utf-8", errors="replace")
