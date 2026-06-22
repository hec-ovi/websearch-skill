"""Controlled exceptions for Layer 2B, mapped to error Envelopes by the caller."""

from __future__ import annotations


class DependencyMissing(Exception):
    """An opt-in Layer 2B backend (a vector or Rust page index) is not installed."""
