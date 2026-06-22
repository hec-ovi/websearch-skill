"""Stable machine error codes used in Envelope.error.code across layers."""

from __future__ import annotations

# Cross-cutting
INVALID_REQUEST = "invalid_request"
DEPENDENCY_MISSING = "dependency_missing"
# An unexpected failure that escaped a lower layer (store I/O, a bug). Always surfaced as a
# clean Envelope rather than a raw traceback so an agent/CLI never sees a stack trace.
INTERNAL_ERROR = "internal_error"

# Layer 1 (search)
ALL_ENGINES_FAILED = "all_engines_failed"
NO_ENGINES_ENABLED = "no_engines_enabled"

# Layer 2A (fetch + extract)
FETCH_FAILED = "fetch_failed"
EXTRACT_FAILED = "extract_failed"

# Layer 3 (agent I/O)
NOT_OPENED = "not_opened"

# Extra keyless tools (arxiv, github)
UPSTREAM_ERROR = "upstream_error"
RATE_LIMITED = "rate_limited"
