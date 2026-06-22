"""Stable, opaque document/passage ids.

A document id is a deterministic function of the canonical URL, so the same URL always
maps to the same id across the FORMAT and STORE ports (the resolver can therefore hand a
``format`` id straight to ``store.get``). Ids are opaque: a consumer must not parse them.
"""

from __future__ import annotations

import hashlib

_DOC_PREFIX = "doc_"


def doc_id(url: str) -> str:
    """Deterministic opaque id for a URL."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{_DOC_PREFIX}{digest}"


def passage_id(document_id: str, ordinal: int) -> str:
    """Stable id for a passage within a document."""
    return f"{document_id}_p{ordinal}"


def site_of(url: str) -> str | None:
    """Display host for a URL (no scheme/www), or None if unparseable."""
    from urllib.parse import urlsplit

    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    return host[4:] if host.startswith("www.") else host
