"""Thread-safe in-memory implementation of the append-only ledger port."""

from __future__ import annotations

import threading
from dataclasses import replace

from src.latency_budgeter.domain.errors import (
    AggregateIntegrityError,
    ConcurrentAppendError,
    IdempotencyConflictError,
    IdentifierCollisionError,
)
from src.latency_budgeter.domain.events import LedgerEvent
from src.latency_budgeter.ports.ledger import AppendResult


class InMemoryEventLedger:
    """Fully functional, concurrency-safe ledger for tests and ephemeral runs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events_by_decision: dict[str, list[LedgerEvent]] = {}
        self._events_by_id: dict[str, LedgerEvent] = {}
        self._events_by_idempotency: dict[str, LedgerEvent] = {}

    def append(
        self,
        event: LedgerEvent,
        *,
        expected_version: int | None = None,
    ) -> AppendResult:
        """Append under optimistic concurrency and immutable retry semantics."""
        with self._lock:
            prior = self._events_by_idempotency.get(event.idempotency_key)
            if prior is not None:
                if prior.semantic_fingerprint != event.semantic_fingerprint:
                    raise IdempotencyConflictError(f"idempotency key {event.idempotency_key!r} has different content")
                return AppendResult(prior, appended=False)

            prior_id = self._events_by_id.get(event.event_id)
            if prior_id is not None:
                if (
                    prior_id.idempotency_key != event.idempotency_key
                    or prior_id.semantic_fingerprint != event.semantic_fingerprint
                ):
                    raise IdentifierCollisionError(f"event_id {event.event_id!r} already identifies different content")
                return AppendResult(prior_id, appended=False)

            stream = self._events_by_decision.get(event.decision_id, [])
            current_version = len(stream)
            if expected_version is not None and expected_version != current_version:
                raise ConcurrentAppendError(f"expected aggregate version {expected_version}, found {current_version}")
            if stream:
                root = stream[0]
                if root.run_id != event.run_id or root.signal_id != event.signal_id:
                    raise AggregateIntegrityError("run_id and signal_id cannot change within a decision aggregate")
            stored = replace(event, aggregate_version=current_version + 1)
            if not stream:
                self._events_by_decision[event.decision_id] = stream
            stream.append(stored)
            self._events_by_id[stored.event_id] = stored
            self._events_by_idempotency[stored.idempotency_key] = stored
            return AppendResult(stored, appended=True)

    def read(self, decision_id: str) -> tuple[LedgerEvent, ...]:
        """Return an immutable aggregate snapshot."""
        with self._lock:
            return tuple(self._events_by_decision.get(decision_id, ()))

    def read_run(self, run_id: str) -> tuple[LedgerEvent, ...]:
        """Return run events in deterministic record/event order."""
        with self._lock:
            events = [
                event for stream in self._events_by_decision.values() for event in stream if event.run_id == run_id
            ]
        return tuple(sorted(events, key=lambda event: (event.recorded_at, event.decision_id, event.aggregate_version)))

    def close(self) -> None:
        """Satisfy the ledger port; no external resources are held."""
