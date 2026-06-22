"""Keyless GitHub repository search over the official unauthenticated REST API.

No token. Unauthenticated repo search is allowed but capped at ~10 requests per
minute. A rate limit (HTTP 429, or a 403 with x-ratelimit-remaining 0 or a Retry-After)
returns a clean ``rate_limited`` Envelope instead of retrying (retrying inside the same
minute would not help); a 403 for any other reason (blocked access, UA rejection)
returns a non-retriable ``upstream_error`` carrying GitHub's own message. Code search is
auth-only and intentionally not offered here. The HTTP boundary is injectable for tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..envelope import Envelope, error_envelope, ok_envelope
from ..errors import RATE_LIMITED, UPSTREAM_ERROR
from .models import (
    GITHUB_CONTRACT_VERSION,
    GithubRepo,
    GithubSearchPayload,
    GithubSearchRequest,
)

ENDPOINT = "https://api.github.com/search/repositories"
USER_AGENT = "websearch-skill (+https://github.com/hec-ovi/websearch-skill)"
_LAYER = "github"
_BACKEND = "github-api"

HttpGet = Callable[..., Any]


def _httpx_get(url: str, *, params: dict, headers: dict, timeout_s: float) -> Any:
    import httpx

    return httpx.get(url, params=params, headers=headers, timeout=timeout_s, follow_redirects=True)


def _header(resp: Any, name: str) -> str | None:
    headers = getattr(resp, "headers", {}) or {}
    try:
        val = headers.get(name)
        if val is not None:
            return str(val)
    except AttributeError:
        pass
    for key, val in dict(headers).items():
        if key.lower() == name.lower():
            return str(val)
    return None


def _int(value: Any) -> int:
    """Coerce a count to int, defaulting to 0 (a malformed item must not crash search)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _forbidden_message(resp: Any) -> str:
    """A non-rate-limit 403 message, surfacing GitHub's own body message when present."""
    body_msg = ""
    try:
        body = json.loads(getattr(resp, "text", "") or "{}")
        if isinstance(body, dict):
            body_msg = body.get("message") or ""
    except json.JSONDecodeError:
        pass
    return f"GitHub returned HTTP 403 (not a rate limit){': ' + body_msg if body_msg else '.'}"


def _parse_item(it: Any) -> GithubRepo | None:
    if not isinstance(it, dict):
        return None  # a swapped/misbehaving upstream could emit a non-object item; drop it
    full_name = it.get("full_name")
    html_url = it.get("html_url")
    if not (full_name and html_url):
        return None
    owner = (it.get("owner") or {}).get("login")
    lic = it.get("license") or {}
    license_id = lic.get("spdx_id") if isinstance(lic, dict) else None
    if license_id in (None, "NOASSERTION"):
        license_id = None
    return GithubRepo(
        full_name=full_name,
        html_url=html_url,
        description=it.get("description"),
        stars=_int(it.get("stargazers_count")),
        forks=_int(it.get("forks_count")),
        open_issues=_int(it.get("open_issues_count")),
        language=it.get("language"),
        topics=list(it.get("topics") or []),
        owner=owner,
        updated_at=it.get("updated_at"),
        pushed_at=it.get("pushed_at"),
        license=license_id,
    )


