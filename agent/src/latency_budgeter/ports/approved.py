"""ALLOW-only output boundary for the future Step 3 order service."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.latency_budgeter.domain.decisions import ApprovedOpportunity


@runtime_checkable
class ApprovedOpportunityPort(Protocol):
    """Publish immutable approved opportunities without submitting an order."""

    def publish(self, opportunity: ApprovedOpportunity) -> bool:
        """Return True on first publication and False on an identical retry."""
        ...
