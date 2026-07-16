"""Integration tests proving BaseEngine remains authoritative in Step 3."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd

from backtest.engines.base import BaseEngine
from backtest.models import OrderRecord
from src.latency_budgeter.adapters.base_engine import BaseEngineLifecycleAdapter
from src.latency_budgeter.domain.events import EventType
from src.latency_budgeter.domain.lifecycle import HandoffState, TerminalState

from .test_step3_lifecycle import BASE, build_context


class AdapterEngine(BaseEngine):
    def can_execute(self, symbol: str, direction: int, bar: pd.Series) -> bool:
        return True

    def round_size(self, raw_size: float, price: float) -> float:
        return float(raw_size)

    def calc_commission(self, size: float, price: float, direction: int, is_open: bool) -> float:
        return float(size) * 0.01

    def apply_slippage(self, price: float, direction: int) -> float:
        return float(price) + 0.25 * direction


class PartialAdapterEngine(AdapterEngine):
    def determine_fill_quantity(
        self,
        order: OrderRecord,
        bar: pd.Series,
        timestamp: pd.Timestamp,
    ) -> float:
        return min(Decimal("4"), Decimal(str(order.remaining_quantity or 0)))


def engine_with_adapter(ctx, *, partial: bool = False):
    engine_type = PartialAdapterEngine if partial else AdapterEngine
    engine = engine_type(
        {
            "initial_cash": 10_000,
            "order_type": "limit",
            "time_in_force": "GTC",
            "volume_participation_rate": 0.5,
        }
    )
    dates = pd.DatetimeIndex(
        [pd.Timestamp(BASE + pd.Timedelta(milliseconds=200)), pd.Timestamp(BASE + pd.Timedelta(milliseconds=500))]
    )
    engine._execution_dates = dates
    engine._bar_idx = 0
    adapter = BaseEngineLifecycleAdapter(ctx.service)
    engine.set_order_lifecycle_observer(adapter)
    return engine, adapter, dates


def create_entry(
    engine: BaseEngine,
    timestamp: pd.Timestamp,
    *,
    quantity: float = 10.0,
    expiry_bars=1,
    decision_id: str | None = None,
    signal_id: str | None = None,
):
    return engine._create_order(
        symbol="BTC/USD",
        event_type="entry",
        direction=1,
        quantity=quantity,
        timestamp=timestamp,
        decision_price=100.0,
        reason="signal",
        signal_time=timestamp - pd.Timedelta(milliseconds=100),
        order_type="limit",
        limit_price=101.0,
        time_in_force="GTC",
        expiry_bars=expiry_bars,
        phase8_decision_id=decision_id,
        phase8_signal_id=signal_id,
    )


def executable_bar() -> pd.Series:
    return pd.Series({"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 100.0})


def test_adapter_records_actual_engine_submission_fill_and_terminal() -> None:
    ctx = build_context()
    engine, adapter, dates = engine_with_adapter(ctx)
    adapter.arm(ctx.root.decision_id)

    order = create_entry(
        engine,
        dates[0],
        quantity=2,
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )
    engine._process_order(order, executable_bar(), dates[0])

    projection = ctx.service.projection(ctx.root.decision_id)
    assert order.status == "filled"
    assert projection.submission.payload["submitted_quantity"] == "2.0"
    assert projection.submission.payload["participation_limit"] == "0.5"
    assert projection.executed_quantity == Decimal("2.0")
    assert projection.terminal_state is TerminalState.FULLY_FILLED
    assert [event.event_type for event in ctx.ledger.read(ctx.root.decision_id)] == [
        EventType.DECISION_CREATED,
        EventType.ORDER_SUBMITTED,
        EventType.FILL_RECEIVED,
        EventType.ORDER_TERMINAL,
        EventType.EXECUTION_EVALUATED,
    ]
    fill_event = projection.fills[0].event
    assert fill_event.payload["actual_spread_result"] is None
    assert fill_event.payload["actual_impact_proxy"] is None
    assert fill_event.payload["actual_slippage"] is not None


def test_unarmed_entry_is_blocked_before_actual_submission() -> None:
    ctx = build_context()
    engine, _, dates = engine_with_adapter(ctx)

    order = create_entry(
        engine,
        dates[0],
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )

    assert order.status == "rejected"
    assert order.status_reason == "phase8_not_authorized"
    assert [event.event_type for event in ctx.ledger.read(ctx.root.decision_id)] == [EventType.DECISION_CREATED]


def test_risk_reducing_exit_stays_under_original_engine_authority() -> None:
    ctx = build_context()
    engine, _, dates = engine_with_adapter(ctx)

    order = engine._create_order(
        symbol="BTC/USD",
        event_type="exit",
        direction=1,
        quantity=1,
        timestamp=dates[0],
        decision_price=100,
        reason="risk_control",
    )

    assert order.status == "open"
    assert order.status_reason == ""
    assert [event.event_type for event in ctx.ledger.read(ctx.root.decision_id)] == [EventType.DECISION_CREATED]


def test_engine_partial_fill_expiry_preserves_completed_fill() -> None:
    ctx = build_context()
    engine, adapter, dates = engine_with_adapter(ctx, partial=True)
    adapter.arm(ctx.root.decision_id)
    order = create_entry(
        engine,
        dates[0],
        quantity=10,
        expiry_bars=0,
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )

    engine._process_order(order, executable_bar(), dates[0])
    assert order.status == "partially_filled"
    engine._bar_idx = 1
    engine._process_order(order, executable_bar(), dates[1])

    projection = ctx.service.projection(ctx.root.decision_id)
    assert order.status == "expired"
    assert order.filled_quantity == 4
    assert order.cancelled_quantity == 6
    assert projection.executed_quantity == Decimal("4.0")
    assert projection.unfilled_quantity == Decimal("6.0")
    assert projection.terminal_state is TerminalState.PARTIALLY_FILLED_EXPIRED


def test_engine_no_fill_expiry_does_not_fabricate_cost_or_ack() -> None:
    ctx = build_context()
    engine, adapter, dates = engine_with_adapter(ctx)
    adapter.arm(ctx.root.decision_id)
    order = create_entry(
        engine,
        dates[0],
        quantity=3,
        expiry_bars=0,
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )
    untouched = pd.Series({"open": 110.0, "high": 112.0, "low": 109.0, "close": 111.0, "volume": 100.0})

    engine._process_order(order, untouched, dates[0])
    engine._bar_idx = 1
    engine._process_order(order, untouched, dates[1])

    projection = ctx.service.projection(ctx.root.decision_id)
    assert projection.terminal_state is TerminalState.EXPIRED_UNFILLED
    assert projection.latest_execution_evaluation.payload["realised_execution_cost"] == "0"
    assert EventType.BROKER_ACKNOWLEDGED not in [
        event.event_type for event in ctx.ledger.read(ctx.root.decision_id)
    ]


def test_restart_arm_does_not_authorize_a_second_order() -> None:
    ctx = build_context()
    engine, adapter, dates = engine_with_adapter(ctx)
    adapter.arm(ctx.root.decision_id)
    first = create_entry(
        engine,
        dates[0],
        quantity=1,
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )
    assert first.status == "open"

    restarted = BaseEngineLifecycleAdapter(ctx.service)
    recovered = restarted.arm(ctx.root.decision_id)
    engine.set_order_lifecycle_observer(restarted)
    second = create_entry(
        engine,
        dates[0],
        quantity=1,
        decision_id=ctx.root.decision_id,
        signal_id=ctx.root.signal_id,
    )

    assert recovered.state is HandoffState.ALREADY_SUBMITTED
    assert second.status == "rejected"
    assert second.status_reason == "phase8_not_authorized"
    assert [event.event_type for event in ctx.ledger.read(ctx.root.decision_id)].count(
        EventType.ORDER_SUBMITTED
    ) == 1
