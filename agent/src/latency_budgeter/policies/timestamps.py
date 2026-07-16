"""Exact pre-decision provenance validation and duration arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from src.latency_budgeter.domain.models import MarketObservation
from src.latency_budgeter.domain.timestamps import elapsed_ms_exact, normalize_timestamp
from src.latency_budgeter.domain.values import Milliseconds


@dataclass(frozen=True, slots=True)
class ProvenanceDurations:
    data_age_ms: Milliseconds | None
    ingestion_delay_ms: Milliseconds | None
    processing_delay_ms: Milliseconds | None

    def to_dict(self) -> dict[str, float | None]:
        return {
            "data_age_ms": self.data_age_ms.to_float() if self.data_age_ms else None,
            "ingestion_delay_ms": self.ingestion_delay_ms.to_float() if self.ingestion_delay_ms else None,
            "processing_delay_ms": self.processing_delay_ms.to_float() if self.processing_delay_ms else None,
        }


@dataclass(frozen=True, slots=True)
class TimestampValidation:
    valid: bool
    reasons: tuple[str, ...]
    durations: ProvenanceDurations
    signed_duration_diagnostics_ms: dict[str, str]


def validate_pre_decision_timestamps(
    observation: MarketObservation,
    decision_at: datetime,
) -> TimestampValidation:
    """Validate only evidence available at the injected decision clock instant."""
    decision = normalize_timestamp(decision_at)
    reasons: list[str] = []
    if not observation.source:
        reasons.append("missing_source")
    if not observation.source_metadata.provider:
        reasons.append("missing_provider")
    if observation.observed_at.utcoffset() != timedelta(0):
        reasons.append("observed_at_not_utc")
    if decision.utcoffset() != timedelta(0):
        reasons.append("decision_at_not_utc")

    data_age_signed = elapsed_ms_exact(decision, observation.observed_at)
    ingestion_signed: Decimal | None = None
    processing_signed: Decimal | None = None
    if observation.source_capture_at is None:
        reasons.append("missing_source_capture_at")
    else:
        if observation.source_capture_at.utcoffset() != timedelta(0):
            reasons.append("source_capture_at_not_utc")
        ingestion_signed = elapsed_ms_exact(observation.source_capture_at, observation.observed_at)
        processing_signed = elapsed_ms_exact(decision, observation.source_capture_at)

    if data_age_signed < 0:
        reasons.append("observed_at_after_decision_at")
    if ingestion_signed is not None and ingestion_signed < 0:
        reasons.append("source_capture_at_before_observed_at")
    if processing_signed is not None and processing_signed < 0:
        reasons.append("source_capture_at_after_decision_at")

    data_age = Milliseconds(data_age_signed) if data_age_signed >= 0 else None
    ingestion = Milliseconds(ingestion_signed) if ingestion_signed is not None and ingestion_signed >= 0 else None
    processing = Milliseconds(processing_signed) if processing_signed is not None and processing_signed >= 0 else None
    diagnostics: dict[str, str] = {"data_age_ms": format(data_age_signed, "f")}
    if ingestion_signed is not None:
        diagnostics["ingestion_delay_ms"] = format(ingestion_signed, "f")
    if processing_signed is not None:
        diagnostics["processing_delay_ms"] = format(processing_signed, "f")
    return TimestampValidation(
        valid=not reasons,
        reasons=tuple(dict.fromkeys(reasons)),
        durations=ProvenanceDurations(data_age, ingestion, processing),
        signed_duration_diagnostics_ms=diagnostics,
    )


def is_fresh(data_age: Milliseconds, freshness_limit_ms: int) -> bool:
    """The architecture's inclusive boundary: age <= frozen limit is fresh."""
    if freshness_limit_ms <= 0:
        raise ValueError("freshness_limit_ms must be positive")
    return data_age.value <= Decimal(freshness_limit_ms)
