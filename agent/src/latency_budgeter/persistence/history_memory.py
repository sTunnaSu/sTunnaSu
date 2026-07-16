"""Concurrency-safe in-memory strict-prior latency history."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from src.latency_budgeter.domain.errors import HistoryConflictError
from src.latency_budgeter.domain.history import HistoryQuery, HistoryWindow, LatencySample
from src.latency_budgeter.domain.timestamps import normalize_timestamp


@dataclass(frozen=True, slots=True)
class _Invalidation:
    invalidated_at: datetime
    reason: str
    integrity_event_id: str


class InMemoryLatencyHistoryStore:
    """Atomic snapshot queries and append-only sample/invalidation records."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._samples: dict[str, LatencySample] = {}
        self._invalidations: dict[tuple[str, str], _Invalidation] = {}

    def add(self, sample: LatencySample) -> bool:
        """Append idempotently or reject identity reuse with different content."""
        with self._lock:
            prior = self._samples.get(sample.sample_id)
            if prior is not None:
                if prior.semantic_fingerprint != sample.semantic_fingerprint:
                    raise HistoryConflictError(f"sample_id {sample.sample_id!r} has different content")
                return False
            self._samples[sample.sample_id] = sample
            return True

    def invalidate(
        self,
        sample_id: str,
        *,
        invalidated_at: datetime,
        reason: str,
        integrity_event_id: str,
    ) -> bool:
        """Append an idempotent point-in-time invalidation."""
        when = normalize_timestamp(invalidated_at)
        if sample_id not in self._samples:
            raise KeyError(f"unknown latency sample: {sample_id}")
        if not reason or not integrity_event_id:
            raise ValueError("reason and integrity_event_id are required")
        key = (sample_id, integrity_event_id)
        candidate = _Invalidation(when, reason, integrity_event_id)
        with self._lock:
            prior = self._invalidations.get(key)
            if prior is not None:
                if prior != candidate:
                    raise HistoryConflictError("integrity invalidation identity has different content")
                return False
            self._invalidations[key] = candidate
            return True

    def prior_window(self, query: HistoryQuery) -> HistoryWindow:
        """Read one lock-consistent strict-prior snapshot.

        Ordering is ``component_available_at DESC, sample_id DESC``; ties are
        therefore deterministic.  The rolling limit is applied after every
        point-in-time, validity, unit, and version predicate.
        """
        with self._lock:
            invalid_at_decision = {
                sample_id
                for (sample_id, _), invalidation in self._invalidations.items()
                if invalidation.invalidated_at <= query.decision_at
            }
            eligible = [
                sample
                for sample in self._samples.values()
                if sample.component is query.component
                and sample.component_available_at < query.decision_at
                and sample.recorded_at <= query.decision_at
                and sample.decision_id != query.current_decision_id
                and sample.valid
                and sample.sample_id not in invalid_at_decision
                and sample.unit == query.unit
                and sample.component_definition_version == query.component_definition_version
                and sample.estimator_schema_version == query.estimator_schema_version
            ]
            eligible.sort(
                key=lambda sample: (sample.component_available_at, sample.sample_id),
                reverse=True,
            )
            return HistoryWindow(tuple(eligible[: query.rolling_window]))

    def close(self) -> None:
        """No resources are held."""
