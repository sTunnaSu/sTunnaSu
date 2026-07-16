"""Shared-cohort and baseline-compatibility tests."""

from __future__ import annotations

from datetime import timedelta

from src.latency_budgeter.application.classification import (
    BaselineActivityViews,
    SharedCohortClassifier,
)
from src.latency_budgeter.domain.models import MarketObservation, SourceMetadata

from .conftest import NOW


def test_common_cohort_classification_is_deterministic(observation) -> None:
    classifier = SharedCohortClassifier("phase8-cohort-v1")

    first = classifier.classify(
        observation=observation,
        decision_at=NOW,
        freshness_limit_ms=5_000,
        strategy_requirements_met=True,
    )
    second = classifier.classify(
        observation=observation,
        decision_at=NOW,
        freshness_limit_ms=5_000,
        strategy_requirements_met=True,
    )

    assert first == second
    assert first.provenance_valid_for_phase8 is True
    assert first.data_fresh_for_phase8 is True
    assert first.strategy_requirements_met is True
    assert first.common_phase8_eligible is True
    assert first.data_age_ms == 900
    assert first.ingestion_delay_ms == 500
    assert first.processing_delay_ms == 400


def test_stale_and_strategy_ineligible_flags_do_not_block_baseline(observation) -> None:
    classification = SharedCohortClassifier().classify(
        observation=observation,
        decision_at=NOW + timedelta(seconds=10),
        freshness_limit_ms=5_000,
        strategy_requirements_met=False,
    )
    activity = {"baseline_order": "unchanged"}
    views = BaselineActivityViews.classify(activity, classification)

    assert classification.common_phase8_eligible is False
    assert views.all_original_baseline_activity is activity
    assert views.matched_baseline_activity is None
    assert views.unmatched_integrity_activity is activity


def test_invalid_timestamp_order_is_classified_not_silently_corrected() -> None:
    observation = MarketObservation.from_input(
        source="feed",
        observed_at=NOW,
        source_capture_at=NOW - timedelta(milliseconds=1),
        timezone="UTC",
        symbol="BTC/USD",
        side="buy",
        strategy_version="v1",
        source_metadata=SourceMetadata(provider="provider"),
        raw_payload={"price": 1.0},
    )
    classification = SharedCohortClassifier().classify(
        observation=observation,
        decision_at=NOW + timedelta(milliseconds=1),
        freshness_limit_ms=5_000,
        strategy_requirements_met=True,
    )

    assert classification.provenance_valid_for_phase8 is False
    assert "source_capture_precedes_observation" in classification.reasons
    assert classification.common_phase8_eligible is False


def test_future_observation_is_unmatched_integrity_activity() -> None:
    observation = MarketObservation.from_input(
        source="feed",
        observed_at=NOW + timedelta(seconds=1),
        source_capture_at=NOW + timedelta(seconds=2),
        timezone="UTC",
        symbol="ETH/USD",
        side="sell",
        strategy_version="v1",
        source_metadata=SourceMetadata(provider="provider"),
        raw_payload={"price": 1.0},
    )
    classification = SharedCohortClassifier().classify(
        observation=observation,
        decision_at=NOW,
        freshness_limit_ms=5_000,
        strategy_requirements_met=True,
    )

    assert classification.provenance_valid_for_phase8 is False
    assert classification.data_fresh_for_phase8 is False
    assert "observation_is_future_dated" in classification.reasons
