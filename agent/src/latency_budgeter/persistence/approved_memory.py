"""In-memory ALLOW-only handoff adapter for Step 2 and tests."""

from __future__ import annotations

import threading

from src.latency_budgeter.domain.decisions import ApprovedOpportunity
from src.latency_budgeter.domain.errors import IdempotencyConflictError


class InMemoryApprovedOpportunityPort:
    """Idempotent output adapter; it cannot place or cancel orders."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._opportunities: dict[str, ApprovedOpportunity] = {}

    def publish(self, opportunity: ApprovedOpportunity) -> bool:
        with self._lock:
            prior = self._opportunities.get(opportunity.decision_id)
            if prior is not None:
                if prior != opportunity:
                    raise IdempotencyConflictError("approved opportunity changed after publication")
                return False
            self._opportunities[opportunity.decision_id] = opportunity
            return True

    def get(self, decision_id: str) -> ApprovedOpportunity | None:
        with self._lock:
            return self._opportunities.get(decision_id)

    def all(self) -> tuple[ApprovedOpportunity, ...]:
        with self._lock:
            return tuple(self._opportunities[key] for key in sorted(self._opportunities))
