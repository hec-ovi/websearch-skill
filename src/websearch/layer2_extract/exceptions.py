"""Controlled exceptions for Layer 2A, mapped to error Envelopes by the pipeline."""

from __future__ import annotations


class DependencyMissing(Exception):
    """A required optional dependency (trafilatura, curl_cffi) is not installed."""

    def __init__(self, package: str, hint: str = ""):
        self.package = package
        self.hint = hint
        msg = f"required dependency '{package}' is not installed"
        if hint:
            msg += f" ({hint})"
        super().__init__(msg)
