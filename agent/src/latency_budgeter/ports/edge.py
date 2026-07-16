"""Strategy gross-edge estimator port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from src.latency_budgeter.domain.models import MarketObservation, RawStrategySignal
from src.latency_budgeter.domain.timestamps import normalize_timestamp
from src.latency_budgeter.domain.values import BasisPoints


@dataclass(frozen=True, slots=True)
class GrossEdgeEstimate:
    """Point-in-time gross expected edge from the strategy layer."""

    gross_edge_bps: BasisPoints
    estimated_at: datetime
    estimator_version: str
    reference_price: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.gross_edge_bps, BasisPoints):
            object.__setattr__(self, "gross_edge_bps", BasisPoints(self.gross_edge_bps))
        object.__setattr__(self, "estimated_at", normalize_timestamp(self.estimated_at))
        if not str(self.estimator_version).strip():
            raise ValueError("estimator_version is required")
        if self.reference_price is not None:
            price = (
                self.reference_price
                if isinstance(self.reference_price, Decimal)
                else Decimal(str(self.reference_price))
            )
            if not price.is_finite() or price <= 0:
                raise ValueError("reference_price must be positive and finite")
            object.__setattr__(self, "reference_price", price)


@runtime_checkable
class GrossEdgeEstimator(Protocol):
    """Existing strategy adapters implement this narrow decision-time boundary."""

    def estimate(
        self,
        *,
        observation: MarketObservation,
        signal: RawStrategySignal,
        decision_at: datetime,
    ) -> GrossEdgeEstimate:
        """Return an estimate whose ``estimated_at`` is no later than the decision."""
        ...
