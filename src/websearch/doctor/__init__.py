"""Doctor: a full self-test of one installation (doctor@1.0.0).

Answers, per capability and independently: does this machine reach the internet, is the
declared VPN actually up, does the egress proxy move traffic, is the self-hosted SearXNG
serving JSON, does each search engine still answer, do the keyless tools work, do both
fetch tiers work, and does the MCP face register its tools.

The three optional layers (VPN, egress proxy, SearXNG) are off unless configured, and a
layer that is off reports ``skipped``, never a failure. Emits the cross-cutting Envelope
(meta.layer "doctor").
"""

from __future__ import annotations

from .models import (
    DEFAULT_FETCH_URL,
    DEFAULT_QUERY,
    DEFAULT_TIMEOUT_MS,
    DOCTOR_CONTRACT_VERSION,
    FAIL,
    GROUP_ORDER,
    OK,
    SKIPPED,
    WARN,
    CheckResult,
    DoctorPayload,
    DoctorRequest,
    DoctorSummary,
    OptionalLayer,
)
from .probes import HttpxNet, Net, Outcome
from .runner import Doctor, build_doctor

__all__ = [
    "DOCTOR_CONTRACT_VERSION",
    "DEFAULT_QUERY",
    "DEFAULT_FETCH_URL",
    "DEFAULT_TIMEOUT_MS",
    "GROUP_ORDER",
    "OK",
    "WARN",
    "FAIL",
    "SKIPPED",
    "DoctorRequest",
    "DoctorPayload",
    "DoctorSummary",
    "CheckResult",
    "OptionalLayer",
    "Doctor",
    "build_doctor",
    "Net",
    "HttpxNet",
    "Outcome",
]
