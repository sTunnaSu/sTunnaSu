"""Adversarial immutable-outcome tests for Phase 8 Step 4."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.latency_budgeter.application.outcomes import OutcomeEvaluationService
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.errors import OutcomeConflictError, OutcomeEvidenceError
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.outcomes import (
    CounterfactualType,
    OutcomeTrigger,
    OutcomeTriggerType,
    OutcomeType,
    ReferencePriceEvidence,
    Step4OutcomePolicy,
)
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.latency_budgeter.ports.outcomes import ReferencePriceRequest
from src.latency_budgeter.projections.research_summary import ResearchDecisionSummaryProjector

BASE = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


@dataclass
class StaticProvider:
    evidence: ReferencePriceEvidence | None
    calls: int = 0
    last_request: ReferencePriceRequest | None = None

    def resolve(self, request: ReferencePriceRequest) -> ReferencePriceEvidence | None:
        self.calls += 1
        self.last_request = request
        return self.evidence


@dataclass
class OutcomeContext:
    config: LatencyBudgetConfig
    ledger: InMemoryEventLedger
    root: LedgerEvent
    clock: MutableClock
    service: OutcomeEvaluationService


def build_context(
    *,
    enabled: bool = True,
    decision: str = "ALLOW",
    side: str = "buy",
    horizon_value: int = 60,
    horizon_unit: str = "seconds",
    counterfactual: str = "fixed_horizon_markout",
    observation_fingerprint: str = "a" * 64,
    signal_key: str = "step4",
) -> OutcomeContext:
    config = LatencyBudgetConfig(
        enabled=enabled,
        minimum_prior_samples=1,
        rolling_history_window=10,
        outcome_horizon={"value": horizon_value, "unit": horizon_unit},
        counterfactual_methodology={
            "method": counterfactual,
            "methodology_version": "shadow-v1",
            "reference_price": "next_open",
        },
        cost_assumptions={
            "maker_fee_bps": 1,
            "taker_fee_bps": 2,
            "spread_bps": 3,
            "slippage_bps": 4,
            "impact_bps": 1,
        },
    )
    factory = Phase8IdentifierFactory()
    run_id = factory.new_run_id()
    signal_id = factory.signal_id(
        run_id=run_id,
        observation_fingerprint=observation_fingerprint,
        strategy_version="strategy-v1",
        side=side,
        signal_key=signal_key,
    )
    decision_id = factory.decision_id(
        run_id=run_id,
        signal_id=signal_id,
        config_version=config.config_version,
    )
    economics = {
        "gross_edge_bps": 30,
        "edge_at_fill_bps": 20,
        "net_edge_bps": 10,
        "estimated_cost_bps": 10,
        "forecast_decision_latency_ms": 100,
        "forecast_submission_latency_ms": 100,
        "forecast_ack_latency_ms": 100,
        "forecast_fill_latency_ms": 100,
    }
    raw_signal = {
        "signal_id": signal_id,
        "run_id": run_id,
        "observation_fingerprint": observation_fingerprint,
        "generated_at": BASE.isoformat(),
        "symbol": "BTC/USD",
        "side": side,
        "strategy_version": "strategy-v1",
        "metadata": {"regime": "bull"},
    }
    payload = {
        "decision": decision,
        "reason_code": "NET_EDGE_POSITIVE" if decision == "ALLOW" else "NET_EDGE_INSUFFICIENT",
        "decision_at": BASE.isoformat(),
        "observation": {
            "observed_at": (BASE - timedelta(milliseconds=100)).isoformat(),
            "source_capture_at": (BASE - timedelta(milliseconds=50)).isoformat(),
            "symbol": "BTC/USD",
            "side": side,
        },
        "raw_strategy_signal": raw_signal,
        "raw_signal_count_increment": 1,
        "shared_cohort_classification": {
            "common_phase8_eligible": True,
            "classification_version": "phase8-cohort-v1",
        },
        "baseline_compatibility": {
            "all_original_baseline_activity": True,
            "matched_baseline_activity": True,
            "unmatched_integrity_activity": False,
            "baseline_execution_modified": False,
        },
        "config_version": config.config_version,
        "config_fingerprint": config.fingerprint,
        "phase8_config": config.model_dump(mode="json"),
        "gross_edge_evidence": {
            "gross_edge_bps": 30,
            "reference_price": "100",
            "estimator_version": "edge-v1",
        },
        "decision_economics": economics if decision == "ALLOW" else None,
        "approved_opportunity": {"quantity": "10"} if decision == "ALLOW" else None,
    }
    root = LedgerEvent.create(
        event_type=EventType.DECISION_CREATED,
        occurred_at=BASE,
        recorded_at=BASE,
        decision_id=decision_id,
        run_id=run_id,
        signal_id=signal_id,
        payload=payload,
        idempotency_key=f"decision_created:{decision_id}",
    )
    ledger = InMemoryEventLedger()
    root = ledger.append(root, expected_version=0).event
    clock = MutableClock(BASE + timedelta(seconds=max(horizon_value, 60) + 1))
    policy = Step4OutcomePolicy(preregistered_at=BASE - timedelta(days=1))
    service = OutcomeEvaluationService(policy=policy, ledger=ledger, clock=clock, clock_source="test-clock")
    return OutcomeContext(config, ledger, root, clock, service)


def append_event(
    context: OutcomeContext,
    event_type: EventType,
    payload: dict,
    occurred_at: datetime,
    suffix: str,
) -> LedgerEvent:
    event = LedgerEvent.create(
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        decision_id=context.root.decision_id,
        run_id=context.root.run_id,
        signal_id=context.root.signal_id,
        payload=payload,
        idempotency_key=f"{event_type.value}:{context.root.decision_id}:{suffix}",
    )
    return context.ledger.append(
        event, expected_version=len(context.ledger.read(context.root.decision_id))
    ).event


def add_execution(
    context: OutcomeContext,
    *,
    submitted_quantity: str = "10",
    fill_quantities: tuple[str, ...] = ("10",),
    terminal_state: str = "FULLY_FILLED",
    terminal_unfilled: str = "0",
    include_ack_latency: bool = True,
) -> None:
    append_event(
        context,
        EventType.ORDER_SUBMITTED,
        {
            "order_id": "order-1",
            "client_order_id": "client-1",
            "submitted_at": (BASE + timedelta(milliseconds=100)).isoformat(),
            "submitted_quantity": submitted_quantity,
            "order_type": "market",
        },
        BASE + timedelta(milliseconds=100),
        "submitted",
    )
    running = Decimal("0")
    total = Decimal(submitted_quantity)
    for index, quantity_text in enumerate(fill_quantities, start=1):
        quantity = Decimal(quantity_text)
        running += quantity
        append_event(
            context,
            EventType.FILL_RECEIVED,
            {
                "order_id": "order-1",
                "fill_id": f"fill-{index}",
                "fill_at": (BASE + timedelta(milliseconds=200 + index)).isoformat(),
                "side": str(context.root.payload["observation"]["side"]),
                "fill_quantity": format(quantity, "f"),
                "fill_price": str(100 + index),
                "cumulative_filled_quantity": format(running, "f"),
                "unfilled_quantity": format(total - running, "f"),
                "actual_fee": "1" if index == 1 else "0",
                "actual_slippage": "10" if index == 1 else "0",
                "implementation_shortfall": "10" if index == 1 else "0",
            },
            BASE + timedelta(milliseconds=200 + index),
            f"fill-{index}",
        )
    append_event(
        context,
        EventType.ORDER_TERMINAL,
        {
            "order_id": "order-1",
            "terminal_at": (BASE + timedelta(milliseconds=500)).isoformat(),
            "terminal_state": terminal_state,
            "reason_code": (
                "FILLED" if terminal_state == "FULLY_FILLED" else "EXPIRE_UNFILLED_REMAINDER"
            ),
            "executed_quantity": format(running, "f"),
            "unfilled_quantity": terminal_unfilled,
            "engine_status": "filled" if terminal_state == "FULLY_FILLED" else "expired",
            "cancellation_fee": None,
        },
        BASE + timedelta(milliseconds=500),
        "terminal",
    )
    append_event(
        context,
        EventType.EXECUTION_EVALUATED,
        {
            "evaluation_version": "phase8-step3-execution-evaluation-v1",
            "execution_outcome": (
                "FULLY_FILLED" if terminal_state == "FULLY_FILLED" else "PARTIALLY_FILLED_EXPIRED"
            ),
            "realised_decision_latency_ms": "100",
            "realised_submission_latency_ms": "100",
            "realised_acknowledgement_latency_ms": "100" if include_ack_latency else None,
            "realised_first_fill_latency_ms": "101",
            "realised_total_latency_ms": "201",
            "actual_fees": "1",
            "actual_slippage": "10",
            "implementation_shortfall": "10",
            "cancellation_fee": None,
            "realised_execution_cost": "11",
            "executed_quantity": format(running, "f"),
            "unfilled_quantity": terminal_unfilled,
            "lifecycle_audit": {"valid": True, "issues": []},
        },
        BASE + timedelta(milliseconds=501),
        "evaluation",
    )


def add_no_fill(context: OutcomeContext) -> None:
    append_event(
        context,
        EventType.ORDER_SUBMITTED,
        {
            "order_id": "order-1",
            "client_order_id": "client-1",
            "submitted_at": (BASE + timedelta(milliseconds=100)).isoformat(),
            "submitted_quantity": "10",
            "order_type": "limit",
        },
        BASE + timedelta(milliseconds=100),
        "submitted",
    )
    append_event(
        context,
        EventType.ORDER_TERMINAL,
        {
            "order_id": "order-1",
            "terminal_at": (BASE + timedelta(seconds=2)).isoformat(),
            "terminal_state": "EXPIRED_UNFILLED",
            "reason_code": "EXPIRE_UNFILLED_REMAINDER",
            "executed_quantity": "0",
            "unfilled_quantity": "10",
            "engine_status": "expired",
            "cancellation_fee": None,
        },
        BASE + timedelta(seconds=2),
        "terminal",
    )
    append_event(
        context,
        EventType.EXECUTION_EVALUATED,
        {
            "evaluation_version": "phase8-step3-execution-evaluation-v1",
            "execution_outcome": "NO_FILL",
            "realised_execution_cost": "0",
            "executed_quantity": "0",
            "unfilled_quantity": "10",
            "lifecycle_audit": {"valid": True, "issues": []},
        },
        BASE + timedelta(seconds=2, milliseconds=1),
        "evaluation",
    )


def horizon_reference(*, price: str = "110", query: str = "query-1") -> ReferencePriceEvidence:
    target = BASE + timedelta(seconds=60)
    return ReferencePriceEvidence(
        symbol="BTC/USD",
        price=Decimal(price),
        observed_at=target,
        source_capture_at=target + timedelta(milliseconds=1),
        target_at=target,
        reference_convention="next_open",
        provider="test-provider",
        feed="point-in-time",
        dataset_version="dataset-v1",
        source_event_id=f"bar-{query}",
        query_fingerprint=query,
    )


def fixed_trigger(*, price: str = "110", query: str = "query-1") -> OutcomeTrigger:
    return OutcomeTrigger(
        trigger_id=f"trigger-{query}",
        trigger_type=OutcomeTriggerType.FIXED_HORIZON,
        triggered_at=BASE + timedelta(seconds=61),
        reference_price=horizon_reference(price=price, query=query),
    )


def test_outcome_is_evaluated_exactly_once_and_duplicate_scheduler_is_idempotent() -> None:
    context = build_context()
    add_execution(context)
    trigger = fixed_trigger()

    first = context.service.evaluate(context.root.decision_id, trigger)
    second = context.service.evaluate(context.root.decision_id, trigger)

    assert first.appended is True
    assert second.appended is False
    events = context.ledger.read(context.root.decision_id)
    assert sum(event.event_type is EventType.OUTCOME_EVALUATED for event in events) == 1


def test_frozen_horizon_boundary_and_deterministic_provider_request() -> None:
    context = build_context()
    provider = StaticProvider(horizon_reference())
    context.clock.value = BASE + timedelta(seconds=59, milliseconds=999)

    assert context.service.evaluate_due(
        context.root.decision_id, provider=provider, dataset_version="dataset-v1"
    ) is None
    assert provider.calls == 0

    context.clock.value = BASE + timedelta(seconds=60, milliseconds=1)
    result = context.service.evaluate_due(
        context.root.decision_id, provider=provider, dataset_version="dataset-v1"
    )
    assert result is not None and result.appended is True
    assert provider.last_request is not None
    assert provider.last_request.horizon_value == 60
    assert provider.last_request.reference_convention == "next_open"


def test_executed_outcome_separates_post_fill_pnl_from_execution_quality_cost() -> None:
    context = build_context()
    add_execution(context)

    context.service.evaluate(context.root.decision_id, fixed_trigger())

    outcome = context.ledger.read(context.root.decision_id)[-1].payload
    realised = outcome["realised_strategy_outcome"]
    assert outcome["outcome_type"] == OutcomeType.REALISED_EXECUTED.value
    assert realised["gross_outcome_amount"] == "90"
    assert realised["actual_entry_execution_quality_cost"] == "11"
    assert realised["actual_entry_fees_and_cancellation"] == "1"
    assert realised["net_outcome_amount"] == "89"
    assert outcome["cost_convention"]["diagnostic_costs_never_enter_realised_pnl"] is True


def test_actual_exit_path_uses_actual_exit_identity_quantity_and_cost() -> None:
    context = build_context()
    add_execution(context)
    exit_at = BASE + timedelta(seconds=30)
    reference = ReferencePriceEvidence(
        symbol="BTC/USD",
        price=Decimal("105"),
        observed_at=exit_at,
        source_capture_at=exit_at,
        target_at=exit_at,
        reference_convention="actual_exit_fill",
        provider="strategy-exit",
        feed="actual-fill",
        dataset_version="dataset-v1",
        source_event_id="exit-fill-1",
        query_fingerprint="exit-query-1",
    )
    trigger = OutcomeTrigger(
        trigger_id="actual-exit-trigger",
        trigger_type=OutcomeTriggerType.ACTUAL_STRATEGY_EXIT,
        triggered_at=exit_at,
        reference_price=reference,
        actual_exit_id="exit-1",
        actual_exit_quantity=Decimal("10"),
        actual_exit_cost=Decimal("2"),
    )

    context.service.evaluate(context.root.decision_id, trigger)

    outcome = context.ledger.read(context.root.decision_id)[-1].payload
    assert outcome["evaluation_trigger"]["actual_exit_id"] == "exit-1"
    assert outcome["realised_strategy_outcome"]["outcome_basis"] == "actual_exit_return"
    assert outcome["realised_strategy_outcome"]["net_outcome_amount"] == "37"


def test_partial_fill_outcome_uses_only_actual_executed_quantity() -> None:
    context = build_context()
    add_execution(
        context,
        fill_quantities=("4",),
        terminal_state="PARTIALLY_FILLED_EXPIRED",
        terminal_unfilled="6",
    )

    context.service.evaluate(context.root.decision_id, fixed_trigger())

    outcome = context.ledger.read(context.root.decision_id)[-1].payload
    assert outcome["quantity_basis"]["executed_quantity"] == "4"
    assert outcome["quantity_basis"]["unfilled_quantity"] == "6"
    assert outcome["quantity_basis"]["partial_fill"] is True
    assert outcome["realised_strategy_outcome"]["gross_outcome_amount"] == "36"


def test_no_fill_outcome_keeps_realised_values_null_and_labels_unfilled_diagnostic() -> None:
    context = build_context()
    add_no_fill(context)

    context.service.evaluate(context.root.decision_id, fixed_trigger())

    outcome = context.ledger.read(context.root.decision_id)[-1].payload
    assert outcome["outcome_type"] == OutcomeType.NO_REALISED_EXECUTION.value
    assert outcome["counterfactual_type"] == CounterfactualType.APPROVED_UNFILLED.value
    assert outcome["realised_strategy_outcome"]["net_outcome_bps"] is None
    assert Decimal(outcome["simulated_diagnostic"]["simulated_net_outcome_bps"]) == Decimal("990")
    assert "not rejected" in outcome["counterfactual_label"]
    assert outcome["is_realised_trade"] is False


@pytest.mark.parametrize("decision", ["REJECT", "DEFER"])
def test_rejected_and_deferred_counterfactuals_are_explicitly_simulated(decision: str) -> None:
    context = build_context(decision=decision)

    context.service.evaluate(context.root.decision_id, fixed_trigger())

    summary = ResearchDecisionSummaryProjector().replay(context.ledger.read(context.root.decision_id))
    outcome = summary.diagnostic_counterfactuals[0]
    assert outcome["counterfactual_type"] == CounterfactualType.REJECTED_SIGNAL.value
    assert outcome["outcome_type"] == OutcomeType.SIMULATED_DIAGNOSTIC.value
    assert outcome["simulated_diagnostic"]["not_actual_execution"] is True
    assert outcome["simulated_diagnostic"]["not_actual_fill"] is True
    assert outcome["simulated_diagnostic"]["not_realised_trade"] is True
    assert summary.strategy_outcome is None
    assert summary.realised_net_outcome_bps is None


def test_forecast_is_immutable_and_missing_acknowledgement_stays_missing() -> None:
    context = build_context()
    add_execution(context, include_ack_latency=False)
    original = context.root.to_dict()

    context.service.evaluate(context.root.decision_id, fixed_trigger())

    assert context.ledger.read(context.root.decision_id)[0].to_dict() == original
    outcome = context.ledger.read(context.root.decision_id)[-1].payload
    latency = outcome["forecast_versus_reality"]["latency"]
    assert "acknowledgement" in latency["missing_components"]
    assert outcome["original_forecast_rewritten"] is False


def test_conflicting_duplicate_trigger_cannot_replace_frozen_outcome() -> None:
    context = build_context()
    add_execution(context)
    context.service.evaluate(context.root.decision_id, fixed_trigger())

    with pytest.raises(OutcomeConflictError):
        context.service.evaluate(
            context.root.decision_id,
            fixed_trigger(price="111", query="different-query"),
        )


def test_provider_dataset_drift_is_rejected() -> None:
    context = build_context()
    provider = StaticProvider(horizon_reference())

    with pytest.raises(OutcomeEvidenceError, match="dataset version"):
        context.service.evaluate_due(
            context.root.decision_id,
            provider=provider,
            dataset_version="different-dataset",
        )


def test_existing_outcome_cannot_be_reinterpreted_under_a_different_policy() -> None:
    context = build_context()
    add_execution(context)
    trigger = fixed_trigger()
    context.service.evaluate(context.root.decision_id, trigger)
    changed_policy_service = OutcomeEvaluationService(
        policy=Step4OutcomePolicy(
            preregistered_at=BASE - timedelta(days=1),
            methodology_version="different-outcome-method",
        ),
        ledger=context.ledger,
        clock=context.clock,
        clock_source="test-clock",
    )

    with pytest.raises(OutcomeConflictError, match="different frozen evaluation policy"):
        changed_policy_service.evaluate(context.root.decision_id, trigger)


def test_actual_exit_retry_cannot_change_cost_evidence() -> None:
    context = build_context()
    add_execution(context)
    exit_at = BASE + timedelta(seconds=30)
    reference = ReferencePriceEvidence(
        symbol="BTC/USD",
        price=Decimal("105"),
        observed_at=exit_at,
        source_capture_at=exit_at,
        target_at=exit_at,
        reference_convention="actual_exit_fill",
        provider="strategy-exit",
        feed="actual-fill",
        dataset_version="dataset-v1",
        source_event_id="exit-fill-1",
        query_fingerprint="exit-query-1",
    )
    first = OutcomeTrigger(
        trigger_id="actual-exit-trigger",
        trigger_type=OutcomeTriggerType.ACTUAL_STRATEGY_EXIT,
        triggered_at=exit_at,
        reference_price=reference,
        actual_exit_id="exit-1",
        actual_exit_quantity=Decimal("10"),
        actual_exit_cost=Decimal("2"),
    )
    changed = OutcomeTrigger(
        trigger_id="actual-exit-trigger",
        trigger_type=OutcomeTriggerType.ACTUAL_STRATEGY_EXIT,
        triggered_at=exit_at,
        reference_price=reference,
        actual_exit_id="exit-1",
        actual_exit_quantity=Decimal("10"),
        actual_exit_cost=Decimal("3"),
    )
    context.service.evaluate(context.root.decision_id, first)

    with pytest.raises(OutcomeConflictError, match="quantity or cost drift"):
        context.service.evaluate(context.root.decision_id, changed)


def test_actual_exit_is_forbidden_for_rejected_signal() -> None:
    context = build_context(decision="REJECT")
    exit_at = BASE + timedelta(seconds=30)
    reference = ReferencePriceEvidence(
        symbol="BTC/USD",
        price=Decimal("105"),
        observed_at=exit_at,
        source_capture_at=exit_at,
        target_at=exit_at,
        reference_convention="actual_exit_fill",
        provider="test",
        feed="test",
        dataset_version="dataset-v1",
        source_event_id="exit",
        query_fingerprint="exit",
    )
    trigger = OutcomeTrigger(
        trigger_id="invalid-exit",
        trigger_type=OutcomeTriggerType.ACTUAL_STRATEGY_EXIT,
        triggered_at=exit_at,
        reference_price=reference,
        actual_exit_id="exit-1",
        actual_exit_quantity=Decimal("1"),
    )

    with pytest.raises(OutcomeEvidenceError):
        context.service.evaluate(context.root.decision_id, trigger)


def test_projection_rebuild_is_deterministic_and_fill_events_do_not_create_opportunities() -> None:
    context = build_context()
    add_execution(context, fill_quantities=("4", "6"))
    context.service.evaluate(context.root.decision_id, fixed_trigger())
    events = context.ledger.read(context.root.decision_id)
    projector = ResearchDecisionSummaryProjector()

    first = projector.replay(events)
    second = projector.replay(events)

    assert first.to_dict() == second.to_dict()
    assert first.execution_state["fill_event_count"] == 2
    assert first.opportunity_key == second.opportunity_key
    assert first.executed is True
