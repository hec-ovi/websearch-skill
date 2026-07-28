"""Ahmia adapter: the keyless index of Tor onion services.

Ahmia crawls onion services and answers over ordinary HTTP, which is what makes onion
search possible without an account or a key. Two things about it shape this adapter:

- Its search form carries a hidden field whose name and value change over time, and a
  request without it is redirected back to the home page. So a search is two requests:
  read the form, then query with the field it gave us. The pair is cached per adapter.
- Result links are wrapped in ``/search/redirect?...&redirect_url=<onion>``. The wrapper
  is Ahmia's click tracking; the ``redirect_url`` parameter is the address you actually
  want, so it is unwrapped here rather than handed downstream.

The whole thing goes through the Tor SOCKS proxy. Onion results you cannot open are not
results, so this adapter stays disabled unless the tor layer gave it a proxy, and the
query itself takes the same path as the pages it will find.
"""

from __future__ import annotations

import os
import re
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..capability import ONION_INDEX
from ..models import FreshnessRange, SearchRequest
from ..port import EngineAdapter, EngineOutput, RawResult

AHMIA_URL_VAR = "WEBSEARCH_AHMIA_URL"
DEFAULT_BASE_URL = "https://ahmia.fi"
# Ahmia's own onion service, for a run that should not touch the clearnet name at all.
# Slower and less reliable than the clearnet host reached over Tor, so not the default.
ONION_BASE_URL = "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion"

# Ahmia's date filter, in days. It offers a week and a month; a day and a year are mapped
# to the nearest thing it can actually do rather than silently ignored.
_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30}

_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) websearch-skill"

_HIDDEN_FIELD = re.compile(
    r"<input[^>]*type=\"hidden\"[^>]*name=\"([^\"]+)\"[^>]*value=\"([^\"]*)\"", re.IGNORECASE
)


def base_url() -> str:
    return (os.environ.get(AHMIA_URL_VAR) or DEFAULT_BASE_URL).rstrip("/")


def unwrap(href: str) -> str:
    """The onion address behind an Ahmia redirect link, or the href when it is direct."""
    if "redirect_url=" not in href:
        return href
    query = urlsplit(href).query
    values = parse_qs(query, keep_blank_values=True).get("redirect_url") or []
    return values[0] if values else href


class _ResultParser(HTMLParser):
    """Pull ``<li class="result">`` blocks out of an Ahmia result page.

    stdlib rather than a parser dependency: the shape needed here is three tags deep and
    the tool's dependency closure is worth more than the last few percent of tolerance.
    Anything unrecognized is skipped rather than raised on, so a markup change costs
    results instead of the whole search.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        classes = attr.get("class", "").split()
        if tag == "li" and "result" in classes:
            self._current = {"url": "", "title": "", "snippet": "", "cite": "", "seen": ""}
            return
        if self._current is None:
            return
        if tag == "a" and not self._current["url"] and attr.get("href"):
            self._current["url"] = unwrap(unescape(attr["href"]))
            self._field = "title"
        elif tag == "p" and not self._current["snippet"]:
            self._field = "snippet"
        elif tag == "cite":
            self._field = "cite"
        elif tag == "span" and "lastSeen" in classes:
            self._current["seen"] = attr.get("data-timestamp", "")
            self._field = None

    def handle_endtag(self, tag: str) -> None:
        if tag == "li" and self._current is not None:
            if self._current["url"]:
                self.results.append(self._current)
            self._current = None
            self._field = None
        elif tag in ("a", "p", "cite"):
            self._field = None

    def handle_data(self, data: str) -> None:
        if self._current is None or self._field is None:
            return
        self._current[self._field] = (self._current[self._field] + " " + data.strip()).strip()


class AhmiaAdapter(EngineAdapter):
    """Onion search through Ahmia. Disabled unless a Tor proxy was handed to it."""

    name = "ahmia"
    correlation_group = ONION_INDEX

    def __init__(
        self,
        *,
        proxy: str | None = None,
        base_url_override: str | None = None,
        client: httpx.Client | None = None,
    ):
        self.base_url = (base_url_override or base_url()).rstrip("/")
        self._proxy = proxy
        self._client = client
        self._owned_client: httpx.Client | None = None
        self._form_field: tuple[str, str] | None = None

    def enabled(self) -> bool:
        # An injected client owns its transport, which is how the tests reach this
        # adapter without a Tor running.
        return bool(self._proxy) or self._client is not None

    def _get_client(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if self._owned_client is None:
            self._owned_client = httpx.Client(
                follow_redirects=True,
                headers={"User-Agent": _USER_AGENT},
                proxy=self._proxy,
            )
        return self._owned_client

    def _form_token(self, client: httpx.Client, timeout: float | None) -> tuple[str, str] | None:
        """The hidden form field, read once per adapter and reused."""
        if self._form_field is not None:
            return self._form_field
        kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        resp = client.get(f"{self.base_url}/", **kwargs)
        resp.raise_for_status()
        match = _HIDDEN_FIELD.search(resp.text)
        if not match:
            return None
        self._form_field = (match.group(1), match.group(2))
        return self._form_field

    def _params(self, request: SearchRequest, token: tuple[str, str] | None) -> dict[str, Any]:
        params: dict[str, Any] = {"q": request.query}
        if token:
            params[token[0]] = token[1]
        if not isinstance(request.freshness, FreshnessRange):
            days = _FRESHNESS_DAYS.get(request.freshness)
            if days:
                params["d"] = days
        return params

    def search(self, request: SearchRequest) -> EngineOutput:
        if not self.enabled():
            return EngineOutput(
                engine=self.name,
                error="ahmia needs the tor layer: start it with `websearch tor up`",
            )
        client = self._get_client()
        timeout = None if self._client is not None else request.timeout_ms / 1000.0
        kwargs: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        try:
            token = self._form_token(client, timeout)
            params = self._params(request, token)
            resp = client.get(f"{self.base_url}/search/", params=params, **kwargs)
            resp.raise_for_status()
            if not token and "searchResults" not in resp.text:
                # The form field could not be read and the answer came back without
                # results: say which of the two failed instead of reporting "no results".
                return EngineOutput(
                    engine=self.name,
                    error="ahmia did not return its search form field, and the query it "
                    "answered without one carried no results",
                )
            parser = _ResultParser()
            parser.feed(resp.text)
            return EngineOutput(engine=self.name, results=self._shape(parser.results))
        except Exception as exc:
            return EngineOutput(engine=self.name, error=f"{type(exc).__name__}: {exc}")

    def _shape(self, rows: list[dict[str, str]]) -> list[RawResult]:
        results: list[RawResult] = []
        for i, row in enumerate(rows):
            url = row["url"]
            if not url.startswith(("http://", "https://")):
                continue  # a relative link is Ahmia's own navigation, not a result
            results.append(
                RawResult(
                    url=url,
                    title=row["title"] or row["cite"],
                    snippet=row["snippet"],
                    rank=i + 1,
                    published_date=row["seen"] or None,
                )
            )
        return results
