"""Stable machine error codes used in Envelope.error.code across layers."""

from __future__ import annotations

# Cross-cutting
INVALID_REQUEST = "invalid_request"
DEPENDENCY_MISSING = "dependency_missing"

# Layer 1 (search)
ALL_ENGINES_FAILED = "all_engines_failed"
NO_ENGINES_ENABLED = "no_engines_enabled"

# Layer 2A (fetch + extract)
FETCH_FAILED = "fetch_failed"
EXTRACT_FAILED = "extract_failed"

# Layer 3 (agent I/O)
NOT_OPENED = "not_opened"
