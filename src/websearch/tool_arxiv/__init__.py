"""Keyless arXiv search tool (arxiv@1.0.0).

A standalone extra tool over the official export.arxiv.org Atom API. Emits the
cross-cutting Envelope (meta.layer "arxiv").
"""

from __future__ import annotations

from .client import ENDPOINT, ArxivTool, build_arxiv_tool
from .models import (
    ARXIV_CONTRACT_VERSION,
    DEFAULT_MAX_RESULTS,
    MAX_MAX_RESULTS,
    ArxivPaper,
    ArxivSearchPayload,
    ArxivSearchRequest,
)

__all__ = [
    "ARXIV_CONTRACT_VERSION",
    "DEFAULT_MAX_RESULTS",
    "MAX_MAX_RESULTS",
    "ENDPOINT",
    "ArxivTool",
    "ArxivPaper",
    "ArxivSearchPayload",
    "ArxivSearchRequest",
    "build_arxiv_tool",
]
