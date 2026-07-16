"""Reference-price port for deterministic Step 4 scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.latency_budgeter.domain.outcomes import ReferencePriceEvidence


@dataclass(frozen=True, slots=True)
class ReferencePriceRequest:
    """Frozen query identity supplied to a point-in-time price adapter."""

    decision_id: str
    symbol: str
    decision_at: datetime
    horizon_value: int
    horizon_unit: str
    reference_convention: str
    dataset_version: str


@runtime_checkable
class OutcomeReferencePriceProvider(Protocol):
    """Resolve one reproducible horizon price without looking ahead."""

    def resolve(self, request: ReferencePriceRequest) -> ReferencePriceEvidence | None:
        """Return exact evidence or ``None`` while the horizon is unavailable."""
        ...
