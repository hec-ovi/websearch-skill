"""Keyless GitHub repository search tool (github@1.0.0).

A standalone extra tool over the official unauthenticated GitHub REST search API.
Emits the cross-cutting Envelope (meta.layer "github").
"""

from __future__ import annotations

from .client import ENDPOINT, GithubTool, build_github_tool
from .models import (
    DEFAULT_PER_PAGE,
    GITHUB_CONTRACT_VERSION,
    MAX_PER_PAGE,
    GithubRepo,
    GithubSearchPayload,
    GithubSearchRequest,
)

__all__ = [
    "GITHUB_CONTRACT_VERSION",
    "DEFAULT_PER_PAGE",
    "MAX_PER_PAGE",
    "ENDPOINT",
    "GithubTool",
    "GithubRepo",
    "GithubSearchPayload",
    "GithubSearchRequest",
    "build_github_tool",
]
