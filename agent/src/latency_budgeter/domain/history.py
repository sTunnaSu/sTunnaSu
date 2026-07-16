"""Point-in-time latency-history records and query contracts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from src.latency_budgeter.domain.json_values import canonical_json
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.domain.values import Milliseconds


class LatencyComponent(str, Enum):
    """Forecast components released by the later execution lifecycle."""

    DECISION = "decision"
    RISK_PROCESSING = "risk_processing"
    SUBMISSION = "submission"
    ACKNOWLEDGEMENT = "acknowledgement"
    # ``FILL`` is the Step 2 forecast component and therefore means latency
    # from actual submission to the first valid fill.  ``FINAL_FILL`` is kept
    # separately for Step 3 execution-quality analysis; Step 2 does not use it.
    FILL = "fill"
    FINAL_FILL = "final_fill"


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One immutable component measurement and its availability instant."""

    sample_id: str
    decision_id: str
    component: LatencyComponent
    value_ms: Milliseconds
    component_available_at: datetime
    recorded_at: datetime
    component_definition_version: str
    estimator_schema_version: str
    valid: bool = True
    invalid_reason: str = ""
    source_event_id: str = ""
    unit: str = "ms"
    order_id: str = ""

    def __post_init__(self) -> None:
        if not str(self.sample_id).strip() or not str(self.decision_id).strip():
            raise ValueError("sample_id and decision_id are required")
        object.__setattr__(self, "component", LatencyComponent(self.component))
        if not isinstance(self.value_ms, Milliseconds):
            object.__setattr__(self, "value_ms", Milliseconds(self.value_ms))
        object.__setattr__(self, "component_available_at", normalize_timestamp(self.component_available_at))
        object.__setattr__(self, "recorded_at", normalize_timestamp(self.recorded_at))
        if self.recorded_at < self.component_available_at:
            raise ValueError("recorded_at cannot precede component_available_at")
        if self.unit != "ms":
            raise ValueError("latency samples must use the explicit 'ms' unit")
        if not self.component_definition_version or not self.estimator_schema_version:
            raise ValueError("component and estimator versions are required")
        if self.valid and self.invalid_reason:
            raise ValueError("valid samples cannot carry invalid_reason")
        if not self.valid and not str(self.invalid_reason).strip():
            raise ValueError("invalid samples require invalid_reason")

    def to_dict(self) -> dict[str, Any]:
        """Return deterministic persistence content."""
        payload = {
            "sample_id": self.sample_id,
            "decision_id": self.decision_id,
            "component": self.component.value,
            "value_ms": self.value_ms.canonical(),
            "component_available_at": utc_iso(self.component_available_at),
            "recorded_at": utc_iso(self.recorded_at),
            "component_definition_version": self.component_definition_version,
            "estimator_schema_version": self.estimator_schema_version,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "source_event_id": self.source_event_id,
            "unit": self.unit,
        }
        # Preserve the exact v1 semantic fingerprint for migrated legacy rows.
        # Step 3 samples always carry an order ID and therefore include it.
        if self.order_id:
            payload["order_id"] = self.order_id
        return payload

    @property
    def semantic_fingerprint(self) -> str:
        """Return a stable identity for idempotent history inserts."""
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """Frozen strict-prior query parameters."""

    component: LatencyComponent
    decision_at: datetime
    current_decision_id: str
    rolling_window: int
    component_definition_version: str
    estimator_schema_version: str
    unit: str = "ms"

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", LatencyComponent(self.component))
        object.__setattr__(self, "decision_at", normalize_timestamp(self.decision_at))
        if self.rolling_window <= 0:
            raise ValueError("rolling_window must be positive")
        if not self.current_decision_id:
            raise ValueError("current_decision_id is required")
        if self.unit != "ms":
            raise ValueError("history query unit must be 'ms'")


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """Most-recent strict-prior samples in deterministic descending order."""

    samples: tuple[LatencySample, ...]

    @property
    def oldest_available_at(self) -> datetime | None:
        return min((sample.component_available_at for sample in self.samples), default=None)

    @property
    def newest_available_at(self) -> datetime | None:
        return max((sample.component_available_at for sample in self.samples), default=None)
