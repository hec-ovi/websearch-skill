"""Doctor models, mirroring ``contracts/doctor.schema.json`` (doctor@1.0.0)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DOCTOR_CONTRACT_VERSION = "2.0.0"

Status = Literal["ok", "warn", "fail", "skipped"]
Group = Literal["runtime", "egress", "vpn", "searxng", "engines", "tools", "fetch"]

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIPPED = "skipped"

# The order groups are printed in: what everything else depends on first, so the first
# failure you read is usually the cause of the ones under it.
GROUP_ORDER: tuple[Group, ...] = (
    "runtime",
    "egress",
    "vpn",
    "searxng",
    "engines",
    "tools",
    "fetch",
)

DEFAULT_QUERY = "rust ownership"
# Small, stable, and deliberately free of anti-bot: a fetch-tier probe should fail only
# when the tier itself is broken.
DEFAULT_FETCH_URL = "https://example.com"
DEFAULT_TIMEOUT_MS = 15000


class DoctorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checks: list[str] | None = None
    quick: bool = False
    # Off by default: the direct baseline is the only request in the tool that
    # deliberately leaves the egress proxy.
    baseline: bool = False
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=1000, le=120000)
    query: str = Field(default=DEFAULT_QUERY, min_length=1)
    fetch_url: str = Field(default=DEFAULT_FETCH_URL, min_length=1)

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0


class OptionalLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["vpn", "proxy", "searxng"]
    enabled: bool
    source: str | None = None
    value: str | None = None
    detail: str
    error: str | None = None


class CheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    group: Group
    status: Status
    summary: str
    detail: dict[str, Any] = Field(default_factory=dict)
    hint: str | None = None
    elapsed_ms: float = 0.0
    optional_layer: str | None = None


class DoctorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: int = 0
    warn: int = 0
    fail: int = 0
    skipped: int = 0
    total: int = 0


class DoctorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_at: str
    layers: dict[str, OptionalLayer]
    checks: list[CheckResult]
    summary: DoctorSummary
    warnings: list[str] = Field(default_factory=list)
