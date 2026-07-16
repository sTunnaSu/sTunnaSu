"""Immutable, versioned configuration for all Phase 8 stages."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.latency_budgeter.domain.errors import UnsupportedSchemaVersionError

PHASE8_CONFIG_VERSION = "phase8-step1-v1"


class _FrozenConfig(BaseModel):
    """Strict and deeply model-frozen configuration base."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CostAssumptions(_FrozenConfig):
    """Declared Phase 6/7-aligned cost components.

    Each configured component is an adverse, non-negative deduction from
    expected edge.  ``maker_fee_bps`` and ``taker_fee_bps`` are alternatives;
    the decision-time liquidity role selects exactly one of them.
    """

    maker_fee_bps: float = Field(default=0.0, ge=0.0)
    taker_fee_bps: float = Field(default=0.0, ge=0.0)
    spread_bps: float = Field(default=0.0, ge=0.0)
    slippage_bps: float = Field(default=0.0, ge=0.0)
    impact_bps: float = Field(default=0.0, ge=0.0)
    convention: Literal["one_way_components", "round_trip_components"] = "one_way_components"
    reference_price_convention: Literal["decision_reference_price"] = "decision_reference_price"


class ExpiryPolicy(_FrozenConfig):
    """Frozen future expiry semantics; no Step 1 enforcement occurs."""

    mode: Literal["none", "latency_budget", "fixed_ms"] = "none"
    fixed_expiry_ms: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_fixed_expiry(self) -> "ExpiryPolicy":
        """Require a duration only for the fixed policy."""
        if self.mode == "fixed_ms" and self.fixed_expiry_ms is None:
            raise ValueError("fixed_expiry_ms is required when expiry mode is fixed_ms")
        if self.mode != "fixed_ms" and self.fixed_expiry_ms is not None:
            raise ValueError("fixed_expiry_ms is only valid when expiry mode is fixed_ms")
        return self


class OutcomeHorizon(_FrozenConfig):
    """Frozen outcome-evaluation horizon for Step 4 projections."""

    value: int = Field(default=1, gt=0)
    unit: Literal["milliseconds", "seconds", "bars"] = "bars"


class CounterfactualMethodology(_FrozenConfig):
    """Named frozen method for later counterfactual evaluation."""

    method: Literal["disabled", "fixed_horizon_markout"] = "disabled"
    methodology_version: str = Field(default="counterfactual-v1", min_length=1)
    reference_price: Literal["next_open", "next_mid", "bar_close"] = "next_open"


class LatencyBudgetConfig(_FrozenConfig):
    """Complete Phase 8 configuration, loaded in Step 1 but not enforced."""

    config_version: str = PHASE8_CONFIG_VERSION
    enabled: bool = False
    classifier_version: str = Field(default="phase8-cohort-v1", min_length=1)
    tau_ms: int = Field(default=30_000, gt=0)
    latency_percentile: Literal[75, 90] = 90
    rolling_history_window: int = Field(default=100, gt=0)
    minimum_prior_samples: int = Field(default=20, ge=1)
    cold_start_policy: Literal["fallback_p90", "insufficient_history", "baseline_only"] = "fallback_p90"
    fallback_p90_latency_ms: int = Field(default=1_000, ge=0)
    freshness_limit_ms: int = Field(default=5_000, gt=0)
    required_buffer_bps: float = Field(default=3.0, ge=0.0)
    acknowledgement_mode: Literal["required", "unsupported"] = "required"
    component_definition_version: str = Field(default="phase8-latency-component-v1", min_length=1)
    estimator_schema_version: str = Field(default="phase8-prior-percentile-v1", min_length=1)
    expiry_policy: ExpiryPolicy = Field(default_factory=ExpiryPolicy)
    cost_assumptions: CostAssumptions = Field(default_factory=CostAssumptions)
    outcome_horizon: OutcomeHorizon = Field(default_factory=OutcomeHorizon)
    counterfactual_methodology: CounterfactualMethodology = Field(default_factory=CounterfactualMethodology)

    @model_validator(mode="after")
    def validate_contract(self) -> "LatencyBudgetConfig":
        """Validate cross-field and schema-version invariants."""
        if self.config_version != PHASE8_CONFIG_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported Phase 8 config version: {self.config_version!r}")
        if self.minimum_prior_samples > self.rolling_history_window:
            raise ValueError("minimum_prior_samples cannot exceed rolling_history_window")
        if self.cold_start_policy == "fallback_p90" and self.fallback_p90_latency_ms <= 0:
            raise ValueError("fallback_p90_latency_ms must be positive for fallback_p90")
        return self

    def canonical_json(self) -> str:
        """Return deterministic JSON suitable for evidence records."""
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        """Return a stable SHA-256 configuration identity."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
