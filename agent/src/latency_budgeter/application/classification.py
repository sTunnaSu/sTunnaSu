"""Deterministic shared-cohort classification with no execution enforcement."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Generic, TypeVar

from src.latency_budgeter.domain.models import CohortClassification, MarketObservation
from src.latency_budgeter.domain.timestamps import elapsed_ms_exact, normalize_timestamp

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BaselineActivityViews(Generic[T]):
    """Reporting views that cannot remove original baseline activity."""

    all_original_baseline_activity: T
    matched_baseline_activity: T | None
    unmatched_integrity_activity: T | None

    @classmethod
    def classify(
        cls,
        activity: T,
        classification: CohortClassification,
    ) -> "BaselineActivityViews[T]":
        """Partition reporting views without changing the supplied activity."""
        eligible = classification.common_phase8_eligible
        return cls(
            all_original_baseline_activity=activity,
            matched_baseline_activity=activity if eligible else None,
            unmatched_integrity_activity=None if eligible else activity,
        )


class SharedCohortClassifier:
    """Compute explicit shared flags before baseline/budgeter branch selection."""

    def __init__(self, classification_version: str = "phase8-cohort-v1") -> None:
        version = str(classification_version).strip()
        if not version:
            raise ValueError("classification_version is required")
        self.classification_version = version

    def classify(
        self,
        *,
        observation: MarketObservation,
        decision_at: datetime,
        freshness_limit_ms: int,
        strategy_requirements_met: bool,
    ) -> CohortClassification:
        """Derive non-blocking, versioned cohort flags and diagnostics."""
        if freshness_limit_ms <= 0:
            raise ValueError("freshness_limit_ms must be positive")
        decision_utc = normalize_timestamp(decision_at)
        data_age_ms = elapsed_ms_exact(decision_utc, observation.observed_at)
        ingestion_delay_ms = (
            elapsed_ms_exact(observation.source_capture_at, observation.observed_at)
            if observation.source_capture_at is not None
            else None
        )
        processing_delay_ms = (
            elapsed_ms_exact(decision_utc, observation.source_capture_at)
            if observation.source_capture_at is not None
            else None
        )
        reasons: list[str] = []

        provenance_valid = True
        if not observation.source:
            provenance_valid = False
            reasons.append("missing_source")
        if not observation.source_metadata.provider:
            provenance_valid = False
            reasons.append("missing_provider")
        if observation.source_capture_at is None:
            provenance_valid = False
            reasons.append("missing_source_capture_at")
        if ingestion_delay_ms is not None and ingestion_delay_ms < 0:
            provenance_valid = False
            reasons.append("source_capture_precedes_observation")
        if processing_delay_ms is not None and processing_delay_ms < 0:
            provenance_valid = False
            reasons.append("decision_precedes_source_capture")
        if data_age_ms < 0:
            provenance_valid = False
            reasons.append("observation_is_future_dated")

        data_fresh = data_age_ms >= 0 and data_age_ms <= freshness_limit_ms
        if not data_fresh:
            reasons.append("data_not_fresh_for_phase8")
        strategy_met = bool(strategy_requirements_met)
        if not strategy_met:
            reasons.append("strategy_requirements_not_met")
        common_eligible = provenance_valid and data_fresh and strategy_met

        return CohortClassification(
            provenance_valid_for_phase8=provenance_valid,
            data_fresh_for_phase8=data_fresh,
            strategy_requirements_met=strategy_met,
            common_phase8_eligible=common_eligible,
            classification_version=self.classification_version,
            evaluated_at=decision_utc,
            data_age_ms=float(data_age_ms),
            ingestion_delay_ms=float(ingestion_delay_ms) if ingestion_delay_ms is not None else None,
            processing_delay_ms=float(processing_delay_ms) if processing_delay_ms is not None else None,
            reasons=tuple(reasons),
        )
