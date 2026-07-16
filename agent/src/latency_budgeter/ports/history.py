"""Latency-history repository port."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from src.latency_budgeter.domain.history import HistoryQuery, HistoryWindow, LatencySample


@runtime_checkable
class LatencyHistoryStore(Protocol):
    """Append-only point-in-time component history."""

    def add(self, sample: LatencySample) -> bool:
        """Append a sample; return False for an identical retry."""
        ...

    def invalidate(
        self,
        sample_id: str,
        *,
        invalidated_at: datetime,
        reason: str,
        integrity_event_id: str,
    ) -> bool:
        """Append a point-in-time integrity invalidation."""
        ...

    def prior_window(self, query: HistoryQuery) -> HistoryWindow:
        """Return the newest legal samples under strict ``available_at < decision_at``."""
        ...

    def close(self) -> None:
        """Release resources."""
        ...
