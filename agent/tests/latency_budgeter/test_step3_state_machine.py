"""State-machine style invariants over reordered and concurrent callbacks."""

from __future__ import annotations

import itertools
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

from src.latency_budgeter.domain.events import EventType
from src.latency_budgeter.domain.lifecycle import (
    FillObservation,
    TerminalObservation,
    TerminalReason,
    TerminalState,
)

from .test_step3_lifecycle import BASE, build_context, submission


def test_all_fill_ingestion_permutations_reconstruct_same_conserved_state() -> None:
    rows = (
        ("fill-1", 300, "1", "1", "2"),
        ("fill-2", 350, "1", "2", "1"),
        ("fill-3", 400, "1", "3", "0"),
    )
    for permutation in itertools.permutations(rows):
        ctx = build_context()
        authorization, _, _ = submission(ctx, quantity="3")
        for fill_id, milliseconds, quantity, cumulative, unfilled in permutation:
            ctx.service.record_fill(
                authorization,
                FillObservation(
                    order_id="order-1",
                    fill_id=fill_id,
                    fill_at=BASE + timedelta(milliseconds=milliseconds),
                    side="buy",
                    quantity=Decimal(quantity),
                    price=Decimal("100.123456789"),
                    cumulative_filled_quantity=Decimal(cumulative),
                    unfilled_quantity=Decimal(unfilled),
                    decision_price=Decimal("100"),
                    fee=Decimal("0.01"),
                ),
            )
        ctx.service.record_terminal(
            authorization,
            TerminalObservation(
                order_id="order-1",
                terminal_at=BASE + timedelta(milliseconds=450),
                terminal_state=TerminalState.FULLY_FILLED,
                reason_code=TerminalReason.FILLED,
                executed_quantity=Decimal("3"),
                unfilled_quantity=Decimal("0"),
                engine_status="filled",
            ),
        )
        projection = ctx.service.projection(ctx.root.decision_id)
        assert [fill.fill_id for fill in projection.fills] == ["fill-1", "fill-2", "fill-3"]
        assert projection.executed_quantity == Decimal("3")
        assert projection.unfilled_quantity == Decimal("0")
        assert projection.issues == ()
        assert projection.latest_execution_evaluation.payload["lifecycle_audit"]["valid"] is True


def test_concurrent_identical_terminal_callbacks_reach_terminal_once() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="0.5")
    fill = FillObservation(
        order_id="order-1",
        fill_id="fill-1",
        fill_at=BASE + timedelta(milliseconds=300),
        side="buy",
        quantity=Decimal("0.5"),
        price=Decimal("100"),
        cumulative_filled_quantity=Decimal("0.5"),
        unfilled_quantity=Decimal("0"),
        decision_price=Decimal("100"),
        fee=Decimal("0"),
    )
    ctx.service.record_fill(authorization, fill)
    terminal = TerminalObservation(
        order_id="order-1",
        terminal_at=BASE + timedelta(milliseconds=350),
        terminal_state=TerminalState.FULLY_FILLED,
        reason_code=TerminalReason.FILLED,
        executed_quantity=Decimal("0.5"),
        unfilled_quantity=Decimal("0"),
        engine_status="filled",
    )

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: ctx.service.record_terminal(authorization, terminal), range(20)))

    stream = ctx.ledger.read(ctx.root.decision_id)
    assert sum(result.appended for result in results) == 1
    assert sum(event.event_type is EventType.ORDER_TERMINAL for event in stream) == 1
    assert sum(event.event_type is EventType.EXECUTION_EVALUATED for event in stream) == 1


def test_conflicting_terminal_retry_is_evidence_not_history_rewrite() -> None:
    ctx = build_context()
    authorization, _, _ = submission(ctx, quantity="1")
    first = TerminalObservation(
        order_id="order-1",
        terminal_at=BASE + timedelta(milliseconds=400),
        terminal_state=TerminalState.EXPIRED_UNFILLED,
        reason_code=TerminalReason.EXPIRE_UNFILLED_REMAINDER,
        executed_quantity=Decimal("0"),
        unfilled_quantity=Decimal("1"),
        engine_status="expired",
    )
    conflict = TerminalObservation(
        order_id="order-1",
        terminal_at=BASE + timedelta(milliseconds=401),
        terminal_state=TerminalState.CANCELLED_UNFILLED,
        reason_code=TerminalReason.CANCEL_UNFILLED_REMAINDER,
        executed_quantity=Decimal("0"),
        unfilled_quantity=Decimal("1"),
        engine_status="cancelled",
    )

    ctx.service.record_terminal(authorization, first)
    ctx.service.record_terminal(authorization, conflict)

    stream = ctx.ledger.read(ctx.root.decision_id)
    assert sum(event.event_type is EventType.ORDER_TERMINAL for event in stream) == 1
    assert any(event.event_type is EventType.LIFECYCLE_INTEGRITY_FAILURE for event in stream)
    assert stream[0].payload["decision"] == "ALLOW"

