"""The cross-cutting Envelope.

Every inter-layer message and CLI ``--json`` output is an Envelope. The model
mirrors ``contracts/envelope.schema.json`` (envelope@1.0.0). ``data`` is held as a
plain JSON-able value (dict/list/None) so a producer serializes its own payload
model first and the Envelope stays layer-agnostic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

ENVELOPE_CONTRACT_VERSION = "1.0.0"


class EnvelopeError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retriable: bool


class Meta(BaseModel):
    # Free-form by contract: adding keys is never breaking.
    model_config = ConfigDict(extra="allow")

    layer: str
    backend: str | None = None
    elapsed_ms: float = 0.0
    trace_id: str | None = None


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_version: str
    ok: bool
    data: Any = None
    error: EnvelopeError | None = None
    meta: Meta


def ok_envelope(
    contract_version: str,
    data: Any,
    *,
    layer: str,
    backend: str | None = None,
    elapsed_ms: float = 0.0,
    trace_id: str | None = None,
    **meta_extra: Any,
) -> Envelope:
    """Build a success Envelope. ``data`` must already be JSON-able."""
    return Envelope(
        contract_version=contract_version,
        ok=True,
        data=data,
        error=None,
        meta=Meta(
            layer=layer,
            backend=backend,
            elapsed_ms=elapsed_ms,
            trace_id=trace_id,
            **meta_extra,
        ),
    )


def error_envelope(
    contract_version: str,
    *,
    code: str,
    message: str,
    retriable: bool,
    layer: str,
    backend: str | None = None,
    elapsed_ms: float = 0.0,
    trace_id: str | None = None,
    **meta_extra: Any,
) -> Envelope:
    """Build a failure Envelope (``ok`` false, ``data`` null)."""
    return Envelope(
        contract_version=contract_version,
        ok=False,
        data=None,
        error=EnvelopeError(code=code, message=message, retriable=retriable),
        meta=Meta(
            layer=layer,
            backend=backend,
            elapsed_ms=elapsed_ms,
            trace_id=trace_id,
            **meta_extra,
        ),
    )
