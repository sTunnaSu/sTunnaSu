"""End-to-end Phase 8 Step 2 gate tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest

from src.latency_budgeter.application.gate import LatencyBudgetDecisionGate
from src.latency_budgeter.application.intake import Phase8IntakeService
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import (
    AvailableLatencyMeasurement,
    ComponentEstimateMode,
    DecisionOutcome,
    DecisionReason,
    PreDecisionLatencyMeasurements,
)
from src.latency_budgeter.domain.errors import ConfigDriftError
from src.latency_budgeter.domain.events import EventType
from src.latency_budgeter.domain.history import LatencyComponent, LatencySample
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.models import MarketObservation, SourceMetadata
from src.latency_budgeter.domain.values import BasisPoints, Milliseconds
from src.latency_budgeter.persistence.approved_memory import InMemoryApprovedOpportunityPort
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger
from src.latency_budgeter.ports.edge import GrossEdgeEstimate

from .conftest import NOW


@dataclass
class FixedEdgeEstimator:
    value: str | float
    estimated_at_offset_ms: int = 0
    calls: int = 0

    def estimate(self, *, observation, signal, decision_at) -> GrossEdgeEstimate:
        self.calls += 1
        return GrossEdgeEstimate(
            gross_edge_bps=BasisPoints(self.value),
            estimated_at=decision_at + timedelta(milliseconds=self.estimated_at_offset_ms),
            estimator_version="fixed-test-v1",
            reference_price=Decimal("100"),
        )


def enabled_config(**overrides) -> LatencyBudgetConfig:
    values = {
        "enabled": True,
        "tau_ms": 1_000_000,
        "freshness_limit_ms": 5_000,
        "minimum_prior_samples": 2,
        "rolling_history_window": 3,
        "cold_start_policy": "fallback_p90",
        "fallback_p90_latency_ms": 10,
        "required_buffer_bps": 3,
        "cost_assumptions": {
            "maker_fee_bps": 1,
            "taker_fee_bps": 2,
            "spread_bps": 3,
            "slippage_bps": 4,
            "impact_bps": 1,
        },
    }
    values.update(overrides)
    return LatencyBudgetConfig(**values)


def make_observation(
    *,
    observed_offset_ms: float = -900,
    capture_offset_ms: float | None = -400,
    source: str = "feed",
    provider: str = "provider",
    side: str = "buy",
) -> MarketObservation:
    capture = NOW + timedelta(milliseconds=capture_offset_ms) if capture_offset_ms is not None else None
    return MarketObservation.from_input(
        source=source,
        observed_at=NOW + timedelta(milliseconds=observed_offset_ms),
        source_capture_at=capture,
        timezone="UTC",
        symbol="BTC/USD",
        side=side,
        strategy_version="strategy-v1",
        source_metadata=SourceMetadata(provider=provider),
        raw_payload={"mid": 100},
    )


def build_gate(
    *,
    config: LatencyBudgetConfig | None = None,
    edge: FixedEdgeEstimator | None = None,
    ledger: InMemoryEventLedger | None = None,
    history: InMemoryLatencyHistoryStore | None = None,
    approved: InMemoryApprovedOpportunityPort | None = None,
) -> tuple[
    LatencyBudgetDecisionGate,
    InMemoryEventLedger,
    InMemoryLatencyHistoryStore,
    InMemoryApprovedOpportunityPort,
    FixedEdgeEstimator,
]:
    ledger = ledger or InMemoryEventLedger()
    history = history or InMemoryLatencyHistoryStore()
    approved = approved or InMemoryApprovedOpportunityPort()
    edge = edge or FixedEdgeEstimator(100)
    gate = LatencyBudgetDecisionGate(
        config=config or enabled_config(),
        ledger=ledger,
        history=history,
        edge_estimator=edge,
        approved_port=approved,
        clock=lambda: NOW,
        clock_source="fixed-test-clock",
    )
    return gate, ledger, history, approved, edge


def run(gate: LatencyBudgetDecisionGate, observation: MarketObservation | None = None, **kwargs):
    return gate.evaluate(
        observation=observation or make_observation(),
        run_id=kwargs.pop("run_id", Phase8IdentifierFactory.new_run_id()),
        strategy_requirements_met=kwargs.pop("strategy_requirements_met", True),
        **kwargs,
    )


def assert_terminal_without_execution(result, ledger) -> None:
    stream = ledger.read(result.event.decision_id)
    assert [event.event_type for event in stream] == [EventType.DECISION_CREATED]
    assert stream[0].payload["execution_events_emitted"] is False


def test_valid_allow_path_publishes_immutable_step3_opportunity() -> None:
    gate, ledger, _, approved, _ = build_gate()

    result = run(gate)

    assert result.outcome is DecisionOutcome.ALLOW
    assert result.reason is DecisionReason.ALLOW_NET_EDGE_ABOVE_BUFFER
    assert result.approved_signal_count_increment == 1
    assert result.approved_opportunity is approved.get(result.event.decision_id)
    assert result.event.payload["decision_economics"]["estimated_cost_bps"] == 10.0
    assert result.event.payload["decision_economics"]["forecast_total_latency_ms"] == 950.0
    assert_terminal_without_execution(result, ledger)
    with pytest.raises(TypeError):
        result.event.payload["decision_economics"]["gross_edge_bps"] = 0


@pytest.mark.parametrize(
    ("observation", "reason"),
    [
        (make_observation(capture_offset_ms=None), DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER),
        (make_observation(observed_offset_ms=1, capture_offset_ms=2), DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER),
        (make_observation(source=""), DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER),
        (make_observation(provider=""), DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER),
        (make_observation(observed_offset_ms=-5_000.001, capture_offset_ms=-1), DecisionReason.REJECT_STALE_DATA),
    ],
)
def test_timestamp_and_stale_rejection_paths(observation, reason) -> None:
    gate, ledger, _, approved, edge = build_gate()

    result = run(gate, observation)

    assert result.outcome is DecisionOutcome.REJECT
    assert result.reason is reason
    if reason is DecisionReason.REJECT_STALE_DATA:
        assert result.event.payload["shared_cohort_classification"]["data_fresh_for_phase8"] is False
    assert approved.all() == ()
    assert edge.calls == 0
    assert_terminal_without_execution(result, ledger)


def test_exact_stale_boundary_is_non_stale() -> None:
    gate, _, _, _, edge = build_gate()

    result = run(gate, make_observation(observed_offset_ms=-5_000, capture_offset_ms=-1))

    assert result.reason is DecisionReason.ALLOW_NET_EDGE_ABOVE_BUFFER
    assert edge.calls == 1


def test_common_ineligible_is_audited_but_excluded_from_matched_denominator() -> None:
    gate, ledger, _, approved, edge = build_gate()

    result = run(gate, strategy_requirements_met=False)

    assert result.reason is DecisionReason.REJECT_COMMON_COHORT_INELIGIBLE
    assert result.event.payload["matched_primary_denominator_increment"] == 0
    assert result.event.payload["baseline_compatibility"]["all_original_baseline_activity"] is True
    assert result.event.payload["baseline_compatibility"]["baseline_execution_modified"] is False
    assert approved.all() == ()
    assert edge.calls == 0
    assert_terminal_without_execution(result, ledger)


@pytest.mark.parametrize("gross", [0, -0.000000001, -20])
def test_nonpositive_gross_edge_rejects_before_history(gross) -> None:
    gate, ledger, _, approved, _ = build_gate(edge=FixedEdgeEstimator(gross))

    result = run(gate)

    assert result.reason is DecisionReason.REJECT_NONPOSITIVE_GROSS_EDGE
    assert result.event.payload["gross_edge_evidence"]["gross_edge_bps"] <= 0
    assert approved.all() == ()
    assert_terminal_without_execution(result, ledger)


def test_absent_fallback_defers_terminally() -> None:
    config = enabled_config(cold_start_policy="insufficient_history", fallback_p90_latency_ms=0)
    gate, ledger, _, approved, _ = build_gate(config=config)

    result = run(gate)

    assert result.outcome is DecisionOutcome.DEFER
    assert result.reason is DecisionReason.DEFER_COLD_START
    assert result.event.payload["diagnostics"]["reconsideration_allowed"] is False
    assert approved.all() == ()
    assert_terminal_without_execution(result, ledger)


def _add_zero_history(history: InMemoryLatencyHistoryStore, config: LatencyBudgetConfig) -> None:
    for component in (LatencyComponent.SUBMISSION, LatencyComponent.FILL):
        history.add(
            LatencySample(
                sample_id=f"sample-{component.value}",
                decision_id=f"prior-{component.value}",
                component=component,
                value_ms=Milliseconds(0),
                component_available_at=NOW - timedelta(microseconds=1),
                recorded_at=NOW,
                component_definition_version=config.component_definition_version,
                estimator_schema_version=config.estimator_schema_version,
            )
        )


def _zero_measurements() -> PreDecisionLatencyMeasurements:
    return PreDecisionLatencyMeasurements(
        decision=AvailableLatencyMeasurement(
            LatencyComponent.DECISION,
            Milliseconds(0),
            NOW - timedelta(microseconds=1),
        ),
        risk_processing=AvailableLatencyMeasurement(
            LatencyComponent.RISK_PROCESSING,
            Milliseconds(0),
            NOW - timedelta(microseconds=1),
        ),
    )


def test_strict_net_edge_equality_rejects() -> None:
    config = enabled_config(
        tau_ms=1_000,
        minimum_prior_samples=1,
        acknowledgement_mode="unsupported",
        required_buffer_bps=3,
        cost_assumptions={"taker_fee_bps": 2, "spread_bps": 3, "slippage_bps": 2},
    )
    history = InMemoryLatencyHistoryStore()
    _add_zero_history(history, config)
    gate, ledger, _, approved, _ = build_gate(
        config=config,
        history=history,
        edge=FixedEdgeEstimator(10),
    )

    result = run(
        gate,
        make_observation(observed_offset_ms=0, capture_offset_ms=0),
        measurements=_zero_measurements(),
    )

    assert result.reason is DecisionReason.REJECT_INSUFFICIENT_NET_EDGE
    assert result.event.payload["decision_economics"]["net_edge_bps"] == 3.0
    assert approved.all() == ()
    assert_terminal_without_execution(result, ledger)


def test_unsupported_acknowledgement_is_explicit_zero_not_missing_optimism() -> None:
    config = enabled_config(acknowledgement_mode="unsupported")
    gate, _, _, _, _ = build_gate(config=config)

    result = run(gate)
    components = result.event.payload["decision_economics"]["component_estimates"]
    acknowledgement = next(row for row in components if row["component"] == "acknowledgement")

    assert acknowledgement["value_ms"] == 0.0
    assert acknowledgement["mode"] == ComponentEstimateMode.UNSUPPORTED_BY_VENUE.value


def test_sufficient_history_uses_rolling_prior_only_and_records_window_counts() -> None:
    config = enabled_config(minimum_prior_samples=2, rolling_history_window=2)
    history = InMemoryLatencyHistoryStore()
    for component in LatencyComponent:
        for index, value in enumerate((10, 40)):
            history.add(
                LatencySample(
                    sample_id=f"{component.value}-{index}",
                    decision_id=f"prior-{component.value}-{index}",
                    component=component,
                    value_ms=Milliseconds(value),
                    component_available_at=NOW - timedelta(milliseconds=2 - index),
                    recorded_at=NOW - timedelta(milliseconds=2 - index),
                    component_definition_version=config.component_definition_version,
                    estimator_schema_version=config.estimator_schema_version,
                )
            )
    gate, _, _, _, _ = build_gate(config=config, history=history)

    result = run(gate)
    economics = result.event.payload["decision_economics"]

    assert economics["latency_estimator_mode"] == "rolling_prior_only"
    assert all(row["prior_sample_count"] == 2 for row in economics["component_estimates"])
    assert all(row["value_ms"] == 40.0 for row in economics["component_estimates"])


def test_future_current_measurement_is_excluded_and_missing_component_defers() -> None:
    config = enabled_config(cold_start_policy="baseline_only", fallback_p90_latency_ms=0)
    gate, _, _, approved, _ = build_gate(config=config)
    measurements = PreDecisionLatencyMeasurements(
        decision=AvailableLatencyMeasurement(
            LatencyComponent.DECISION,
            Milliseconds(1),
            NOW + timedelta(microseconds=1),
        )
    )

    result = run(gate, measurements=measurements)

    assert result.reason is DecisionReason.DEFER_COLD_START
    assert "decision has 0 valid prior samples" in result.event.payload["diagnostics"]["cold_start_detail"]
    assert approved.all() == ()


def test_repeated_evaluation_is_deterministic_and_does_not_republish() -> None:
    gate, ledger, _, approved, edge = build_gate()
    run_id = Phase8IdentifierFactory.new_run_id()
    observation = make_observation()

    first = run(gate, observation, run_id=run_id)
    second = run(gate, observation, run_id=run_id)

    assert second.event == first.event
    assert second.outcome == first.outcome
    assert second.approved_opportunity == first.approved_opportunity
    assert second.appended is False
    assert second.approved_signal_count_increment == 0
    assert len(approved.all()) == 1
    assert edge.calls == 1
    assert len(ledger.read(first.event.decision_id)) == 1


def test_config_fingerprint_is_pinned_across_repeated_decision() -> None:
    ledger = InMemoryEventLedger()
    history = InMemoryLatencyHistoryStore()
    approved = InMemoryApprovedOpportunityPort()
    first_gate, _, _, _, _ = build_gate(ledger=ledger, history=history, approved=approved)
    run_id = Phase8IdentifierFactory.new_run_id()
    observation = make_observation()
    run(first_gate, observation, run_id=run_id)
    changed_gate, _, _, _, _ = build_gate(
        config=enabled_config(required_buffer_bps=99),
        ledger=ledger,
        history=history,
        approved=approved,
    )

    with pytest.raises(ConfigDriftError):
        run(changed_gate, observation, run_id=run_id)


def test_durable_allow_replay_reconstructs_and_repairs_step3_publication(tmp_path) -> None:
    config = enabled_config()
    path = tmp_path / "phase8.db"
    first_ledger = SQLiteEventLedger(path)
    first_port = InMemoryApprovedOpportunityPort()
    first_edge = FixedEdgeEstimator(100)
    first_gate = LatencyBudgetDecisionGate(
        config=config,
        ledger=first_ledger,
        history=InMemoryLatencyHistoryStore(),
        edge_estimator=first_edge,
        approved_port=first_port,
        clock=lambda: NOW,
        clock_source="fixed-test-clock",
    )
    run_id = Phase8IdentifierFactory.new_run_id()
    observation = make_observation()
    first = run(first_gate, observation, run_id=run_id)
    first_ledger.close()

    reopened = SQLiteEventLedger(path)
    recovery_port = InMemoryApprovedOpportunityPort()
    recovery_edge = FixedEdgeEstimator(-999)
    recovery_gate = LatencyBudgetDecisionGate(
        config=config,
        ledger=reopened,
        history=InMemoryLatencyHistoryStore(),
        edge_estimator=recovery_edge,
        approved_port=recovery_port,
        clock=lambda: NOW,
        clock_source="fixed-test-clock",
    )
    replay = run(recovery_gate, observation, run_id=run_id)

    assert replay.event == first.event
    assert replay.outcome is DecisionOutcome.ALLOW
    assert replay.approved_opportunity == first.approved_opportunity
    assert replay.approved_signal_count_increment == 1
    assert recovery_port.get(first.event.decision_id) == first.approved_opportunity
    assert recovery_edge.calls == 0
    reopened.close()


def test_future_edge_estimate_is_rejected_as_unavailable_evidence() -> None:
    gate, ledger, _, approved, _ = build_gate(edge=FixedEdgeEstimator(100, estimated_at_offset_ms=1))

    result = run(gate)

    assert result.reason is DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER
    assert result.event.payload["timestamp_validation"]["valid"] is False
    assert result.event.payload["diagnostics"]["timestamp_reasons"] == ("gross_edge_estimated_after_decision",)
    assert approved.all() == ()
    assert_terminal_without_execution(result, ledger)


def test_disabled_baseline_path_remains_step1_only(phase8_config, observation) -> None:
    ledger = InMemoryEventLedger()
    service = Phase8IntakeService(config=phase8_config, ledger=ledger, clock=lambda: NOW)
    baseline = service.record_raw_signal(
        observation=observation,
        run_id=Phase8IdentifierFactory.new_run_id(),
        decision_at=NOW,
        strategy_requirements_met=True,
    )

    assert baseline.event.integrity_metadata["step"] == "phase8-step1"
    assert baseline.event.payload["baseline_compatibility"]["baseline_execution_modified"] is False
    with pytest.raises(ValueError, match="enabled=True"):
        LatencyBudgetDecisionGate(
            config=phase8_config,
            ledger=ledger,
            history=InMemoryLatencyHistoryStore(),
            edge_estimator=FixedEdgeEstimator(10),
            approved_port=InMemoryApprovedOpportunityPort(),
        )
