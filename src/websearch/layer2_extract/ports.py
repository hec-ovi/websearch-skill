"""The two Layer 2A capability ports: FETCH and EXTRACT.

Both are decoupled (per the layer2-fetch-extraction finding): a fetcher knows
nothing about extraction and vice versa. Adapters are plugins behind these ports;
the default closure is ``HttpxFetcher`` + ``CurlCffiFetcher`` (FETCH) and
``TrafilaturaExtractor`` (EXTRACT). Browser/stealth fetchers and neural extractors
are opt-in adapters that implement the same ports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ExtractRequest, ExtractResult, FetchRequest, FetchResult


class FetchAdapter(ABC):
    """A single fetch tier (URL in, raw content out)."""

    #: Stable adapter id, e.g. "httpx", "curl_cffi".
    name: str = "fetch"
    #: The fetched_via enum value this adapter reports.
    fetched_via: str = "http"
    #: Tier class for tier_hint filtering: "http" | "browser" | "stealth".
    tier_class: str = "http"
    #: Ascending escalation order within the eligible set (cheap first).
    escalation_order: int = 0

    @abstractmethod
    def fetch(self, request: FetchRequest) -> FetchResult:
        """Fetch ``request.url``. Must return a FetchResult, never raise."""

    def available(self) -> bool:
        """Whether this adapter's dependencies are importable / usable."""
        return True


class ExtractAdapter(ABC):
    """A single extraction engine (raw HTML in, clean content out)."""

    #: Stable adapter id, e.g. "trafilatura".
    name: str = "extract"

    @abstractmethod
    def extract(self, request: ExtractRequest) -> ExtractResult:
        """Extract clean content from ``request.html``. Must not raise."""

    def available(self) -> bool:
        return True
