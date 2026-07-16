"""Frozen Phase 6/7-aligned cost adapter for the Step 2 gate."""

from __future__ import annotations

from src.latency_budgeter.configuration.models import CostAssumptions
from src.latency_budgeter.domain.decisions import ExecutionCostEstimate, LiquidityRole
from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.values import BasisPoints


class FrozenExecutionCostEstimator:
    """Select one fee and sum each declared adverse component exactly once."""

    def estimate(
        self,
        *,
        assumptions: CostAssumptions,
        side: SignalSide,
        liquidity_role: LiquidityRole,
    ) -> ExecutionCostEstimate:
        side = SignalSide(side)
        role = LiquidityRole(liquidity_role)
        fee = BasisPoints(assumptions.maker_fee_bps if role is LiquidityRole.MAKER else assumptions.taker_fee_bps)
        spread = BasisPoints(assumptions.spread_bps)
        slippage = BasisPoints(assumptions.slippage_bps)
        impact = BasisPoints(assumptions.impact_bps)
        total = fee + spread + slippage + impact
        if side is SignalSide.BUY:
            adverse_adjustment = total
        elif side is SignalSide.SELL:
            adverse_adjustment = BasisPoints(-total.value)
        else:
            adverse_adjustment = BasisPoints(0)
        return ExecutionCostEstimate(
            fee_bps=fee,
            spread_bps=spread,
            slippage_bps=slippage,
            impact_bps=impact,
            total_bps=total,
            liquidity_role=role,
            side=side,
            adverse_price_adjustment_bps=adverse_adjustment,
            convention=assumptions.convention,
            reference_price_convention=assumptions.reference_price_convention,
        )
