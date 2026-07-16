"""Event-sourced Phase 8 Step 3 execution-lifecycle application service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Mapping, Sequence

from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.errors import (
    ConcurrentAppendError,
    ExecutionBlockedError,
    IdempotencyConflictError,
    LifecycleConflictError,
    LifecycleIntegrityError,
)
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.history import LatencyComponent, LatencySample
from src.latency_budgeter.domain.lifecycle import (
    STEP3_AUDIT_VERSION,
    STEP3_EXECUTION_EVALUATION_VERSION,
    STEP3_LIFECYCLE_VERSION,
    AcknowledgementObservation,
    CallbackResult,
    ComponentValidationStatus,
    ExecutionAuthorization,
    ExecutionOutcome,
    FillObservation,
    HandoffState,
    SubmissionObservation,
    TerminalObservation,
    TerminalState,
    decimal_text,
    exact_decimal,
)
from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.timestamps import elapsed_ms_exact, normalize_timestamp, utc_iso
from src.latency_budgeter.domain.values import Milliseconds
from src.latency_budgeter.ports.history import LatencyHistoryStore
from src.latency_budgeter.ports.ledger import AppendResult, EventLedger
from src.latency_budgeter.projections.order_lifecycle import (
    OrderLifecycleProjection,
    OrderLifecycleProjector,
)

LifecycleClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class _ComponentMeasurement:
    component: LatencyComponent
    start_at: datetime
    available_at: datetime
    source_event: LedgerEvent
    order_id: str


class ExecutionLifecycleService:
    """Validate ALLOW handoff and record actual engine callbacks immutably."""

    def __init__(
        self,
        *,
        config: LatencyBudgetConfig,
        ledger: EventLedger,
        history: LatencyHistoryStore,
        clock: LifecycleClock | None = None,
        clock_source: str = "system_utc_clock",
        max_append_retries: int = 20,
    ) -> None:
        if not config.enabled:
            raise ValueError("Step 3 requires the enabled Step 2 gate configuration")
        if not clock_source:
            raise ValueError("clock_source is required")
        self.config = config
        self.ledger = ledger
        self.history = history
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.clock_source = str(clock_source)
        self.max_append_retries = max(int(max_append_retries), 1)
        self.projector = OrderLifecycleProjector()

    # ------------------------------------------------------------------
    # ALLOW-only handoff

    def authorize(self, decision_id: str) -> ExecutionAuthorization:
        """Return immutable proof for one valid ALLOW; never submit an order."""
        projection = self._projection(decision_id)
        root = projection.root
        payload = root.payload
        if str(payload.get("decision", "")) != "ALLOW":
            raise ExecutionBlockedError("only immutable ALLOW decisions may enter execution")
        approved = payload.get("approved_opportunity")
        economics = payload.get("decision_economics")
        observation = payload.get("observation")
        if not isinstance(approved, Mapping) or not isinstance(economics, Mapping):
            raise ExecutionBlockedError("ALLOW decision is missing frozen approved economics")
        if not isinstance(observation, Mapping):
            raise ExecutionBlockedError("ALLOW decision is missing its source observation")
        if str(approved.get("decision_id", "")) != root.decision_id:
            raise ExecutionBlockedError("approved opportunity decision_id does not match the aggregate")
        if str(approved.get("run_id", "")) != root.run_id or str(approved.get("signal_id", "")) != root.signal_id:
            raise ExecutionBlockedError("approved opportunity correlation identity is inconsistent")
        approved_economics = approved.get("economics")
        if approved_economics != economics:
            raise ExecutionBlockedError("approved opportunity economics differ from frozen decision economics")
        side = SignalSide(str(approved.get("side", "")))
        if side is SignalSide.FLAT:
            raise ExecutionBlockedError("flat opportunities cannot submit")
        existing_order_id = projection.order_id
        state = HandoffState.ALREADY_SUBMITTED if projection.submission else HandoffState.READY
        return ExecutionAuthorization(
            decision_id=root.decision_id,
            run_id=root.run_id,
            signal_id=root.signal_id,
            decision_event_id=root.event_id,
            decision_at=normalize_timestamp(str(payload["decision_at"])),
            observed_at=normalize_timestamp(str(observation["observed_at"])),
            symbol=str(approved.get("symbol", "")),
            side=side,
            frozen_decision_fingerprint=root.semantic_fingerprint,
            # Adapters must pass this deterministic key to brokers/engines
            # that expose client-order idempotency.
            client_order_id=root.decision_id,
            state=state,
            submitted_order_id=existing_order_id,
        )

    def record_submission(
        self,
        authorization: ExecutionAuthorization,
        observation: SubmissionObservation,
    ) -> CallbackResult:
        """Append ``order_submitted`` only after the adapter reports success."""
        projection = self._validated_authorization(authorization)
        if observation.client_order_id != authorization.client_order_id:
            raise ExecutionBlockedError("submission did not use the deterministic authorised client_order_id")
        if projection.order_id is not None and projection.order_id != observation.order_id:
            raise LifecycleConflictError("submission order_id conflicts with earlier out-of-order callbacks")
        if projection.submission is not None:
            self._assert_same_submission(projection.submission, observation)
            released = self._reconcile_component_history(projection)
            self._reconcile_terminal(authorization.decision_id)
            return self._result(projection.submission, False, released)

        status, value, reason = self._component_status(
            authorization.decision_at,
            observation.submitted_at,
            "decision_at_after_submitted_at",
        )
        recorded_at = self._recorded_at(observation.submitted_at)
        event = LedgerEvent.create(
            event_type=EventType.ORDER_SUBMITTED,
            occurred_at=observation.submitted_at,
            recorded_at=recorded_at,
            decision_id=authorization.decision_id,
            run_id=authorization.run_id,
            signal_id=authorization.signal_id,
            idempotency_key=f"order_submitted:{authorization.decision_id}",
            causation_id=authorization.decision_event_id,
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "submitted_at": utc_iso(observation.submitted_at),
                "submitted_quantity": decimal_text(observation.quantity),
                "order_type": observation.order_type,
                "limit_price": decimal_text(observation.limit_price),
                "participation_limit": decimal_text(observation.participation_limit),
                "expiry_at": utc_iso(observation.expiry_at) if observation.expiry_at else None,
                "decision_id": authorization.decision_id,
                "order_id": observation.order_id,
                "client_order_id": observation.client_order_id,
                "venue_reference": observation.venue_reference,
                "side": authorization.side.value,
                "symbol": authorization.symbol,
                "component_validation": self._validation_payload(
                    LatencyComponent.SUBMISSION, status, value, reason
                ),
            },
            source_metadata={
                "timestamp_source": observation.timestamp_source,
                "ingestion_clock_source": self.clock_source,
            },
            integrity_metadata={"step": "phase8-step3", "actual_callback": True},
        )
        result = self._append(event)
        projection = self._projection(authorization.decision_id)
        released = self._reconcile_component_history(projection)
        self._reconcile_terminal(authorization.decision_id)
        return self._result(result.event, result.appended, released)

    # ------------------------------------------------------------------
    # Broker and fill callbacks

    def record_acknowledgement(
        self,
        authorization: ExecutionAuthorization,
        observation: AcknowledgementObservation,
    ) -> CallbackResult:
        """Append one real acknowledgement; absence must not call this method."""
        projection = self._validated_authorization(authorization)
        self._assert_order_correlation(projection, observation.order_id)
        key = f"broker_acknowledged:{authorization.decision_id}:{observation.acknowledgement_id}"
        if existing := self._find_idempotency(projection, key):
            self._assert_same_acknowledgement(existing, observation)
            released = self._reconcile_component_history(projection)
            self._reconcile_terminal(authorization.decision_id)
            return self._result(existing, False, released)

        if projection.submitted_at is None:
            status, value, reason = ComponentValidationStatus.PENDING_CONTEXT, None, "submission_not_ingested"
        else:
            status, value, reason = self._component_status(
                projection.submitted_at,
                observation.acknowledged_at,
                "acknowledged_at_before_submitted_at",
            )
        event = LedgerEvent.create(
            event_type=EventType.BROKER_ACKNOWLEDGED,
            occurred_at=observation.acknowledged_at,
            recorded_at=self._recorded_at(observation.acknowledged_at),
            decision_id=authorization.decision_id,
            run_id=authorization.run_id,
            signal_id=authorization.signal_id,
            idempotency_key=key,
            causation_id=projection.submission.event_id if projection.submission else authorization.decision_event_id,
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "order_id": observation.order_id,
                "acknowledgement_id": observation.acknowledgement_id,
                "acknowledged_at": utc_iso(observation.acknowledged_at),
                "venue_reference": observation.venue_reference,
                "component_validation": self._validation_payload(
                    LatencyComponent.ACKNOWLEDGEMENT, status, value, reason
                ),
            },
            source_metadata={
                "timestamp_source": observation.timestamp_source,
                "ingestion_clock_source": self.clock_source,
            },
            integrity_metadata={"step": "phase8-step3", "actual_callback": True},
        )
        result = self._append(event)
        projection = self._projection(authorization.decision_id)
        released = self._reconcile_component_history(projection)
        self._reconcile_terminal(authorization.decision_id)
        return self._result(result.event, result.appended, released)

    def record_fill(
        self,
        authorization: ExecutionAuthorization,
        observation: FillObservation,
    ) -> CallbackResult:
        """Append one actual fill, deduplicated by venue fill identifier."""
        before = self._validated_authorization(authorization)
        self._assert_order_correlation(before, observation.order_id)
        if observation.side is not authorization.side:
            raise LifecycleConflictError("fill side differs from the authorised signal side")
        key = f"fill_received:{authorization.decision_id}:{observation.order_id}:{observation.fill_id}"
        if existing := self._find_idempotency(before, key):
            self._assert_same_fill(existing, observation)
            released = self._reconcile_component_history(before)
            self._reconcile_terminal(authorization.decision_id)
            return self._result(existing, False, released)

        status, value, reason = self._fill_component_status(before, observation.fill_at)
        implementation_shortfall, shortfall_source = self._implementation_shortfall(observation)
        event = LedgerEvent.create(
            event_type=EventType.FILL_RECEIVED,
            occurred_at=observation.fill_at,
            recorded_at=self._recorded_at(observation.fill_at),
            decision_id=authorization.decision_id,
            run_id=authorization.run_id,
            signal_id=authorization.signal_id,
            idempotency_key=key,
            causation_id=before.submission.event_id if before.submission else authorization.decision_event_id,
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "order_id": observation.order_id,
                "fill_id": observation.fill_id,
                "fill_at": utc_iso(observation.fill_at),
                "side": observation.side.value,
                "signed_fill_quantity": decimal_text(observation.signed_quantity),
                "fill_quantity": decimal_text(observation.quantity),
                "fill_price": decimal_text(observation.price),
                "cumulative_filled_quantity": decimal_text(observation.cumulative_filled_quantity),
                "unfilled_quantity": decimal_text(observation.unfilled_quantity),
                "venue_reference": observation.venue_reference,
                "decision_price": decimal_text(observation.decision_price),
                "actual_fee": decimal_text(observation.fee),
                "actual_spread_result": decimal_text(observation.spread_cost),
                "actual_slippage": decimal_text(observation.slippage_cost),
                "actual_impact_proxy": decimal_text(observation.impact_cost),
                "implementation_shortfall": decimal_text(implementation_shortfall),
                "implementation_shortfall_source": shortfall_source,
                "component_validation": self._validation_payload(
                    LatencyComponent.FILL, status, value, reason
                ),
            },
            source_metadata={
                "timestamp_source": observation.timestamp_source,
                "ingestion_clock_source": self.clock_source,
            },
            integrity_metadata={"step": "phase8-step3", "actual_callback": True},
        )
        result = self._append(event)
        after = self._projection(authorization.decision_id)
        # Cumulative fields may look incomplete while earlier event-time fills
        # are still in flight.  Defer those checks to the terminal audit; only
        # an overfill is irrecoverable by receiving more messages.
        fill_issues = tuple(issue for issue in after.issues if issue.startswith("overfill"))
        if fill_issues:
            self._append_integrity_failure(
                after,
                invalid_sequences=fill_issues,
                affected_components=(LatencyComponent.FILL, LatencyComponent.FINAL_FILL),
                related_events=(result.event,),
                stage="fill_ingestion",
            )
            after = self._projection(authorization.decision_id)
        released = self._reconcile_component_history(after)
        self._reconcile_terminal(authorization.decision_id)
        return self._result(result.event, result.appended, released)

    # ------------------------------------------------------------------
    # Terminal state, audit, and actual execution evaluation

    def record_terminal(
        self,
        authorization: ExecutionAuthorization,
        observation: TerminalObservation,
    ) -> CallbackResult:
        """Append one actual terminal transition, then audit and evaluate it."""
        projection = self._validated_authorization(authorization)
        self._assert_order_correlation(projection, observation.order_id)
        existing = projection.terminal
        if existing is not None:
            if self._terminal_matches(existing, observation):
                self._reconcile_terminal(authorization.decision_id)
                return self._result(existing, False, ())
            failure = self._append_integrity_failure(
                projection,
                invalid_sequences=("conflicting_terminal_callback",),
                affected_components=(),
                related_events=(existing,),
                stage="terminal_race",
            )
            self._reconcile_terminal(authorization.decision_id)
            return self._result(failure.event, failure.appended, ())

        event = LedgerEvent.create(
            event_type=EventType.ORDER_TERMINAL,
            occurred_at=observation.terminal_at,
            recorded_at=self._recorded_at(observation.terminal_at),
            decision_id=authorization.decision_id,
            run_id=authorization.run_id,
            signal_id=authorization.signal_id,
            idempotency_key=f"order_terminal:{authorization.decision_id}:{observation.order_id}",
            causation_id=(
                projection.final_fill.event.event_id
                if projection.final_fill
                else projection.submission.event_id if projection.submission else authorization.decision_event_id
            ),
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "order_id": observation.order_id,
                "terminal_at": utc_iso(observation.terminal_at),
                "terminal_state": observation.terminal_state.value,
                "reason_code": observation.reason_code.value,
                "engine_status": observation.engine_status,
                "engine_status_reason": observation.engine_status_reason,
                "executed_quantity": decimal_text(observation.executed_quantity),
                "unfilled_quantity": decimal_text(observation.unfilled_quantity),
                "cancellation_fee": decimal_text(observation.cancellation_fee),
                "cancellation_fee_reported": observation.cancellation_fee is not None,
                "acknowledgement_availability": observation.acknowledgement_availability.value,
                "venue_reference": observation.venue_reference,
                "approved_but_unfilled": observation.executed_quantity == 0,
                "realised_fill_latency_ms": None if observation.executed_quantity == 0 else "derived_after_audit",
                "realised_trade_pnl": None,
            },
            source_metadata={
                "timestamp_source": observation.timestamp_source,
                "ingestion_clock_source": self.clock_source,
            },
            integrity_metadata={"step": "phase8-step3", "actual_callback": True},
        )
        try:
            result = self._append(event)
        except IdempotencyConflictError:
            raced = self._projection(authorization.decision_id)
            if raced.terminal is not None and self._terminal_matches(raced.terminal, observation):
                self._reconcile_terminal(authorization.decision_id)
                return self._result(raced.terminal, False, ())
            failure = self._append_integrity_failure(
                raced,
                invalid_sequences=("conflicting_terminal_callback",),
                affected_components=(),
                related_events=(raced.terminal,) if raced.terminal else (),
                stage="terminal_race",
            )
            self._reconcile_terminal(authorization.decision_id)
            return self._result(failure.event, failure.appended, ())
        projection = self._projection(authorization.decision_id)
        released = self._reconcile_component_history(projection)
        self._reconcile_terminal(authorization.decision_id)
        return self._result(result.event, result.appended, released)

    def projection(self, decision_id: str) -> OrderLifecycleProjection:
        """Public deterministic replay entry point used by restart recovery."""
        return self._projection(decision_id)

    # ------------------------------------------------------------------
    # Internal event, history, and audit mechanics

    def _validated_authorization(self, authorization: ExecutionAuthorization) -> OrderLifecycleProjection:
        current = self.authorize(authorization.decision_id)
        immutable_fields = (
            "run_id",
            "signal_id",
            "decision_event_id",
            "decision_at",
            "observed_at",
            "symbol",
            "side",
            "frozen_decision_fingerprint",
            "client_order_id",
        )
        if any(getattr(current, name) != getattr(authorization, name) for name in immutable_fields):
            raise ExecutionBlockedError("execution authorization no longer matches immutable ALLOW evidence")
        return self._projection(authorization.decision_id)

    def _projection(self, decision_id: str) -> OrderLifecycleProjection:
        events = self.ledger.read(decision_id)
        if not events:
            raise ExecutionBlockedError(f"unknown decision_id: {decision_id}")
        return self.projector.replay(events)

    def _append(self, event: LedgerEvent) -> AppendResult:
        for _ in range(self.max_append_retries):
            current = self.ledger.read(event.decision_id)
            try:
                return self.ledger.append(event, expected_version=len(current))
            except ConcurrentAppendError:
                continue
        raise ConcurrentAppendError("lifecycle append retry budget exhausted")

    def _recorded_at(self, occurred_at: datetime) -> datetime:
        now = normalize_timestamp(self.clock())
        occurred = normalize_timestamp(occurred_at)
        if now < occurred:
            raise LifecycleIntegrityError("ingestion clock precedes the adapter event timestamp")
        return now

    @staticmethod
    def _component_status(
        start: datetime,
        end: datetime,
        invalid_reason: str,
    ) -> tuple[ComponentValidationStatus, Decimal | None, str]:
        value = elapsed_ms_exact(end, start)
        if value < 0:
            return ComponentValidationStatus.INVALID, None, invalid_reason
        return ComponentValidationStatus.VALID, value, ""

    def _fill_component_status(
        self,
        projection: OrderLifecycleProjection,
        fill_at: datetime,
    ) -> tuple[ComponentValidationStatus, Decimal | None, str]:
        if projection.submitted_at is None:
            return ComponentValidationStatus.PENDING_CONTEXT, None, "submission_not_ingested"
        return self._component_status(
            projection.submitted_at,
            fill_at,
            "fill_at_before_submitted_at",
        )

    @staticmethod
    def _validation_payload(
        component: LatencyComponent,
        status: ComponentValidationStatus,
        value: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        return {
            "component_type": component.value,
            "status": status.value,
            "component_value_ms": decimal_text(value),
            "invalidation_reason": reason or None,
            "unit": "ms",
        }

    @staticmethod
    def _sample_id(
        decision_id: str,
        order_id: str,
        component: LatencyComponent,
        source_event_id: str,
    ) -> str:
        material = "\x1f".join((decision_id, order_id, component.value, source_event_id))
        return f"lat_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    def _measurements(self, projection: OrderLifecycleProjection) -> tuple[_ComponentMeasurement, ...]:
        if projection.submission is None or projection.submitted_at is None or projection.order_id is None:
            return ()
        root_payload = projection.root.payload
        decision_at = normalize_timestamp(str(root_payload["decision_at"]))
        measurements: list[_ComponentMeasurement] = [
            _ComponentMeasurement(
                component=LatencyComponent.SUBMISSION,
                start_at=decision_at,
                available_at=projection.submitted_at,
                source_event=projection.submission,
                order_id=projection.order_id,
            )
        ]
        if projection.acknowledgements:
            valid_acks = sorted(
                (
                    (normalize_timestamp(str(event.payload["acknowledged_at"])), event)
                    for event in projection.acknowledgements
                ),
                key=lambda item: (item[0], item[1].recorded_at, item[1].aggregate_version),
            )
            ack_at, ack_event = valid_acks[0]
            measurements.append(
                _ComponentMeasurement(
                    component=LatencyComponent.ACKNOWLEDGEMENT,
                    start_at=projection.submitted_at,
                    available_at=ack_at,
                    source_event=ack_event,
                    order_id=projection.order_id,
                )
            )
        if projection.first_fill is not None:
            measurements.append(
                _ComponentMeasurement(
                    component=LatencyComponent.FILL,
                    start_at=projection.submitted_at,
                    available_at=projection.first_fill.fill_at,
                    source_event=projection.first_fill.event,
                    order_id=projection.order_id,
                )
            )
        if (
            projection.final_fill is not None
            and projection.first_fill is not None
            and projection.submitted_quantity is not None
            and projection.executed_quantity == projection.submitted_quantity
        ):
            measurements.append(
                _ComponentMeasurement(
                    component=LatencyComponent.FINAL_FILL,
                    start_at=projection.first_fill.fill_at,
                    available_at=projection.final_fill.fill_at,
                    source_event=projection.final_fill.event,
                    order_id=projection.order_id,
                )
            )
        return tuple(measurements)

    def _reconcile_component_history(self, projection: OrderLifecycleProjection) -> tuple[str, ...]:
        released: list[str] = []
        desired: dict[LatencyComponent, _ComponentMeasurement] = {
            measurement.component: measurement for measurement in self._measurements(projection)
        }
        now = normalize_timestamp(self.clock())
        for measurement in desired.values():
            value = elapsed_ms_exact(measurement.available_at, measurement.start_at)
            if value < 0 or now < measurement.available_at:
                continue
            sample_id = self._sample_id(
                projection.root.decision_id,
                measurement.order_id,
                measurement.component,
                measurement.source_event.event_id,
            )
            sample = LatencySample(
                sample_id=sample_id,
                decision_id=projection.root.decision_id,
                order_id=measurement.order_id,
                component=measurement.component,
                value_ms=Milliseconds(value),
                component_available_at=measurement.available_at,
                recorded_at=now,
                component_definition_version=self.config.component_definition_version,
                estimator_schema_version=self.config.estimator_schema_version,
                source_event_id=measurement.source_event.event_id,
            )
            if self.history.add(sample):
                released.append(sample_id)

        # Event-time correction policy: a late earlier first fill (or later
        # final fill) creates a new sample and append-only invalidates any
        # formerly selected sample. Historical queries before invalidation keep
        # their original point-in-time answer; later research sees the repair.
        fill_events = [fill.event for fill in projection.fills]
        for component in (LatencyComponent.FILL, LatencyComponent.FINAL_FILL):
            selected = desired.get(component)
            for event in fill_events:
                if selected is not None and event.event_id == selected.source_event.event_id:
                    continue
                sample_id = self._sample_id(
                    projection.root.decision_id,
                    projection.order_id or "",
                    component,
                    event.event_id,
                )
                try:
                    self.history.invalidate(
                        sample_id,
                        invalidated_at=now,
                        reason="superseded_by_event_time_reconstruction",
                        integrity_event_id=selected.source_event.event_id if selected else event.event_id,
                    )
                except KeyError:
                    pass
        return tuple(released)

    def _reconcile_terminal(self, decision_id: str) -> None:
        projection = self._projection(decision_id)
        if projection.terminal is None:
            return
        audit_issues, affected = self._audit(projection)
        if audit_issues:
            failure = self._append_integrity_failure(
                projection,
                invalid_sequences=audit_issues,
                affected_components=affected,
                related_events=self._audit_related_events(projection),
                stage="terminal_cross_event_audit",
            )
            projection = self._projection(decision_id)
            self._invalidate_components(projection, affected, failure.event)
            projection = self._projection(decision_id)
        self._append_execution_evaluation(projection, audit_issues)

    def _audit(
        self,
        projection: OrderLifecycleProjection,
    ) -> tuple[tuple[str, ...], tuple[LatencyComponent, ...]]:
        issues = list(projection.issues)
        affected: set[LatencyComponent] = set()
        root_payload = projection.root.payload
        observation = root_payload.get("observation")
        if not isinstance(observation, Mapping):
            issues.append("missing_observation_timestamp")
        else:
            observed_at = normalize_timestamp(str(observation["observed_at"]))
            decision_at = normalize_timestamp(str(root_payload["decision_at"]))
            if observed_at > decision_at:
                issues.append("observed_at_after_decision_at")
        decision_at = normalize_timestamp(str(root_payload["decision_at"]))
        if projection.submitted_at is None:
            issues.append("terminal_without_submission")
            affected.add(LatencyComponent.SUBMISSION)
        else:
            if decision_at > projection.submitted_at:
                issues.append("decision_at_after_submitted_at")
                affected.add(LatencyComponent.SUBMISSION)
            if projection.acknowledged_at is not None and projection.submitted_at > projection.acknowledged_at:
                issues.append("submitted_at_after_acknowledged_at")
                affected.add(LatencyComponent.ACKNOWLEDGEMENT)
            if projection.first_fill_at is not None and projection.submitted_at > projection.first_fill_at:
                issues.append("submitted_at_after_first_fill_at")
                affected.add(LatencyComponent.FILL)
            if (
                projection.first_fill_at is not None
                and projection.final_fill_at is not None
                and projection.first_fill_at > projection.final_fill_at
            ):
                issues.append("first_fill_at_after_final_fill_at")
                affected.add(LatencyComponent.FINAL_FILL)
            if projection.terminal_at is not None and projection.submitted_at > projection.terminal_at:
                issues.append("submitted_at_after_terminal_at")
                affected.update(
                    {LatencyComponent.SUBMISSION, LatencyComponent.ACKNOWLEDGEMENT, LatencyComponent.FILL, LatencyComponent.FINAL_FILL}
                )
        if projection.terminal is not None:
            terminal_executed = exact_decimal(
                projection.terminal.payload.get("executed_quantity", "0"), label="terminal executed"
            )
            terminal_unfilled = exact_decimal(
                projection.terminal.payload.get("unfilled_quantity", "0"), label="terminal unfilled"
            )
            submitted = projection.submitted_quantity
            if submitted is not None and terminal_executed + terminal_unfilled != submitted:
                issues.append("terminal_quantity_conservation_failure")
            if terminal_executed != projection.executed_quantity:
                issues.append("terminal_executed_quantity_mismatch")
            state = projection.terminal_state
            if state is TerminalState.FULLY_FILLED and (
                terminal_unfilled != 0
                or (submitted is not None and terminal_executed != submitted)
            ):
                issues.append("fully_filled_state_quantity_mismatch")
            if state in {
                TerminalState.PARTIALLY_FILLED_EXPIRED,
                TerminalState.PARTIALLY_FILLED_CANCELLED,
            } and (terminal_executed <= 0 or terminal_unfilled <= 0):
                issues.append("partial_terminal_state_quantity_mismatch")
            if state in {
                TerminalState.EXPIRED_UNFILLED,
                TerminalState.CANCELLED_UNFILLED,
                TerminalState.REJECTED_UNFILLED,
            } and terminal_executed != 0:
                issues.append("unfilled_terminal_state_has_execution")
        if any(issue.startswith(("cumulative_fill_mismatch", "unfilled_quantity_mismatch", "overfill")) for issue in issues):
            affected.update({LatencyComponent.FILL, LatencyComponent.FINAL_FILL})
        return tuple(dict.fromkeys(issues)), tuple(sorted(affected, key=lambda component: component.value))

    @staticmethod
    def _audit_related_events(projection: OrderLifecycleProjection) -> tuple[LedgerEvent, ...]:
        events: list[LedgerEvent] = [projection.root]
        if projection.submission:
            events.append(projection.submission)
        events.extend(projection.acknowledgements)
        events.extend(fill.event for fill in projection.fills)
        if projection.terminal:
            events.append(projection.terminal)
        return tuple(events)

    def _append_integrity_failure(
        self,
        projection: OrderLifecycleProjection,
        *,
        invalid_sequences: Sequence[str],
        affected_components: Sequence[LatencyComponent],
        related_events: Sequence[LedgerEvent],
        stage: str,
    ) -> AppendResult:
        canonical_issues = tuple(sorted(set(str(issue) for issue in invalid_sequences)))
        related_ids = tuple(sorted(set(event.event_id for event in related_events)))
        signature = hashlib.sha256(
            "\x1f".join((*canonical_issues, *related_ids, stage)).encode("utf-8")
        ).hexdigest()
        key = f"lifecycle_integrity_failure:{projection.root.decision_id}:{signature}"
        if existing := self._find_idempotency(projection, key):
            return AppendResult(existing, appended=False)
        detected_at = normalize_timestamp(self.clock())
        affected = tuple(sorted(set(affected_components), key=lambda component: component.value))
        timestamps = {
            "observed_at": (
                projection.root.payload.get("observation", {}).get("observed_at")
                if isinstance(projection.root.payload.get("observation"), Mapping)
                else None
            ),
            "decision_at": projection.root.payload.get("decision_at"),
            "submitted_at": utc_iso(projection.submitted_at) if projection.submitted_at else None,
            "acknowledged_at": utc_iso(projection.acknowledged_at) if projection.acknowledged_at else None,
            "first_fill_at": utc_iso(projection.first_fill_at) if projection.first_fill_at else None,
            "final_fill_at": utc_iso(projection.final_fill_at) if projection.final_fill_at else None,
            "terminal_at": utc_iso(projection.terminal_at) if projection.terminal_at else None,
        }
        event = LedgerEvent.create(
            event_type=EventType.LIFECYCLE_INTEGRITY_FAILURE,
            occurred_at=detected_at,
            recorded_at=detected_at,
            decision_id=projection.root.decision_id,
            run_id=projection.root.run_id,
            signal_id=projection.root.signal_id,
            idempotency_key=key,
            causation_id=related_events[-1].event_id if related_events else projection.root.event_id,
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "audit_version": STEP3_AUDIT_VERSION,
                "stage": stage,
                "invalid_sequence": list(canonical_issues),
                "affected_timestamps": timestamps,
                "affected_latency_components": [component.value for component in affected],
                "related_event_ids": list(related_ids),
                "detected_at": utc_iso(detected_at),
                "original_decision_rewritten": False,
            },
            source_metadata={"ingestion_clock_source": self.clock_source},
            integrity_metadata={"step": "phase8-step3", "append_only_correction": True},
        )
        return self._append(event)

    def _invalidate_components(
        self,
        projection: OrderLifecycleProjection,
        components: Iterable[LatencyComponent],
        integrity_event: LedgerEvent,
    ) -> None:
        wanted = set(components)
        for measurement in self._measurements(projection):
            if measurement.component not in wanted:
                continue
            sample_id = self._sample_id(
                projection.root.decision_id,
                measurement.order_id,
                measurement.component,
                measurement.source_event.event_id,
            )
            try:
                self.history.invalidate(
                    sample_id,
                    invalidated_at=integrity_event.occurred_at,
                    reason="terminal_cross_event_integrity_failure",
                    integrity_event_id=integrity_event.event_id,
                )
            except KeyError:
                pass

    def _append_execution_evaluation(
        self,
        projection: OrderLifecycleProjection,
        audit_issues: Sequence[str],
    ) -> AppendResult | None:
        if projection.terminal is None or projection.submission is None:
            return None
        source_events = [
            event
            for event in self.ledger.read(projection.root.decision_id)
            if event.event_type
            in {
                EventType.ORDER_SUBMITTED,
                EventType.BROKER_ACKNOWLEDGED,
                EventType.FILL_RECEIVED,
                EventType.ORDER_TERMINAL,
                EventType.LIFECYCLE_INTEGRITY_FAILURE,
            }
        ]
        signature = hashlib.sha256(
            "\x1f".join(event.event_id for event in source_events).encode("utf-8")
        ).hexdigest()
        key = f"execution_evaluated:{projection.root.decision_id}:{signature}"
        if existing := self._find_idempotency(projection, key):
            return AppendResult(existing, appended=False)

        terminal_state = projection.terminal_state
        if terminal_state is TerminalState.FULLY_FILLED:
            outcome = ExecutionOutcome.FULLY_FILLED
        elif projection.executed_quantity > 0:
            outcome = ExecutionOutcome.PARTIALLY_FILLED_EXPIRED
        else:
            outcome = ExecutionOutcome.NO_FILL

        root_payload = projection.root.payload
        observation = root_payload.get("observation")
        observed_at = (
            normalize_timestamp(str(observation["observed_at"]))
            if isinstance(observation, Mapping) and observation.get("observed_at")
            else None
        )
        decision_at = normalize_timestamp(str(root_payload["decision_at"]))
        fill_payloads = [fill.event.payload for fill in projection.fills]
        fees = self._sum_optional(fill_payloads, "actual_fee")
        spread = self._sum_optional(fill_payloads, "actual_spread_result")
        slippage = self._sum_optional(fill_payloads, "actual_slippage")
        impact = self._sum_optional(fill_payloads, "actual_impact_proxy")
        shortfall = self._sum_optional(fill_payloads, "implementation_shortfall")
        cancellation_fee_raw = projection.terminal.payload.get("cancellation_fee")
        cancellation_fee = Decimal(str(cancellation_fee_raw)) if cancellation_fee_raw is not None else Decimal("0")
        realised_cost = (
            fees + shortfall + cancellation_fee
            if fees is not None and shortfall is not None
            else Decimal("0") if not projection.fills else None
        )
        supersedes = projection.latest_execution_evaluation
        occurred_at = normalize_timestamp(self.clock())
        event = LedgerEvent.create(
            event_type=EventType.EXECUTION_EVALUATED,
            occurred_at=occurred_at,
            recorded_at=self._recorded_at(occurred_at),
            decision_id=projection.root.decision_id,
            run_id=projection.root.run_id,
            signal_id=projection.root.signal_id,
            idempotency_key=key,
            causation_id=projection.terminal.event_id,
            payload={
                "lifecycle_version": STEP3_LIFECYCLE_VERSION,
                "evaluation_version": STEP3_EXECUTION_EVALUATION_VERSION,
                "order_id": projection.order_id,
                "execution_outcome": outcome.value,
                "terminal_state": terminal_state.value if terminal_state else None,
                "realised_decision_latency_ms": self._elapsed_or_none(decision_at, observed_at),
                "realised_submission_latency_ms": self._elapsed_or_none(projection.submitted_at, decision_at),
                "realised_acknowledgement_latency_ms": self._elapsed_or_none(
                    projection.acknowledged_at, projection.submitted_at
                ),
                "realised_first_fill_latency_ms": self._elapsed_or_none(
                    projection.first_fill_at, projection.submitted_at
                ),
                "realised_final_fill_latency_ms": self._elapsed_or_none(
                    projection.final_fill_at, projection.first_fill_at
                ),
                "realised_total_latency_ms": (
                    self._elapsed_or_none(projection.final_fill_at, decision_at)
                    if projection.executed_quantity > 0
                    else None
                ),
                "actual_fees": decimal_text(fees),
                "actual_spread_result": decimal_text(spread),
                "actual_slippage": decimal_text(slippage),
                "actual_impact_proxy": decimal_text(impact),
                "implementation_shortfall": decimal_text(shortfall),
                "cancellation_fee": decimal_text(cancellation_fee if cancellation_fee_raw is not None else None),
                "realised_execution_cost": decimal_text(realised_cost),
                "execution_cost_convention": "fees_plus_implementation_shortfall_plus_actual_cancellation_fee",
                "executed_quantity": decimal_text(projection.executed_quantity),
                "unfilled_quantity": decimal_text(projection.unfilled_quantity or Decimal("0")),
                "realised_trade_pnl": None,
                "strategy_outcome_calculated": False,
                "lifecycle_audit": {
                    "audit_version": STEP3_AUDIT_VERSION,
                    "valid": not audit_issues,
                    "issues": list(audit_issues),
                    "audited_event_ids": [event.event_id for event in source_events],
                },
                "supersedes_execution_evaluation_event_id": supersedes.event_id if supersedes else None,
            },
            source_metadata={"ingestion_clock_source": self.clock_source},
            integrity_metadata={
                "step": "phase8-step3",
                "actual_execution_only": True,
                "strategy_pnl_claimed": False,
            },
        )
        return self._append(event)

    @staticmethod
    def _elapsed_or_none(later: datetime | None, earlier: datetime | None) -> str | None:
        if later is None or earlier is None:
            return None
        value = elapsed_ms_exact(later, earlier)
        return decimal_text(value) if value >= 0 else None

    @staticmethod
    def _sum_optional(payloads: Sequence[Mapping[str, object]], key: str) -> Decimal | None:
        if not payloads:
            return Decimal("0")
        values = [payload.get(key) for payload in payloads]
        if any(value is None for value in values):
            return None
        return sum((Decimal(str(value)) for value in values), Decimal("0"))

    @staticmethod
    def _implementation_shortfall(observation: FillObservation) -> tuple[Decimal | None, str]:
        if observation.implementation_shortfall is not None:
            return observation.implementation_shortfall, "adapter_actual"
        if observation.decision_price is None:
            return None, "unavailable"
        if observation.side is SignalSide.BUY:
            value = (observation.price - observation.decision_price) * observation.quantity
        else:
            value = (observation.decision_price - observation.price) * observation.quantity
        return value, "derived_from_actual_decision_and_fill_prices"

    @staticmethod
    def _find_idempotency(projection: OrderLifecycleProjection, key: str) -> LedgerEvent | None:
        for event in (
            projection.root,
            *(projection.acknowledgements),
            *(fill.event for fill in projection.fills),
            *(projection.integrity_failures),
            *(projection.execution_evaluations),
        ):
            if event.idempotency_key == key:
                return event
        if projection.submission and projection.submission.idempotency_key == key:
            return projection.submission
        if projection.terminal and projection.terminal.idempotency_key == key:
            return projection.terminal
        return None

    @staticmethod
    def _assert_order_correlation(projection: OrderLifecycleProjection, order_id: str) -> None:
        if projection.order_id is not None and projection.order_id != order_id:
            raise LifecycleConflictError("callback order_id differs from the immutable submission")

    @staticmethod
    def _assert_same_submission(event: LedgerEvent, observation: SubmissionObservation) -> None:
        expected = (
            observation.order_id,
            observation.client_order_id,
            utc_iso(observation.submitted_at),
            decimal_text(observation.quantity),
            observation.order_type,
            decimal_text(observation.limit_price),
            decimal_text(observation.participation_limit),
            utc_iso(observation.expiry_at) if observation.expiry_at else None,
            observation.venue_reference,
        )
        actual = (
            event.payload.get("order_id"),
            event.payload.get("client_order_id"),
            event.payload.get("submitted_at"),
            event.payload.get("submitted_quantity"),
            event.payload.get("order_type"),
            event.payload.get("limit_price"),
            event.payload.get("participation_limit"),
            event.payload.get("expiry_at"),
            event.payload.get("venue_reference"),
        )
        if actual != expected:
            raise LifecycleConflictError("duplicate submission callback contains different immutable evidence")

    @staticmethod
    def _assert_same_acknowledgement(event: LedgerEvent, observation: AcknowledgementObservation) -> None:
        if (
            event.payload.get("order_id"),
            event.payload.get("acknowledgement_id"),
            event.payload.get("acknowledged_at"),
            event.payload.get("venue_reference"),
        ) != (
            observation.order_id,
            observation.acknowledgement_id,
            utc_iso(observation.acknowledged_at),
            observation.venue_reference,
        ):
            raise LifecycleConflictError("duplicate acknowledgement contains different immutable evidence")

    @staticmethod
    def _assert_same_fill(event: LedgerEvent, observation: FillObservation) -> None:
        expected = (
            observation.order_id,
            observation.fill_id,
            utc_iso(observation.fill_at),
            observation.side.value,
            decimal_text(observation.quantity),
            decimal_text(observation.price),
            decimal_text(observation.cumulative_filled_quantity),
            decimal_text(observation.unfilled_quantity),
        )
        actual = (
            event.payload.get("order_id"),
            event.payload.get("fill_id"),
            event.payload.get("fill_at"),
            event.payload.get("side"),
            event.payload.get("fill_quantity"),
            event.payload.get("fill_price"),
            event.payload.get("cumulative_filled_quantity"),
            event.payload.get("unfilled_quantity"),
        )
        if actual != expected:
            raise LifecycleConflictError("duplicate fill identifier contains different immutable evidence")

    @staticmethod
    def _terminal_matches(event: LedgerEvent, observation: TerminalObservation) -> bool:
        return (
            event.payload.get("order_id"),
            event.payload.get("terminal_at"),
            event.payload.get("terminal_state"),
            event.payload.get("reason_code"),
            event.payload.get("executed_quantity"),
            event.payload.get("unfilled_quantity"),
        ) == (
            observation.order_id,
            utc_iso(observation.terminal_at),
            observation.terminal_state.value,
            observation.reason_code.value,
            decimal_text(observation.executed_quantity),
            decimal_text(observation.unfilled_quantity),
        )

    @staticmethod
    def _result(event: LedgerEvent, appended: bool, released: Sequence[str]) -> CallbackResult:
        return CallbackResult(
            event_id=event.event_id,
            appended=appended,
            aggregate_version=event.aggregate_version,
            released_sample_ids=tuple(released),
        )
