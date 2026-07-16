"""Append-only event-ledger port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from src.latency_budgeter.domain.events import LedgerEvent


@dataclass(frozen=True, slots=True)
class AppendResult:
    """Result of an append or an idempotent retry."""

    event: LedgerEvent
    appended: bool


@runtime_checkable
class EventLedger(Protocol):
    """Persistence contract for ordered immutable decision aggregates."""

    def append(
        self,
        event: LedgerEvent,
        *,
        expected_version: int | None = None,
    ) -> AppendResult:
        """Append one event or return its idempotent prior write."""
        ...

    def read(self, decision_id: str) -> tuple[LedgerEvent, ...]:
        """Read one aggregate in deterministic version order."""
        ...

    def read_run(self, run_id: str) -> tuple[LedgerEvent, ...]:
        """Read all run events in deterministic record order."""
        ...

    def close(self) -> None:
        """Release persistence resources."""
        ...
