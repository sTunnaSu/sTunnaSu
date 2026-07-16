"""Deterministic event-time projection for Phase 8 Step 3."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Sequence

from src.latency_budgeter.domain.errors import ProjectionError
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.lifecycle import TerminalState, exact_decimal
from src.latency_budgeter.domain.timestamps import normalize_timestamp


def _timestamp(payload: Mapping[str, object], key: str) -> datetime | None:
    value = payload.get(key)
    return normalize_timestamp(str(value)) if value else None


def _quantity(payload: Mapping[str, object], key: str) -> Decimal:
    return exact_decimal(payload.get(key, "0"), label=key)


@dataclass(frozen=True, slots=True)
class ProjectedFill:
    event: LedgerEvent
    fill_id: str
    order_id: str
    fill_at: datetime
    side: str
    quantity: Decimal
    price: Decimal
    cumulative_filled_quantity: Decimal
    unfilled_quantity: Decimal


@dataclass(frozen=True, slots=True)
class OrderLifecycleProjection:
    """Complete immutable reconstruction of one decision/order lifecycle."""

    root: LedgerEvent
    submission: LedgerEvent | None
    acknowledgements: tuple[LedgerEvent, ...]
    fills: tuple[ProjectedFill, ...]
    terminal: LedgerEvent | None
    integrity_failures: tuple[LedgerEvent, ...]
    execution_evaluations: tuple[LedgerEvent, ...]
    issues: tuple[str, ...]
    aggregate_version: int

    @property
    def submitted_quantity(self) -> Decimal | None:
        if self.submission is None:
            return None
        return _quantity(self.submission.payload, "submitted_quantity")

    @property
    def order_id(self) -> str | None:
        if self.submission is not None:
            return str(self.submission.payload.get("order_id", "")) or None
        if self.fills:
            return self.fills[0].order_id
        if self.terminal is not None:
            return str(self.terminal.payload.get("order_id", "")) or None
        return None

    @property
    def submitted_at(self) -> datetime | None:
        return _timestamp(self.submission.payload, "submitted_at") if self.submission else None

    @property
    def acknowledged_at(self) -> datetime | None:
        timestamps = [
            timestamp
            for event in self.acknowledgements
            if (timestamp := _timestamp(event.payload, "acknowledged_at")) is not None
        ]
        return min(timestamps, default=None)

    @property
    def first_fill(self) -> ProjectedFill | None:
        return self.fills[0] if self.fills else None

    @property
    def final_fill(self) -> ProjectedFill | None:
        return self.fills[-1] if self.fills else None

    @property
    def first_fill_at(self) -> datetime | None:
        return self.first_fill.fill_at if self.first_fill else None

    @property
    def final_fill_at(self) -> datetime | None:
        return self.final_fill.fill_at if self.final_fill else None

    @property
    def executed_quantity(self) -> Decimal:
        return sum((fill.quantity for fill in self.fills), Decimal("0"))

    @property
    def unfilled_quantity(self) -> Decimal | None:
        submitted = self.submitted_quantity
        return max(submitted - self.executed_quantity, Decimal("0")) if submitted is not None else None

    @property
    def terminal_at(self) -> datetime | None:
        return _timestamp(self.terminal.payload, "terminal_at") if self.terminal else None

    @property
    def terminal_state(self) -> TerminalState | None:
        if self.terminal is None:
            return None
        return TerminalState(str(self.terminal.payload["terminal_state"]))

    @property
    def latest_execution_evaluation(self) -> LedgerEvent | None:
        return self.execution_evaluations[-1] if self.execution_evaluations else None


class OrderLifecycleProjector:
    """Replay ingestion order while deriving fill state in event-time order."""

    def replay(self, events: Sequence[LedgerEvent]) -> OrderLifecycleProjection:
        if not events:
            raise ProjectionError("cannot project an empty lifecycle")
        ordered = tuple(events)
        root = ordered[0]
        if root.event_type is not EventType.DECISION_CREATED:
            raise ProjectionError("the lifecycle root must be decision_created")
        for expected, event in enumerate(ordered, start=1):
            if event.aggregate_version != expected:
                raise ProjectionError(
                    f"non-contiguous aggregate version: expected {expected}, found {event.aggregate_version}"
                )
            if (event.decision_id, event.run_id, event.signal_id) != (
                root.decision_id,
                root.run_id,
                root.signal_id,
            ):
                raise ProjectionError("lifecycle stream contains mixed aggregate identities")

        submissions = [event for event in ordered if event.event_type is EventType.ORDER_SUBMITTED]
        acknowledgements = tuple(
            event for event in ordered if event.event_type is EventType.BROKER_ACKNOWLEDGED
        )
        terminal_events = [event for event in ordered if event.event_type is EventType.ORDER_TERMINAL]
        failures = tuple(
            event for event in ordered if event.event_type is EventType.LIFECYCLE_INTEGRITY_FAILURE
        )
        evaluations = tuple(
            event for event in ordered if event.event_type is EventType.EXECUTION_EVALUATED
        )
        issues: list[str] = []
        if len(submissions) > 1:
            issues.append("multiple_order_submitted_events")
        if len(terminal_events) > 1:
            issues.append("multiple_order_terminal_events")
        submission = submissions[0] if submissions else None
        terminal = terminal_events[0] if terminal_events else None

        unique_fills: dict[str, ProjectedFill] = {}
        for event in ordered:
            if event.event_type is not EventType.FILL_RECEIVED:
                continue
            payload = event.payload
            fill_id = str(payload.get("fill_id", ""))
            if not fill_id:
                issues.append(f"missing_fill_identifier:{event.event_id}")
                continue
            projected = ProjectedFill(
                event=event,
                fill_id=fill_id,
                order_id=str(payload.get("order_id", "")),
                fill_at=normalize_timestamp(str(payload["fill_at"])),
                side=str(payload.get("side", "")),
                quantity=_quantity(payload, "fill_quantity"),
                price=exact_decimal(payload.get("fill_price", "0"), label="fill price", allow_zero=False),
                cumulative_filled_quantity=_quantity(payload, "cumulative_filled_quantity"),
                unfilled_quantity=_quantity(payload, "unfilled_quantity"),
            )
            prior = unique_fills.get(fill_id)
            if prior is not None:
                issues.append(f"duplicate_fill_identifier:{fill_id}")
                continue
            unique_fills[fill_id] = projected

        fills = tuple(
            sorted(
                unique_fills.values(),
                key=lambda fill: (
                    fill.fill_at,
                    fill.event.recorded_at,
                    fill.event.aggregate_version,
                    fill.fill_id,
                ),
            )
        )
        submitted_quantity = _quantity(submission.payload, "submitted_quantity") if submission else None
        submitted_order_id = str(submission.payload.get("order_id", "")) if submission else ""
        running = Decimal("0")
        for fill in fills:
            running += fill.quantity
            if submitted_order_id and fill.order_id != submitted_order_id:
                issues.append(f"fill_order_mismatch:{fill.fill_id}")
            if fill.cumulative_filled_quantity != running:
                issues.append(f"cumulative_fill_mismatch:{fill.fill_id}")
            if submitted_quantity is not None:
                if running > submitted_quantity:
                    issues.append(f"overfill:{fill.fill_id}")
                expected_unfilled = max(submitted_quantity - running, Decimal("0"))
                if fill.unfilled_quantity != expected_unfilled:
                    issues.append(f"unfilled_quantity_mismatch:{fill.fill_id}")

        if terminal is not None and submitted_quantity is not None:
            terminal_executed = _quantity(terminal.payload, "executed_quantity")
            terminal_unfilled = _quantity(terminal.payload, "unfilled_quantity")
            if terminal_executed + terminal_unfilled != submitted_quantity:
                issues.append("terminal_quantity_conservation_failure")
            if terminal_executed != running:
                issues.append("terminal_executed_quantity_mismatch")
            terminal_order_id = str(terminal.payload.get("order_id", ""))
            if submitted_order_id and terminal_order_id != submitted_order_id:
                issues.append("terminal_order_mismatch")

        return OrderLifecycleProjection(
            root=root,
            submission=submission,
            acknowledgements=acknowledgements,
            fills=fills,
            terminal=terminal,
            integrity_failures=failures,
            execution_evaluations=evaluations,
            issues=tuple(dict.fromkeys(issues)),
            aggregate_version=ordered[-1].aggregate_version,
        )

