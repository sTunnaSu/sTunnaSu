"""Frozen pre-registered holdout and release policy."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.latency_budgeter.domain.errors import HoldoutPolicyError
from src.latency_budgeter.domain.timestamps import normalize_timestamp

STEP4_HOLDOUT_POLICY_VERSION = "phase8-step4-holdout-v1"


class HoldoutDecisionPolicy(BaseModel):
    """Immutable thresholds declared before the holdout begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = STEP4_HOLDOUT_POLICY_VERSION
    preregistered_at: datetime
    holdout_start: datetime
    holdout_end: datetime
    primary_setting_id: str = Field(min_length=1)
    nearby_setting_ids: tuple[str, ...] = ()
    minimum_effect_bps: Decimal = Field(default=Decimal("0"), ge=0)
    minimum_retention_rate: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    maximum_drawdown: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    maximum_concentration: Decimal = Field(default=Decimal("0.35"), ge=0, le=1)
    maximum_p90_slippage_worsening_bps: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_rejection_rate: Decimal = Field(default=Decimal("0.75"), ge=0, le=1)
    minimum_common_opportunities: int = Field(default=30, ge=2)
    minimum_symbol_clusters: int = Field(default=2, ge=1)
    bootstrap_iterations: int = Field(default=2_000, ge=100)
    bootstrap_block_length: int = Field(default=5, ge=1)
    confidence_level: Decimal = Field(default=Decimal("0.95"), gt=0, lt=1)
    random_seed: int = Field(default=42, ge=0)
    multiple_comparison_count: int = Field(default=1, ge=1)
    require_nearby_agreement: bool = True
    require_provenance_evidence: bool = True
    require_cost_evidence: bool = True
    require_regime_coverage: bool = False
    calibration_requirement: Literal["credible", "complete", "not_required"] = "credible"

    @model_validator(mode="after")
    def validate_frozen_policy(self) -> "HoldoutDecisionPolicy":
        object.__setattr__(self, "preregistered_at", normalize_timestamp(self.preregistered_at))
        object.__setattr__(self, "holdout_start", normalize_timestamp(self.holdout_start))
        object.__setattr__(self, "holdout_end", normalize_timestamp(self.holdout_end))
        if self.policy_version != STEP4_HOLDOUT_POLICY_VERSION:
            raise HoldoutPolicyError(f"unsupported holdout policy: {self.policy_version!r}")
        if self.preregistered_at >= self.holdout_start:
            raise HoldoutPolicyError("holdout policy must be registered before holdout_start")
        if self.holdout_start >= self.holdout_end:
            raise HoldoutPolicyError("holdout_start must precede holdout_end")
        if len(set(self.nearby_setting_ids)) != len(self.nearby_setting_ids):
            raise HoldoutPolicyError("nearby setting IDs must be unique")
        if self.primary_setting_id in self.nearby_setting_ids:
            raise HoldoutPolicyError("primary setting cannot also be a nearby diagnostic setting")
        return self

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
