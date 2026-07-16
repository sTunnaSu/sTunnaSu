"""Decision-time execution-cost estimator port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.latency_budgeter.configuration.models import CostAssumptions
from src.latency_budgeter.domain.decisions import ExecutionCostEstimate, LiquidityRole
from src.latency_budgeter.domain.models import SignalSide


@runtime_checkable
class ExecutionCostEstimator(Protocol):
    """Estimate each frozen Phase 6/7-aligned cost component exactly once."""

    def estimate(
        self,
        *,
        assumptions: CostAssumptions,
        side: SignalSide,
        liquidity_role: LiquidityRole,
    ) -> ExecutionCostEstimate: ...
