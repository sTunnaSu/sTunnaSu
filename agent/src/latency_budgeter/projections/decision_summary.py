"""Deterministic per-decision reconstruction from append-only events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.latency_budgeter.domain.errors import ProjectionError
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.json_values import freeze_json


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """A read-only projection that keeps forecasts and outcomes separate."""

    decision_id: str
    run_id: str
    signal_id: str
    original_decision: Mapping[str, Any]
    raw_signal_count: int
    common_phase8_eligible: bool
    event_ids: tuple[str, ...]
    order_submissions: tuple[Mapping[str, Any], ...]
    broker_acknowledgements: tuple[Mapping[str, Any], ...]
    fills: tuple[Mapping[str, Any], ...]
    terminal_events: tuple[Mapping[str, Any], ...]
    lifecycle_integrity_failures: tuple[Mapping[str, Any], ...]
    execution_evaluations: tuple[Mapping[str, Any], ...]
    outcome_evaluations: tuple[Mapping[str, Any], ...]
    aggregate_version: int


class DecisionSummaryProjector:
    """Pure projector; it never mutates events or original decision evidence."""

    def replay(self, events: Sequence[LedgerEvent]) -> DecisionSummary:
        """Reconstruct one decision after validating identity and ordering."""
        if not events:
            raise ProjectionError("cannot project an empty decision stream")
        ordered = tuple(events)
        root = ordered[0]
        for expected_version, event in enumerate(ordered, start=1):
            if event.aggregate_version != expected_version:
                raise ProjectionError(
                    f"non-contiguous aggregate version: expected {expected_version}, found {event.aggregate_version}"
                )
            if (
                event.decision_id != root.decision_id
                or event.run_id != root.run_id
                or event.signal_id != root.signal_id
            ):
                raise ProjectionError("event stream contains mixed aggregate identities")
        if root.event_type is not EventType.DECISION_CREATED:
            raise ProjectionError("the first aggregate event must be decision_created")
        if sum(event.event_type is EventType.DECISION_CREATED for event in ordered) != 1:
            raise ProjectionError("a decision aggregate must contain exactly one decision_created")

        buckets: dict[EventType, list[Mapping[str, Any]]] = {event_type: [] for event_type in EventType}
        for event in ordered[1:]:
            buckets[event.event_type].append(freeze_json(event.payload))
        original = freeze_json(root.payload)
        classification = original.get("shared_cohort_classification", {})
        common_eligible = bool(classification.get("common_phase8_eligible", False))
        return DecisionSummary(
            decision_id=root.decision_id,
            run_id=root.run_id,
            signal_id=root.signal_id,
            original_decision=original,
            raw_signal_count=1,
            common_phase8_eligible=common_eligible,
            event_ids=tuple(event.event_id for event in ordered),
            order_submissions=tuple(buckets[EventType.ORDER_SUBMITTED]),
            broker_acknowledgements=tuple(buckets[EventType.BROKER_ACKNOWLEDGED]),
            fills=tuple(buckets[EventType.FILL_RECEIVED]),
            terminal_events=tuple(buckets[EventType.ORDER_TERMINAL]),
            lifecycle_integrity_failures=tuple(buckets[EventType.LIFECYCLE_INTEGRITY_FAILURE]),
            execution_evaluations=tuple(buckets[EventType.EXECUTION_EVALUATED]),
            outcome_evaluations=tuple(buckets[EventType.OUTCOME_EVALUATED]),
            aggregate_version=ordered[-1].aggregate_version,
        )
