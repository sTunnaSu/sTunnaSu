"""Adversarial event-lifecycle tests for Phase 8 Step 3."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.latency_budgeter.application.lifecycle import ExecutionLifecycleService
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.errors import (
    ExecutionBlockedError,
    LifecycleConflictError,
)
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.history import HistoryQuery, LatencyComponent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.lifecycle import (
    AcknowledgementAvailability,
    AcknowledgementObservation,
    ExecutionOutcome,
    FillObservation,
    SubmissionObservation,
    TerminalObservation,
    TerminalReason,
    TerminalState,
)
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.memory import InMemoryEventLedger

BASE = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class MutableClock:
    value: datetime = BASE + timedelta(seconds=10)

    def __call__(self) -> datetime:
        return self.value


@dataclass
class Context:
    config: LatencyBudgetConfig
    ledger: InMemoryEventLedger
    history: InMemoryLatencyHistoryStore
    clock: MutableClock
    service: ExecutionLifecycleService
    root: LedgerEvent


def build_context(
    decision: str = "ALLOW",
    *,
    observed_at: datetime = BASE,
    decision_at: datetime = BASE + timedelta(milliseconds=100),
    side: str = "buy",
) -> Context:
    config = LatencyBudgetConfig(
        enabled=True,
        minimum_prior_samples=1,
        rolling_history_window=10,
        cold_start_policy="fallback_p90",
        fallback_p90_latency_ms=10,
    )
    ledger = InMemoryEventLedger()
    history = InMemoryLatencyHistoryStore()
    clock = MutableClock()
    factory = Phase8IdentifierFactory()
    run_id = factory.new_run_id()
    signal_id = factory.signal_id(
        run_id=run_id,
        observation_fingerprint="a" * 64,
        strategy_version="strategy-v1",
        side=side,
        signal_key="step3",
    )
    decision_id = factory.decision_id(
        run_id=run_id,
        signal_id=signal_id,
        config_version=config.config_version,
    )
    economics = {"frozen_test_economics": True, "net_edge_bps": 12.5}
    approved = (
        {
            "decision_id": decision_id,
            "run_id": run_id,
            "signal_id": signal_id,
            "symbol": "BTC/USD",
            "side": side,
            "decision_at": decision_at.isoformat(),
            "economics": economics,
        }
        if decision == "ALLOW"
        else None
    )
    root = LedgerEvent.create(
        event_type=EventType.DECISION_CREATED,
        occurred_at=decision_at,
        recorded_at=decision_at,
        decision_id=decision_id,
        run_id=run_id,
        signal_id=signal_id,
        idempotency_key=f"decision_created:{decision_id}",
        payload={
            "decision": decision,
            "reason_code": "TEST",
            "decision_at": decision_at.isoformat(),
            "observation": {
                "observed_at": observed_at.isoformat(),
                "symbol": "BTC/USD",
                "side": side,
            },
            "approved_opportunity": approved,
            "decision_economics": economics if decision == "ALLOW" else None,
        },
    )
    root = ledger.append(root, expected_version=0).event
    service = ExecutionLifecycleService(
        config=config,
        ledger=ledger,
        history=history,
        clock=clock,
        clock_source="test-clock",
    )
    return Context(config, ledger, history, clock, service, root)


def submission(
    ctx: Context,
    *,
    order_id: str = "order-1",
    submitted_at: datetime = BASE + timedelta(milliseconds=200),
    quantity: str = "10.00000001",
    expiry_at: datetime | None = BASE + timedelta(milliseconds=600),
) -> tuple:
    authorization = ctx.service.authorize(ctx.root.decision_id)
    observation = SubmissionObservation(
        order_id=order_id,
        client_order_id=authorization.client_order_id,
        submitted_at=submitted_at,
        quantity=Decimal(quantity),
        order_type="limit",
        limit_price=Decimal("100.00000001"),
        participation_limit=Decimal("0.10"),
        expiry_at=expiry_at,
        venue_reference="venue-order-1",
        timestamp_source="test-adapter",
    )
    result = ctx.service.record_submission(authorization, observation)
    return authorization, observation, result


def fill(
    ctx: Context,
    authorization,
    *,
    fill_id: str,
    fill_at: datetime,
    quantity: str,
    cumulative: str,
    unfilled: str,
    price: str = "101.00000001",
    fee: str | None = "0.10",
    slippage: str | None = "0.20",
) -> FillObservation:
    observation = FillObservation(
        order_id="order-1",
        fill_id=fill_id,
        fill_at=fill_at,
        side="buy",
        quantity=Decimal(quantity),
        price=Decimal(price),
        cumulative_filled_quantity=Decimal(cumulative),
        unfilled_quantity=Decimal(unfilled),
        decision_price=Decimal("100.00000001"),
        fee=Decimal(fee) if fee is not None else None,
        slippage_cost=Decimal(slippage) if slippage is not None else None,
        timestamp_source="test-fill",
    )
    ctx.service.record_fill(authorization, observation)
    return observation


def terminal(
    ctx: Context,
    authorization,
    *,
    terminal_at: datetime = BASE + timedelta(milliseconds=600),
    state: TerminalState = TerminalState.FULLY_FILLED,
    executed: str = "10.00000001",
    unfilled: str = "0",
    cancellation_fee: str | None = None,
    acknowledgement: AcknowledgementAvailability = AcknowledgementAvailability.UNSUPPORTED,
) -> None:
    reason = TerminalReason.FILLED if state is TerminalState.FULLY_FILLED else TerminalReason.EXPIRE_UNFILLED_REMAINDER
    ctx.service.record_terminal(
        authorization,
        TerminalObservation(
            order_id="order-1",
            terminal_at=terminal_at,
            terminal_state=state,
            reason_code=reason,
            executed_quantity=Decimal(executed),
            unfilled_quantity=Decimal(unfilled),
            engine_status="filled" if state is TerminalState.FULLY_FILLED else "expired",
            cancellation_fee=Decimal(cancellation_fee) if cancellation_fee is not None else None,
            acknowledgement_availability=acknowledgement,
        ),
    )


def history_window(ctx: Context, component: LatencyComponent):
    return ctx.history.prior_window(
        HistoryQuery(
            component=component,
            decision_at=ctx.clock.value + timedelta(seconds=1),
            current_decision_id="future-decision",
            rolling_window=10,
            component_definition_version=ctx.config.component_definition_version,
            estimator_schema_version=ctx.config.estimator_schema_version,
        )
    )


def event_types(ctx: Context) -> list[EventType]:
    return [event.event_type for event in ctx.ledger.read(ctx.root.decision_id)]


def test_valid_submission_is_actual_idempotent_and_releases_component() -> None:
    ctx = build_context()
    authorization, observation, first = submission(ctx)

    second = ctx.service.record_submission(authorization, observation)

    assert first.appended is True
    assert second.appended is False
    assert event_types(ctx).count(EventType.ORDER_SUBMITTED) == 1
    submitted = ctx.service.projection(ctx.root.decision_id).submission
    assert submitted is not None
    assert submitted.payload["submitted_quantity"] == "10.00000001"
    assert submitted.payload["component_validation"]["status"] == "VALID"
    samples = history_window(ctx, LatencyComponent.SUBMISSION).samples
    assert len(samples) == 1
    assert samples[0].order_id == "order-1"
    assert samples[0].component_available_at == observation.submitted_at


def test_duplicate_submission_with_changed_order_is_a_conflict() -> None:
    ctx = build_context()
    authorization, observation, _ = submission(ctx)
    changed = SubmissionObservation(
        order_id="order-2",
        client_order_id=authorization.client_order_id,
        submitted_at=observation.submitted_at,
        quantity=observation.quantity,
        order_type="market",
    )

    with pytest.raises(LifecycleConflictError):
        ctx.service.record_submission(authorization, changed)


@pytest.mark.parametrize("decision", ["REJECT", "DEFER"])
def test_rejected_and_deferred_decisions_are_blocked(decision: str) -> None:
    ctx = build_context(decision)

    with pytest.raises(ExecutionBlockedError):
        ctx.service.authorize(ctx.root.decision_id)

    assert event_types(ctx) == [EventType.DECISION_CREATED]


def test_valid_acknowledgement_releases_real_component() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx)
    ack = AcknowledgementObservation(
        order_id="order-1",
        acknowledgement_id="ack-1",
        acknowledged_at=BASE + timedelta(milliseconds=250),
        venue_reference="venue-order-1",
    )

    first = ctx.service.record_acknowledgement(authorization, ack)
    second = ctx.service.record_acknowledgement(authorization, ack)

    assert first.appended is True and second.appended is False
    assert event_types(ctx).count(EventType.BROKER_ACKNOWLEDGED) == 1
    assert len(history_window(ctx, LatencyComponent.ACKNOWLEDGEMENT).samples) == 1


def test_later_callback_reconciliation_keeps_existing_sample_idempotent() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="1")
    ctx.service.record_acknowledgement(
        authorization,
        AcknowledgementObservation(
            order_id="order-1",
            acknowledgement_id="ack-stable-history",
            acknowledged_at=BASE + timedelta(milliseconds=250),
        ),
    )
    ctx.clock.value += timedelta(seconds=1)

    fill(
        ctx,
        authorization,
        fill_id="fill-after-clock-advance",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="1",
        cumulative="1",
        unfilled="0",
    )

    assert len(history_window(ctx, LatencyComponent.SUBMISSION).samples) == 1
    assert len(history_window(ctx, LatencyComponent.ACKNOWLEDGEMENT).samples) == 1
    assert len(history_window(ctx, LatencyComponent.FILL).samples) == 1


def test_acknowledgement_ingested_after_fill_uses_event_time_without_reordering_ledger() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="1")
    fill(
        ctx,
        authorization,
        fill_id="fill-before-ack-arrival",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="1",
        cumulative="1",
        unfilled="0",
    )
    ctx.service.record_acknowledgement(
        authorization,
        AcknowledgementObservation(
            order_id="order-1",
            acknowledgement_id="late-arriving-ack",
            acknowledged_at=BASE + timedelta(milliseconds=250),
        ),
    )
    terminal(ctx, authorization, executed="1")

    stream = ctx.ledger.read(ctx.root.decision_id)
    fill_index = next(index for index, event in enumerate(stream) if event.event_type is EventType.FILL_RECEIVED)
    ack_index = next(index for index, event in enumerate(stream) if event.event_type is EventType.BROKER_ACKNOWLEDGED)
    assert fill_index < ack_index
    assert (
        ctx.service.projection(ctx.root.decision_id).latest_execution_evaluation.payload["lifecycle_audit"]["valid"]
        is True
    )
    assert len(history_window(ctx, LatencyComponent.ACKNOWLEDGEMENT).samples) == 1


def test_unsupported_acknowledgement_is_not_fabricated() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx)
    terminal(
        ctx,
        authorization,
        state=TerminalState.EXPIRED_UNFILLED,
        executed="0",
        unfilled="10.00000001",
    )

    assert EventType.BROKER_ACKNOWLEDGED not in event_types(ctx)
    terminal_event = ctx.service.projection(ctx.root.decision_id).terminal
    assert terminal_event is not None
    assert terminal_event.payload["acknowledgement_availability"] == "UNSUPPORTED"
    assert history_window(ctx, LatencyComponent.ACKNOWLEDGEMENT).samples == ()


def test_ack_before_submission_is_retained_but_excluded() -> None:
    ctx = build_context()
    authorization = ctx.service.authorize(ctx.root.decision_id)
    ctx.service.record_acknowledgement(
        authorization,
        AcknowledgementObservation(
            order_id="order-1",
            acknowledgement_id="ack-early",
            acknowledged_at=BASE + timedelta(milliseconds=150),
        ),
    )
    submission(ctx, submitted_at=BASE + timedelta(milliseconds=200))
    terminal(
        ctx,
        authorization,
        state=TerminalState.EXPIRED_UNFILLED,
        executed="0",
        unfilled="10.00000001",
    )

    assert event_types(ctx).count(EventType.BROKER_ACKNOWLEDGED) == 1
    assert history_window(ctx, LatencyComponent.ACKNOWLEDGEMENT).samples == ()
    assert EventType.LIFECYCLE_INTEGRITY_FAILURE in event_types(ctx)


def test_valid_first_fill_releases_at_fill_time_with_exact_price() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx)
    fill(
        ctx,
        authorization,
        fill_id="fill-1",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="4.00000000",
        cumulative="4.00000000",
        unfilled="6.00000001",
        price="101.123456789012345678",
    )

    projection = ctx.service.projection(ctx.root.decision_id)
    assert projection.first_fill is not None
    assert projection.first_fill.price == Decimal("101.123456789012345678")
    samples = history_window(ctx, LatencyComponent.FILL).samples
    assert len(samples) == 1
    assert samples[0].component_available_at == BASE + timedelta(milliseconds=300)


def test_fill_before_submission_is_preserved_and_component_excluded() -> None:
    ctx = build_context()
    authorization = ctx.service.authorize(ctx.root.decision_id)
    fill(
        ctx,
        authorization,
        fill_id="fill-early",
        fill_at=BASE + timedelta(milliseconds=150),
        quantity="10.00000001",
        cumulative="10.00000001",
        unfilled="0",
    )
    submission(ctx, submitted_at=BASE + timedelta(milliseconds=200))
    terminal(ctx, authorization)

    assert event_types(ctx).count(EventType.FILL_RECEIVED) == 1
    assert history_window(ctx, LatencyComponent.FILL).samples == ()
    assert EventType.LIFECYCLE_INTEGRITY_FAILURE in event_types(ctx)


def test_multiple_out_of_order_partial_fills_are_event_time_reconstructed() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="10")
    fill(
        ctx,
        authorization,
        fill_id="fill-later",
        fill_at=BASE + timedelta(milliseconds=400),
        quantity="5",
        cumulative="10",
        unfilled="0",
    )
    fill(
        ctx,
        authorization,
        fill_id="fill-earlier",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="5",
        cumulative="5",
        unfilled="5",
    )
    terminal(ctx, authorization, executed="10", unfilled="0")

    projection = ctx.service.projection(ctx.root.decision_id)
    assert [item.fill_id for item in projection.fills] == ["fill-earlier", "fill-later"]
    assert projection.executed_quantity == Decimal("10")
    assert projection.issues == ()
    first_samples = history_window(ctx, LatencyComponent.FILL).samples
    assert len(first_samples) == 1
    assert first_samples[0].source_event_id == projection.fills[0].event.event_id
    final_samples = history_window(ctx, LatencyComponent.FINAL_FILL).samples
    assert len(final_samples) == 1
    assert final_samples[0].source_event_id == projection.fills[-1].event.event_id


def test_duplicate_fill_does_not_double_count_and_conflict_is_rejected() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="2")
    original = fill(
        ctx,
        authorization,
        fill_id="fill-1",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="2",
        cumulative="2",
        unfilled="0",
    )
    duplicate = ctx.service.record_fill(authorization, original)
    changed = FillObservation(
        order_id="order-1",
        fill_id="fill-1",
        fill_at=original.fill_at,
        side="buy",
        quantity=Decimal("1"),
        price=original.price,
        cumulative_filled_quantity=Decimal("1"),
        unfilled_quantity=Decimal("1"),
    )

    assert duplicate.appended is False
    assert ctx.service.projection(ctx.root.decision_id).executed_quantity == Decimal("2")
    with pytest.raises(LifecycleConflictError):
        ctx.service.record_fill(authorization, changed)


def test_fully_filled_terminal_and_execution_evaluation_are_separate() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="2")
    fill(
        ctx,
        authorization,
        fill_id="fill-full",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="2",
        cumulative="2",
        unfilled="0",
    )
    frozen_root = ctx.root.to_dict()
    terminal(ctx, authorization, terminal_at=BASE + timedelta(milliseconds=350), executed="2")

    stream = ctx.ledger.read(ctx.root.decision_id)
    terminal_index = next(index for index, event in enumerate(stream) if event.event_type is EventType.ORDER_TERMINAL)
    evaluation_index = next(
        index for index, event in enumerate(stream) if event.event_type is EventType.EXECUTION_EVALUATED
    )
    assert terminal_index < evaluation_index
    evaluation = ctx.service.projection(ctx.root.decision_id).latest_execution_evaluation
    assert evaluation is not None
    assert evaluation.payload["execution_outcome"] == ExecutionOutcome.FULLY_FILLED.value
    assert evaluation.payload["realised_trade_pnl"] is None
    assert evaluation.payload["strategy_outcome_calculated"] is False
    assert evaluation.payload["actual_fees"] == "0.10"
    assert Decimal(evaluation.payload["realised_execution_cost"]) == Decimal("2.10")
    assert ctx.ledger.read(ctx.root.decision_id)[0].to_dict() == frozen_root


def test_sell_fill_has_signed_quantity_and_side_aware_shortfall() -> None:
    ctx = build_context(side="sell")
    authorization, _, _ = submission(ctx, quantity="1")
    ctx.service.record_fill(
        authorization,
        FillObservation(
            order_id="order-1",
            fill_id="sell-fill",
            fill_at=BASE + timedelta(milliseconds=300),
            side="sell",
            quantity=Decimal("1"),
            price=Decimal("99"),
            cumulative_filled_quantity=Decimal("1"),
            unfilled_quantity=Decimal("0"),
            decision_price=Decimal("100"),
            fee=Decimal("0"),
        ),
    )
    terminal(ctx, authorization, executed="1")

    projection = ctx.service.projection(ctx.root.decision_id)
    assert projection.fills[0].event.payload["signed_fill_quantity"] == "-1"
    assert projection.latest_execution_evaluation.payload["implementation_shortfall"] == "1"


def test_partial_fill_expiry_preserves_fill_and_cancels_only_remainder() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="10")
    fill(
        ctx,
        authorization,
        fill_id="fill-partial",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="4",
        cumulative="4",
        unfilled="6",
    )
    terminal(
        ctx,
        authorization,
        state=TerminalState.PARTIALLY_FILLED_EXPIRED,
        executed="4",
        unfilled="6",
    )

    projection = ctx.service.projection(ctx.root.decision_id)
    assert projection.executed_quantity == Decimal("4")
    assert projection.unfilled_quantity == Decimal("6")
    assert projection.terminal_state is TerminalState.PARTIALLY_FILLED_EXPIRED
    assert len(projection.fills) == 1
    assert projection.latest_execution_evaluation.payload["execution_outcome"] == "PARTIALLY_FILLED_EXPIRED"


@pytest.mark.parametrize(("cancellation_fee", "expected_cost"), [(None, "0"), ("0.25", "0.25")])
def test_no_fill_expiry_has_null_pnl_and_only_actual_cancellation_fee(
    cancellation_fee: str | None,
    expected_cost: str,
) -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="10")
    terminal(
        ctx,
        authorization,
        state=TerminalState.EXPIRED_UNFILLED,
        executed="0",
        unfilled="10",
        cancellation_fee=cancellation_fee,
    )

    projection = ctx.service.projection(ctx.root.decision_id)
    terminal_event = projection.terminal
    evaluation = projection.latest_execution_evaluation
    assert terminal_event.payload["approved_but_unfilled"] is True
    assert terminal_event.payload["realised_trade_pnl"] is None
    assert evaluation.payload["execution_outcome"] == "NO_FILL"
    assert evaluation.payload["realised_first_fill_latency_ms"] is None
    assert Decimal(evaluation.payload["realised_execution_cost"]) == Decimal(expected_cost)
    assert evaluation.payload["realised_trade_pnl"] is None


def test_fill_exactly_at_expiry_is_valid_and_fully_filled() -> None:
    expiry = BASE + timedelta(milliseconds=400)
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="1", expiry_at=expiry)
    fill(
        ctx,
        authorization,
        fill_id="fill-at-expiry",
        fill_at=expiry,
        quantity="1",
        cumulative="1",
        unfilled="0",
    )
    terminal(ctx, authorization, terminal_at=expiry, executed="1")

    evaluation = ctx.service.projection(ctx.root.decision_id).latest_execution_evaluation
    assert evaluation.payload["lifecycle_audit"]["valid"] is True


def test_fill_and_cancellation_crossing_is_retained_and_audited() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="10")
    terminal(
        ctx,
        authorization,
        terminal_at=BASE + timedelta(milliseconds=500),
        state=TerminalState.EXPIRED_UNFILLED,
        executed="0",
        unfilled="10",
    )
    fill(
        ctx,
        authorization,
        fill_id="late-ingested-fill",
        fill_at=BASE + timedelta(milliseconds=499),
        quantity="10",
        cumulative="10",
        unfilled="0",
    )

    projection = ctx.service.projection(ctx.root.decision_id)
    assert len(projection.fills) == 1
    assert len(projection.execution_evaluations) == 2
    assert EventType.LIFECYCLE_INTEGRITY_FAILURE in event_types(ctx)
    assert projection.latest_execution_evaluation.payload["lifecycle_audit"]["valid"] is False


def test_terminal_chronology_failure_invalidates_only_affected_history() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, submitted_at=BASE + timedelta(milliseconds=300), quantity="10")
    assert len(history_window(ctx, LatencyComponent.SUBMISSION).samples) == 1
    terminal(
        ctx,
        authorization,
        terminal_at=BASE + timedelta(milliseconds=250),
        state=TerminalState.EXPIRED_UNFILLED,
        executed="0",
        unfilled="10",
    )

    assert history_window(ctx, LatencyComponent.SUBMISSION).samples == ()
    failure = next(
        event
        for event in ctx.ledger.read(ctx.root.decision_id)
        if event.event_type is EventType.LIFECYCLE_INTEGRITY_FAILURE
    )
    assert "submission" in failure.payload["affected_latency_components"]
    assert ctx.ledger.read(ctx.root.decision_id)[0].payload["decision"] == "ALLOW"


def test_component_is_not_available_at_equal_decision_time() -> None:
    ctx = build_context()
    _, observation, _ = submission(ctx)
    equal = ctx.history.prior_window(
        HistoryQuery(
            component=LatencyComponent.SUBMISSION,
            decision_at=observation.submitted_at,
            current_decision_id="future",
            rolling_window=10,
            component_definition_version=ctx.config.component_definition_version,
            estimator_schema_version=ctx.config.estimator_schema_version,
        )
    )
    assert equal.samples == ()


def test_concurrent_duplicate_fill_ingestion_is_exactly_once() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="2")
    observation = FillObservation(
        order_id="order-1",
        fill_id="concurrent-fill",
        fill_at=BASE + timedelta(milliseconds=300),
        side="buy",
        quantity=Decimal("2"),
        price=Decimal("101"),
        cumulative_filled_quantity=Decimal("2"),
        unfilled_quantity=Decimal("0"),
        decision_price=Decimal("100"),
        fee=Decimal("0.1"),
    )

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: ctx.service.record_fill(authorization, observation), range(30)))

    assert sum(result.appended for result in results) == 1
    assert event_types(ctx).count(EventType.FILL_RECEIVED) == 1
    assert ctx.service.projection(ctx.root.decision_id).executed_quantity == Decimal("2")


def test_projection_reconstruction_is_deterministic() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="1")
    fill(
        ctx,
        authorization,
        fill_id="fill-1",
        fill_at=BASE + timedelta(milliseconds=300),
        quantity="1",
        cumulative="1",
        unfilled="0",
    )
    terminal(ctx, authorization, executed="1")

    first = ctx.service.projection(ctx.root.decision_id)
    second = ctx.service.projection(ctx.root.decision_id)

    assert first == second
    assert first.aggregate_version == len(ctx.ledger.read(ctx.root.decision_id))
