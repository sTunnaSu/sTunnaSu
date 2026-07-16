"""End-to-end Step 1 intake and deterministic projection tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.latency_budgeter.application.intake import Phase8IntakeService
from src.latency_budgeter.domain.errors import ProjectionError
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger
from src.latency_budgeter.projections.decision_summary import DecisionSummaryProjector

from .conftest import NOW


def test_valid_opportunity_creation_and_idempotent_raw_signal_count(phase8_config, observation) -> None:
    ledger = InMemoryEventLedger()
    run_id = Phase8IdentifierFactory.new_run_id()
    service = Phase8IntakeService(
        config=phase8_config,
        ledger=ledger,
        clock=lambda: NOW + timedelta(seconds=1),
    )

    first = service.record_raw_signal(
        observation=observation,
        run_id=run_id,
        decision_at=NOW,
        strategy_requirements_met=True,
        signal_key="alpha-1",
    )
    retry = service.record_raw_signal(
        observation=observation,
        run_id=run_id,
        decision_at=NOW,
        strategy_requirements_met=True,
        signal_key="alpha-1",
    )

    assert first.appended is True
    assert first.raw_signal_count_increment == 1
    assert retry.appended is False
    assert retry.raw_signal_count_increment == 0
    assert retry.event == first.event
    assert first.classification.common_phase8_eligible is True
    assert first.baseline_views.all_original_baseline_activity == first.signal
    assert first.baseline_views.matched_baseline_activity == first.signal
    assert first.event.payload["baseline_compatibility"]["baseline_execution_modified"] is False


def test_projection_reconstructs_lifecycle_without_overwriting_original_forecast(phase8_config, observation) -> None:
    ledger = InMemoryEventLedger()
    run_id = Phase8IdentifierFactory.new_run_id()
    service = Phase8IntakeService(
        config=phase8_config,
        ledger=ledger,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    intake = service.record_raw_signal(
        observation=observation,
        run_id=run_id,
        decision_at=NOW,
        strategy_requirements_met=True,
    )
    original_payload = intake.event.payload

    execution = LedgerEvent.create(
        event_type=EventType.EXECUTION_EVALUATED,
        occurred_at=NOW + timedelta(milliseconds=100),
        recorded_at=NOW + timedelta(seconds=1),
        decision_id=intake.event.decision_id,
        run_id=run_id,
        signal_id=intake.signal.signal_id,
        payload={"forecast_edge_bps": 12.0, "realized_cost_bps": 5.0},
        idempotency_key="execution-evaluated",
        causation_id=intake.event.event_id,
    )
    execution_result = ledger.append(execution, expected_version=1)
    outcome = LedgerEvent.create(
        event_type=EventType.OUTCOME_EVALUATED,
        occurred_at=NOW + timedelta(milliseconds=200),
        recorded_at=NOW + timedelta(seconds=1),
        decision_id=intake.event.decision_id,
        run_id=run_id,
        signal_id=intake.signal.signal_id,
        payload={"realized_outcome_bps": -2.0, "forecast_edge_bps": -999.0},
        idempotency_key="outcome-evaluated",
        causation_id=execution_result.event.event_id,
    )
    ledger.append(outcome, expected_version=2)

    stream = ledger.read(intake.event.decision_id)
    first = DecisionSummaryProjector().replay(stream)
    second = DecisionSummaryProjector().replay(stream)

    assert first == second
    assert first.raw_signal_count == 1
    assert first.aggregate_version == 3
    assert first.original_decision == original_payload
    assert first.execution_evaluations[0]["forecast_edge_bps"] == 12.0
    assert first.outcome_evaluations[0]["forecast_edge_bps"] == -999.0
    assert "realized_outcome_bps" not in first.original_decision
    with pytest.raises(TypeError):
        first.original_decision["changed"] = True  # type: ignore[index]


def test_sqlite_intake_replay_is_deterministic_across_restart(tmp_path, phase8_config, observation) -> None:
    path = tmp_path / "phase8.db"
    run_id = Phase8IdentifierFactory.new_run_id()
    ledger = SQLiteEventLedger(path)
    service = Phase8IntakeService(
        config=phase8_config,
        ledger=ledger,
        clock=lambda: NOW + timedelta(seconds=1),
    )
    intake = service.record_raw_signal(
        observation=observation,
        run_id=run_id,
        decision_at=NOW,
        strategy_requirements_met=True,
    )
    before_restart = DecisionSummaryProjector().replay(ledger.read(intake.event.decision_id))
    ledger.close()

    reopened = SQLiteEventLedger(path)
    after_restart = DecisionSummaryProjector().replay(reopened.read(intake.event.decision_id))
    reopened.close()

    assert after_restart == before_restart


def test_projection_rejects_non_contiguous_or_mixed_stream(event_factory) -> None:
    ledger = InMemoryEventLedger()
    root = ledger.append(event_factory()).event
    later = event_factory(
        EventType.ORDER_SUBMITTED,
        occurred_offset_ms=1,
        idempotency_key="later",
    )
    ledger.append(later, expected_version=1)
    stream = list(ledger.read(root.decision_id))
    stream[1] = __import__("dataclasses").replace(stream[1], aggregate_version=3)

    with pytest.raises(ProjectionError, match="non-contiguous aggregate version"):
        DecisionSummaryProjector().replay(stream)
