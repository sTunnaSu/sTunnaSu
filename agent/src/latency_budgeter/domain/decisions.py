"""Immutable Step 2 decisions, forecasts, and approved opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from src.latency_budgeter.domain.history import LatencyComponent
from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.domain.values import BasisPoints, Milliseconds

STEP2_EVALUATION_VERSION = "phase8-step2-gate-v1"


class DecisionOutcome(str, Enum):
    REJECT = "REJECT"
    DEFER = "DEFER"
    ALLOW = "ALLOW"


class DecisionReason(str, Enum):
    REJECT_INVALID_TIMESTAMP_ORDER = "REJECT_INVALID_TIMESTAMP_ORDER"
    REJECT_STALE_DATA = "REJECT_STALE_DATA"
    REJECT_COMMON_COHORT_INELIGIBLE = "REJECT_COMMON_COHORT_INELIGIBLE"
    REJECT_NONPOSITIVE_GROSS_EDGE = "REJECT_NONPOSITIVE_GROSS_EDGE"
    DEFER_COLD_START = "DEFER_COLD_START"
    REJECT_INSUFFICIENT_NET_EDGE = "REJECT_INSUFFICIENT_NET_EDGE"
    ALLOW_NET_EDGE_ABOVE_BUFFER = "ALLOW_NET_EDGE_ABOVE_BUFFER"


class EstimatorMode(str, Enum):
    ROLLING_PRIOR_ONLY = "rolling_prior_only"
    COLD_START_FALLBACK = "cold_start_fallback"


class ComponentEstimateMode(str, Enum):
    DIRECT_OBSERVATION = "direct_observation"
    MEASURED_PRE_DECISION = "measured_pre_decision"
    ROLLING_PRIOR_ONLY = "rolling_prior_only"
    COLD_START_FALLBACK = "cold_start_fallback"
    UNSUPPORTED_BY_VENUE = "unsupported_by_venue"


class LiquidityRole(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True, slots=True)
class AvailableLatencyMeasurement:
    """A current pre-decision component with an explicit availability time."""

    component: LatencyComponent
    value_ms: Milliseconds
    available_at: datetime
    valid: bool = True
    component_definition_version: str = "phase8-latency-component-v1"
    unit: str = "ms"

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", LatencyComponent(self.component))
        if self.component not in {LatencyComponent.DECISION, LatencyComponent.RISK_PROCESSING}:
            raise ValueError("only decision and risk latency can be measured pre-decision")
        if not isinstance(self.value_ms, Milliseconds):
            object.__setattr__(self, "value_ms", Milliseconds(self.value_ms))
        object.__setattr__(self, "available_at", normalize_timestamp(self.available_at))
        if not self.component_definition_version:
            raise ValueError("component_definition_version is required")
        if self.unit != "ms":
            raise ValueError("pre-decision latency measurements must use 'ms'")


@dataclass(frozen=True, slots=True)
class PreDecisionLatencyMeasurements:
    decision: AvailableLatencyMeasurement | None = None
    risk_processing: AvailableLatencyMeasurement | None = None


@dataclass(frozen=True, slots=True)
class ComponentForecast:
    component: LatencyComponent
    value_ms: Milliseconds
    mode: ComponentEstimateMode
    prior_sample_count: int = 0
    window_oldest_available_at: datetime | None = None
    window_newest_available_at: datetime | None = None
    percentile: int | None = None
    measured_available_at: datetime | None = None
    excluded_current_measurement_reason: str = ""
    component_definition_version: str = ""
    unit: str = "ms"

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component.value,
            "value_ms": self.value_ms.to_float(),
            "mode": self.mode.value,
            "prior_sample_count": self.prior_sample_count,
            "window_oldest_available_at": (
                utc_iso(self.window_oldest_available_at) if self.window_oldest_available_at else None
            ),
            "window_newest_available_at": (
                utc_iso(self.window_newest_available_at) if self.window_newest_available_at else None
            ),
            "percentile": self.percentile,
            "measured_available_at": utc_iso(self.measured_available_at) if self.measured_available_at else None,
            "excluded_current_measurement_reason": self.excluded_current_measurement_reason,
            "component_definition_version": self.component_definition_version,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class LatencyForecast:
    data_age_ms: Milliseconds
    decision: ComponentForecast
    risk_processing: ComponentForecast
    submission: ComponentForecast
    acknowledgement: ComponentForecast
    fill: ComponentForecast
    total_ms: Milliseconds
    estimator_mode: EstimatorMode
    estimator_version: str

    @property
    def components(self) -> tuple[ComponentForecast, ...]:
        return (self.decision, self.risk_processing, self.submission, self.acknowledgement, self.fill)

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_age_ms": self.data_age_ms.to_float(),
            "forecast_decision_latency_ms": self.decision.value_ms.to_float(),
            "forecast_risk_latency_ms": self.risk_processing.value_ms.to_float(),
            "forecast_submission_latency_ms": self.submission.value_ms.to_float(),
            "forecast_ack_latency_ms": self.acknowledgement.value_ms.to_float(),
            "forecast_fill_latency_ms": self.fill.value_ms.to_float(),
            "forecast_total_latency_ms": self.total_ms.to_float(),
            "latency_estimator_mode": self.estimator_mode.value,
            "estimator_version": self.estimator_version,
            "component_estimates": [component.to_dict() for component in self.components],
        }


@dataclass(frozen=True, slots=True)
class ExecutionCostEstimate:
    fee_bps: BasisPoints
    spread_bps: BasisPoints
    slippage_bps: BasisPoints
    impact_bps: BasisPoints
    total_bps: BasisPoints
    liquidity_role: LiquidityRole
    side: SignalSide
    adverse_price_adjustment_bps: BasisPoints
    convention: str
    reference_price_convention: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_bps": self.fee_bps.to_float(),
            "spread_bps": self.spread_bps.to_float(),
            "slippage_bps": self.slippage_bps.to_float(),
            "impact_bps": self.impact_bps.to_float(),
            "estimated_cost_bps": self.total_bps.to_float(),
            "liquidity_role": self.liquidity_role.value,
            "side": self.side.value,
            "adverse_price_adjustment_bps": self.adverse_price_adjustment_bps.to_float(),
            "cost_convention": self.convention,
            "reference_price_convention": self.reference_price_convention,
        }


@dataclass(frozen=True, slots=True)
class DecisionEconomics:
    gross_edge_bps: BasisPoints
    latency_forecast: LatencyForecast
    tau_ms: Milliseconds
    predicted_decay_factor: float
    edge_at_fill_bps: BasisPoints
    costs: ExecutionCostEstimate
    net_edge_bps: BasisPoints
    required_buffer_bps: BasisPoints
    config_version: str
    config_fingerprint: str
    evaluation_version: str = STEP2_EVALUATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "gross_edge_bps": self.gross_edge_bps.to_float(),
            **self.latency_forecast.to_dict(),
            "tau_ms": self.tau_ms.to_float(),
            "predicted_decay_factor": self.predicted_decay_factor,
            "edge_at_fill_bps": self.edge_at_fill_bps.to_float(),
            **self.costs.to_dict(),
            "net_edge_bps": self.net_edge_bps.to_float(),
            "required_buffer_bps": self.required_buffer_bps.to_float(),
            "config_version": self.config_version,
            "config_fingerprint": self.config_fingerprint,
            "evaluation_version": self.evaluation_version,
            "numeric_precision": {"basis_points": "1e-9", "milliseconds": "0.001"},
        }


@dataclass(frozen=True, slots=True)
class ApprovedOpportunity:
    """Immutable ALLOW-only handoff; it deliberately has no submit method."""

    decision_id: str
    run_id: str
    signal_id: str
    symbol: str
    side: SignalSide
    decision_at: datetime
    economics: DecisionEconomics

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", SignalSide(self.side))
        object.__setattr__(self, "decision_at", normalize_timestamp(self.decision_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "decision_at": utc_iso(self.decision_at),
            "economics": self.economics.to_dict(),
        }
