"""Step 2 numeric, cost, timestamp, and decay boundary tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.latency_budgeter.configuration.models import CostAssumptions, LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import LiquidityRole
from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.values import BasisPoints, Milliseconds
from src.latency_budgeter.estimation.costs import FrozenExecutionCostEstimator
from src.latency_budgeter.estimation.decay import exponential_decay
from src.latency_budgeter.policies.decision import decide_net_edge
from src.latency_budgeter.policies.timestamps import is_fresh, validate_pre_decision_timestamps


def test_costs_choose_one_fee_count_each_component_once_and_are_side_aware() -> None:
    assumptions = CostAssumptions(
        maker_fee_bps=1,
        taker_fee_bps=2,
        spread_bps=3,
        slippage_bps=4,
        impact_bps=5,
    )
    estimator = FrozenExecutionCostEstimator()

    buy = estimator.estimate(assumptions=assumptions, side=SignalSide.BUY, liquidity_role=LiquidityRole.TAKER)
    sell = estimator.estimate(assumptions=assumptions, side=SignalSide.SELL, liquidity_role=LiquidityRole.TAKER)
    maker = estimator.estimate(assumptions=assumptions, side=SignalSide.BUY, liquidity_role=LiquidityRole.MAKER)

    assert buy.total_bps == BasisPoints(14)
    assert maker.total_bps == BasisPoints(13)
    assert buy.adverse_price_adjustment_bps == BasisPoints(14)
    assert sell.adverse_price_adjustment_bps == BasisPoints(-14)


def test_strict_buffer_equality_rejects_and_fixed_precision_preserves_near_threshold() -> None:
    equality = decide_net_edge(BasisPoints("3.0000000004"), BasisPoints(3))
    above = decide_net_edge(BasisPoints("3.0000000006"), BasisPoints(3))

    assert equality[0].value == "REJECT"
    assert above[0].value == "ALLOW"


@pytest.mark.parametrize("tau", [0, -1])
def test_tau_zero_or_negative_is_rejected_by_frozen_config(tau: int) -> None:
    with pytest.raises(ValidationError):
        LatencyBudgetConfig(tau_ms=tau)


def test_extreme_decay_is_finite_and_monotonic() -> None:
    tau = Milliseconds(1_000)
    values = [exponential_decay(Milliseconds(latency), tau) for latency in (0, 1, 10, 100, 1_000, 10_000, 1_000_000)]

    assert values[0] == 1.0
    assert values[-1] == 0.0
    assert all(left >= right for left, right in zip(values, values[1:]))
    assert all(0 <= value <= 1 for value in values)


def test_timestamp_equality_and_freshness_boundaries(observation) -> None:
    equal = validate_pre_decision_timestamps(observation, observation.source_capture_at)
    assert equal.valid is True
    assert "source_capture_at_after_decision_at" not in equal.reasons

    assert is_fresh(Milliseconds(5_000), 5_000) is True
    assert is_fresh(Milliseconds("5000.001"), 5_000) is False
