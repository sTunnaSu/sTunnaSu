"""Independent clean-room adversarial tests for the Phase 8 audit.

These tests target specification boundaries rather than mirroring existing
implementation branches.  They intentionally document release-blocking
behaviour found during the independent model-risk review.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pandas as pd

from src.latency_budgeter.adapters.base_engine import BaseEngineLifecycleAdapter
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import (
    AvailableLatencyMeasurement,
    ComponentEstimateMode,
    PreDecisionLatencyMeasurements,
)
from src.latency_budgeter.domain.history import LatencyComponent
from src.latency_budgeter.domain.values import Milliseconds
from src.latency_budgeter.estimation.forecast import PriorOnlyLatencyForecaster
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore

from .conftest import NOW
from .test_step3_base_engine_adapter import AdapterEngine, create_entry
from .test_step3_lifecycle import BASE, build_context
from .test_step4_research import (
    build_result,
    holdout_policy,
    make_summary,
    two_arm_summaries,
)


def test_measurement_available_exactly_at_decision_is_not_prior_information() -> None:
    """The frozen architecture requires availability strictly before decision time."""
    config = LatencyBudgetConfig(
        enabled=True,
        minimum_prior_samples=1,
        rolling_history_window=1,
        cold_start_policy="fallback_p90",
        fallback_p90_latency_ms=25,
        acknowledgement_mode="unsupported",
    )
    measurements = PreDecisionLatencyMeasurements(
        decision=AvailableLatencyMeasurement(
            LatencyComponent.DECISION,
            Milliseconds(1),
            NOW,
        ),
        risk_processing=AvailableLatencyMeasurement(
            LatencyComponent.RISK_PROCESSING,
            Milliseconds(2),
            NOW - timedelta(microseconds=1),
        ),
    )

    forecast = PriorOnlyLatencyForecaster(InMemoryLatencyHistoryStore()).forecast(
        decision_at=NOW,
        current_decision_id="current-decision",
        data_age_ms=Milliseconds(0),
        config=config,
        measurements=measurements,
    )

    assert forecast.decision.mode is ComponentEstimateMode.COLD_START_FALLBACK
    assert forecast.decision.value_ms == Milliseconds(25)
    assert (
        forecast.decision.excluded_current_measurement_reason
        == "current_measurement_unavailable_at_decision"
    )
    assert forecast.risk_processing.mode is ComponentEstimateMode.MEASURED_PRE_DECISION


def test_lifecycle_integrity_failure_invalidates_primary_experiment() -> None:
    """Retained race evidence must fail closed at the research release boundary."""
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    budgeter = (
        make_summary(
            "opp-0",
            arm="budgeter",
            index=0,
            integrity_failures=1,
        ),
        budgeter[1],
    )

    result = build_result(baseline, budgeter, policy)

    assert result.validity.valid is False
    assert result.validity.primary_improvement_claim_allowed is False


def test_authorization_cannot_be_consumed_by_an_unrelated_signal_identity() -> None:
    """Symbol and side alone are not a sufficient causal authorization key."""
    context = build_context()
    adapter = BaseEngineLifecycleAdapter(context.service)
    adapter.arm(context.root.decision_id)

    authorization = adapter.authorize_submission(
        {
            "decision_id": "dec_unrelated",
            "signal_id": "sig_unrelated",
            "symbol": "BTC/USD",
            "side": "buy",
            "event_type": "entry",
            "signal_time": BASE,
        }
    )

    assert authorization is None


class _MalformedAuthorizationObserver:
    @staticmethod
    def requires_authorization(intent) -> bool:
        return True

    @staticmethod
    def authorize_submission(intent):
        return object()

    @staticmethod
    def on_order_submitted(order, authorization) -> None:
        raise AssertionError("malformed authorization reached post-submission callback")


def test_malformed_authorization_is_rejected_before_order_registration() -> None:
    """Fail-closed enforcement must occur before the engine mutates order state."""
    engine = AdapterEngine({"initial_cash": 10_000})
    timestamp = pd.Timestamp(BASE + timedelta(milliseconds=200))
    engine._execution_dates = pd.DatetimeIndex([timestamp])
    engine.set_order_lifecycle_observer(_MalformedAuthorizationObserver())

    order = create_entry(
        engine,
        timestamp,
        quantity=1,
        decision_id="dec_expected",
        signal_id="sig_expected",
    )

    assert order.status == "rejected"
    assert order.status_reason == "phase8_not_authorized"


def test_matched_arms_require_identical_frozen_outcome_price_evidence() -> None:
    """A shared dataset label cannot substitute for identical outcome evidence."""
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    changed_outcome = {
        **budgeter[0].strategy_outcome,
        "reference_prices": {
            **budgeter[0].strategy_outcome["reference_prices"],
            "outcome_reference_price": "999",
        },
        "data_provenance": {
            **budgeter[0].strategy_outcome["data_provenance"],
            "query_fingerprint": "different-query",
        },
    }
    budgeter = (replace(budgeter[0], strategy_outcome=changed_outcome), budgeter[1])

    result = build_result(baseline, budgeter, policy)

    assert result.validity.valid is False
    assert result.validity.primary_improvement_claim_allowed is False


def test_non_gate_configuration_is_derived_not_self_attested() -> None:
    """Matched arms must prove every non-gate setting is identical from root snapshots."""
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    baseline_root = {
        **baseline[0].original_decision,
        "phase8_config": {"enabled": False, "tau_ms": 10_000},
    }
    budgeter_root = {
        **budgeter[0].original_decision,
        "phase8_config": {"enabled": True, "tau_ms": 90_000},
    }
    baseline = (replace(baseline[0], original_decision=baseline_root), baseline[1])
    budgeter = (replace(budgeter[0], original_decision=budgeter_root), budgeter[1])

    result = build_result(baseline, budgeter, policy)

    assert result.validity.valid is False
    assert result.validity.primary_improvement_claim_allowed is False
