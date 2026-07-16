"""Frozen Phase 8 Step 4 outcome-evaluation records.

The records in this module carry evidence into Step 4.  They do not obtain
prices, place orders, mutate forecasts, or decide whether a feature ships.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from src.latency_budgeter.domain.json_values import canonical_json
from src.latency_budgeter.domain.lifecycle import exact_decimal
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso

STEP4_OUTCOME_EVENT_VERSION = "phase8-step4-outcome-v1"
STEP4_OUTCOME_POLICY_VERSION = "phase8-step4-outcome-policy-v1"


class OutcomeTriggerType(str, Enum):
    """The two frozen paths that may release a strategy outcome."""

    FIXED_HORIZON = "FIXED_HORIZON"
    ACTUAL_STRATEGY_EXIT = "ACTUAL_STRATEGY_EXIT"


class OutcomeType(str, Enum):
    """Semantically disjoint outcome families."""

    REALISED_EXECUTED = "REALISED_EXECUTED"
    NO_REALISED_EXECUTION = "NO_REALISED_EXECUTION"
    SIMULATED_DIAGNOSTIC = "SIMULATED_DIAGNOSTIC"


class CounterfactualType(str, Enum):
    """Explicit diagnostic labels; ``NONE`` means actual evidence."""

    NONE = "NONE"
    REJECTED_SIGNAL = "rejected_signal_counterfactual"
    APPROVED_UNFILLED = "approved_unfilled_counterfactual"


@dataclass(frozen=True, slots=True)
class Step4OutcomePolicy:
    """Pre-registered methodology layered on the per-decision frozen config."""

    preregistered_at: datetime
    methodology_version: str = STEP4_OUTCOME_EVENT_VERSION
    horizon_version: str = "phase8-outcome-horizon-v1"
    policy_version: str = STEP4_OUTCOME_POLICY_VERSION
    trigger_policy: str = "fixed_horizon_or_actual_exit"
    quantity_basis: str = "actual_executed_quantity"
    equal_opportunity_weighting: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "preregistered_at", normalize_timestamp(self.preregistered_at))
        if self.policy_version != STEP4_OUTCOME_POLICY_VERSION:
            raise ValueError(f"unsupported Step 4 outcome policy: {self.policy_version!r}")
        for value, label in (
            (self.methodology_version, "methodology_version"),
            (self.horizon_version, "horizon_version"),
            (self.trigger_policy, "trigger_policy"),
            (self.quantity_basis, "quantity_basis"),
        ):
            if not str(value).strip():
                raise ValueError(f"{label} is required")
        if self.trigger_policy != "fixed_horizon_or_actual_exit":
            raise ValueError("Step 4 v1 supports only fixed_horizon_or_actual_exit")
        if self.quantity_basis != "actual_executed_quantity":
            raise ValueError("Step 4 v1 requires actual_executed_quantity")
        if not self.equal_opportunity_weighting:
            raise ValueError("Step 4 v1 primary expectancy is equal-opportunity weighted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "preregistered_at": utc_iso(self.preregistered_at),
            "methodology_version": self.methodology_version,
            "horizon_version": self.horizon_version,
            "trigger_policy": self.trigger_policy,
            "quantity_basis": self.quantity_basis,
            "equal_opportunity_weighting": self.equal_opportunity_weighting,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReferencePriceEvidence:
    """Point-in-time price selected under a declared frozen convention."""

    symbol: str
    price: Decimal
    observed_at: datetime
    source_capture_at: datetime
    target_at: datetime
    reference_convention: str
    provider: str
    feed: str
    dataset_version: str
    source_event_id: str
    query_fingerprint: str
    bars_elapsed: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", exact_decimal(self.price, label="reference price", allow_zero=False))
        for field_name in ("observed_at", "source_capture_at", "target_at"):
            object.__setattr__(self, field_name, normalize_timestamp(getattr(self, field_name)))
        if self.source_capture_at < self.observed_at:
            raise ValueError("source_capture_at cannot precede the represented price timestamp")
        if self.bars_elapsed is not None and self.bars_elapsed < 0:
            raise ValueError("bars_elapsed cannot be negative")
        required = (
            self.symbol,
            self.reference_convention,
            self.provider,
            self.feed,
            self.dataset_version,
            self.source_event_id,
            self.query_fingerprint,
        )
        if any(not str(value).strip() for value in required):
            raise ValueError("reference-price identity and provenance fields are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": format(self.price, "f"),
            "observed_at": utc_iso(self.observed_at),
            "source_capture_at": utc_iso(self.source_capture_at),
            "target_at": utc_iso(self.target_at),
            "reference_convention": self.reference_convention,
            "provider": self.provider,
            "feed": self.feed,
            "dataset_version": self.dataset_version,
            "source_event_id": self.source_event_id,
            "query_fingerprint": self.query_fingerprint,
            "bars_elapsed": self.bars_elapsed,
        }


@dataclass(frozen=True, slots=True)
class OutcomeTrigger:
    """One scheduler or actual-exit trigger carrying immutable evidence."""

    trigger_id: str
    trigger_type: OutcomeTriggerType
    triggered_at: datetime
    reference_price: ReferencePriceEvidence
    actual_exit_id: str | None = None
    actual_exit_quantity: Decimal | None = None
    actual_exit_cost: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "trigger_type", OutcomeTriggerType(self.trigger_type))
        object.__setattr__(self, "triggered_at", normalize_timestamp(self.triggered_at))
        if not self.trigger_id:
            raise ValueError("trigger_id is required")
        if self.triggered_at < self.reference_price.source_capture_at:
            raise ValueError("triggered_at cannot precede availability of reference evidence")
        if self.actual_exit_quantity is not None:
            object.__setattr__(
                self,
                "actual_exit_quantity",
                exact_decimal(self.actual_exit_quantity, label="actual exit quantity", allow_zero=False),
            )
        if self.actual_exit_cost is not None:
            object.__setattr__(
                self,
                "actual_exit_cost",
                exact_decimal(self.actual_exit_cost, label="actual exit cost"),
            )
        if self.trigger_type is OutcomeTriggerType.ACTUAL_STRATEGY_EXIT:
            if not self.actual_exit_id or self.actual_exit_quantity is None:
                raise ValueError("actual exit triggers require an exit ID and actual quantity")
        elif any(
            value is not None
            for value in (self.actual_exit_id, self.actual_exit_quantity, self.actual_exit_cost)
        ):
            raise ValueError("fixed-horizon triggers cannot contain actual-exit fields")


@dataclass(frozen=True, slots=True)
class OutcomeCallbackResult:
    """Result of an idempotent immutable Step 4 append."""

    event_id: str
    appended: bool
    aggregate_version: int
    outcome_type: OutcomeType
    counterfactual_type: CounterfactualType