class GithubTool:
    """The GitHub repository-search port. Swap ``http_get`` to retarget the transport."""

    def __init__(self, *, http_get: HttpGet, timeout_s: float = 15.0):
        self._http_get = http_get
        self._timeout_s = timeout_s

    def _error(self, *, code: str, message: str, retriable: bool, elapsed_ms: float) -> Envelope:
        return error_envelope(
            GITHUB_CONTRACT_VERSION,
            code=code,
            message=message,
            retriable=retriable,
            layer=_LAYER,
            backend=_BACKEND,
            elapsed_ms=elapsed_ms,
        )

    def search(self, request: GithubSearchRequest) -> Envelope:
        params: dict[str, Any] = {
            "q": request.q(),
            "order": request.order,
            "per_page": request.per_page,
        }
        if request.sort != "best-match":
            params["sort"] = request.sort  # omit => GitHub best-match relevance
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,  # GitHub rejects requests with no User-Agent
        }
        t0 = time.perf_counter()
        try:
            resp = self._http_get(
                ENDPOINT, params=params, headers=headers, timeout_s=self._timeout_s
            )
        except Exception as exc:
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"GitHub request failed: {type(exc).__name__}: {exc}",
                retriable=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        status = int(getattr(resp, "status_code", 0))
        elapsed = (time.perf_counter() - t0) * 1000
        if self._is_rate_limited(resp, status):
            return self._rate_limited(resp, status, elapsed)
        if status == 403:
            # A 403 that is not a rate limit (blocked access, UA rejection, secret
            # scanning, etc.): non-retriable, surface GitHub's own message.
            return self._error(
                code=UPSTREAM_ERROR,
                message=_forbidden_message(resp),
                retriable=False,
                elapsed_ms=elapsed,
            )
        if status == 422:
            return self._error(
                code=UPSTREAM_ERROR,
                message="GitHub rejected the query (HTTP 422); check the search qualifiers.",
                retriable=False,
                elapsed_ms=elapsed,
            )
        if status >= 400:
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"GitHub returned HTTP {status}.",
                retriable=status >= 500,
                elapsed_ms=elapsed,
            )

        try:
            body = json.loads(getattr(resp, "text", "") or "{}")
        except json.JSONDecodeError as exc:
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"GitHub response was not valid JSON: {exc}",
                retriable=True,
                elapsed_ms=elapsed,
            )
        if not isinstance(body, dict):
            return self._error(
                code=UPSTREAM_ERROR,
                message="GitHub response was not a JSON object.",
                retriable=True,
                elapsed_ms=elapsed,
            )

        # Shaping a mistyped upstream/mirror field (or items[] of the wrong type) must yield
        # a clean error Envelope, never a raw traceback out of search().
        try:
            items = body.get("items")
            repos = [r for it in (items or []) if (r := _parse_item(it))]
            incomplete = bool(body.get("incomplete_results", False))
            warnings = (
                [
                    "GitHub returned incomplete_results: the search index timed out, results "
                    "may be partial."
                ]
                if incomplete
                else []
            )
            payload = GithubSearchPayload(
                query=params["q"],
                total_count=body.get("total_count"),
                incomplete_results=incomplete,
                repos=repos,
                warnings=warnings,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            return self._error(
                code=UPSTREAM_ERROR,
                message=f"GitHub response had an unexpected shape: {type(exc).__name__}: {exc}",
                retriable=True,
                elapsed_ms=elapsed,
            )
        return ok_envelope(
            GITHUB_CONTRACT_VERSION,
            payload.model_dump(mode="json"),
            layer=_LAYER,
            backend=_BACKEND,
            elapsed_ms=elapsed,
            endpoint=ENDPOINT,
        )

    def _is_rate_limited(self, resp: Any, status: int) -> bool:
        """A 429 is always a (secondary) limit. A 403 is a rate limit only when GitHub
        signals it: x-ratelimit-remaining is 0 (primary) or a Retry-After is present
        (secondary). Other 403s are real forbidden errors, not rate limits."""
        if status == 429:
            return True
        if status != 403:
            return False
        remaining = _header(resp, "x-ratelimit-remaining")
        if remaining is not None and remaining.strip() == "0":
            return True
        return _header(resp, "retry-after") is not None

    def _rate_limited(self, resp: Any, status: int, elapsed_ms: float) -> Envelope:
        retry_after = _header(resp, "retry-after")
        reset = _header(resp, "x-ratelimit-reset")
        secs: int | None = None
        if retry_after and retry_after.strip().isdigit():
            secs = int(retry_after.strip())
        elif reset and reset.strip().isdigit():
            secs = max(0, int(reset.strip()) - int(time.time()))
        suffix = f"; retry in ~{secs}s" if secs is not None else ""
        return self._error(
            code=RATE_LIMITED,
            message=(
                f"GitHub search rate limit reached (HTTP {status}; unauthenticated search "
                f"allows about 10 requests/min){suffix}."
            ),
            retriable=True,
            elapsed_ms=elapsed_ms,
        )


def build_github_tool(*, http_get: HttpGet | None = None, timeout_s: float = 15.0) -> GithubTool:
    return GithubTool(http_get=http_get or _httpx_get, timeout_s=timeout_s)
