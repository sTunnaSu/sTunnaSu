"""Immutable Phase 8 Step 4 strategy-outcome evaluation service."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Mapping

from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.errors import (
    ConcurrentAppendError,
    IdempotencyConflictError,
    OutcomeConflictError,
    OutcomeEvidenceError,
    OutcomeNotDueError,
)
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.lifecycle import TerminalState, decimal_text, exact_decimal
from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.outcomes import (
    STEP4_OUTCOME_EVENT_VERSION,
    CounterfactualType,
    OutcomeCallbackResult,
    OutcomeTrigger,
    OutcomeTriggerType,
    OutcomeType,
    ReferencePriceEvidence,
    Step4OutcomePolicy,
)
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.ports.ledger import AppendResult, EventLedger
from src.latency_budgeter.ports.outcomes import (
    OutcomeReferencePriceProvider,
    ReferencePriceRequest,
)
from src.latency_budgeter.projections.order_lifecycle import (
    OrderLifecycleProjection,
    OrderLifecycleProjector,
)

OutcomeClock = Callable[[], datetime]
_BPS = Decimal("10000")


class OutcomeEvaluationService:
    """Append one frozen-horizon or actual-exit outcome per decision.

    The immutable decision root supplies horizon, counterfactual, cost and
    configuration evidence.  The injected policy supplies a pre-registered
    methodology identity; neither can be changed after seeing the outcome.
    """

    def __init__(
        self,
        *,
        policy: Step4OutcomePolicy,
        ledger: EventLedger,
        clock: OutcomeClock | None = None,
        clock_source: str = "system_utc_clock",
        max_append_retries: int = 20,
    ) -> None:
        if not clock_source:
            raise ValueError("clock_source is required")
        self.policy = policy
        self.ledger = ledger
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.clock_source = str(clock_source)
        self.max_append_retries = max(int(max_append_retries), 1)
        self.lifecycle_projector = OrderLifecycleProjector()

    def evaluate_due(
        self,
        decision_id: str,
        *,
        provider: OutcomeReferencePriceProvider,
        dataset_version: str,
    ) -> OutcomeCallbackResult | None:
        """Resolve and evaluate one fixed horizon, or return ``None`` if unavailable."""
        projection = self._projection(decision_id)
        root = projection.root
        config = self._root_config(root)
        decision_at = self._decision_at(root)
        now = normalize_timestamp(self.clock())
        horizon = config.outcome_horizon
        if horizon.unit == "milliseconds":
            target_at = decision_at + timedelta(milliseconds=horizon.value)
            if now < target_at:
                return None
        elif horizon.unit == "seconds":
            target_at = decision_at + timedelta(seconds=horizon.value)
            if now < target_at:
                return None
        request = ReferencePriceRequest(
            decision_id=decision_id,
            symbol=self._symbol(root),
            decision_at=decision_at,
            horizon_value=horizon.value,
            horizon_unit=horizon.unit,
            reference_convention=config.counterfactual_methodology.reference_price,
            dataset_version=dataset_version,
        )
        reference = provider.resolve(request)
        if reference is None:
            return None
        if reference.dataset_version != dataset_version:
            raise OutcomeEvidenceError("reference provider returned a different dataset version")
        trigger_material = "\x1f".join(
            (decision_id, self.policy.fingerprint, reference.query_fingerprint)
        )
        trigger = OutcomeTrigger(
            trigger_id=f"horizon_{hashlib.sha256(trigger_material.encode('utf-8')).hexdigest()}",
            trigger_type=OutcomeTriggerType.FIXED_HORIZON,
            triggered_at=now,
            reference_price=reference,
        )
        return self.evaluate(decision_id, trigger)

    def evaluate(
        self,
        decision_id: str,
        trigger: OutcomeTrigger,
    ) -> OutcomeCallbackResult:
        """Validate one trigger and append a separate ``outcome_evaluated`` event."""
        projection = self._projection(decision_id)
        root = projection.root
        existing = self._outcome_from_ledger(decision_id)
        if existing is not None:
            self._assert_same_evaluation(existing, trigger)
            return self._result(existing, appended=False)

        config = self._root_config(root)
        decision_at = self._decision_at(root)
        if self.policy.preregistered_at >= decision_at:
            raise OutcomeEvidenceError("outcome methodology was not registered before the frozen decision")
        self._validate_trigger(root, projection, config, trigger)
        payload = self._build_payload(root, projection, config, trigger)
        recorded_at = normalize_timestamp(self.clock())
        if recorded_at < trigger.triggered_at:
            raise OutcomeEvidenceError("evaluation clock precedes the immutable trigger")
        event = LedgerEvent.create(
            event_type=EventType.OUTCOME_EVALUATED,
            occurred_at=trigger.triggered_at,
            recorded_at=recorded_at,
            decision_id=root.decision_id,
            run_id=root.run_id,
            signal_id=root.signal_id,
            idempotency_key=f"outcome_evaluated:{root.decision_id}:{self.policy.fingerprint}",
            causation_id=self._causation_event(projection).event_id,
            payload=payload,
            source_metadata={
                "timestamp_source": "outcome_scheduler",
                "ingestion_clock_source": self.clock_source,
                "reference_price_source": trigger.reference_price.to_dict(),
            },
            integrity_metadata={
                "step": "phase8-step4",
                "forecast_rewritten": False,
                "execution_evaluation_rewritten": False,
                "diagnostic_in_realised_pnl": False,
            },
        )
        try:
            result = self._append(event)
        except IdempotencyConflictError as exc:
            raced = self._outcome_from_ledger(decision_id)
            if raced is None:
                raise
            try:
                self._assert_same_evaluation(raced, trigger)
            except OutcomeConflictError:
                raise OutcomeConflictError("concurrent outcome trigger conflicts with prior evidence") from exc
            return self._result(raced, appended=False)
        return self._result(result.event, result.appended)

    def _validate_trigger(
        self,
        root: LedgerEvent,
        projection: OrderLifecycleProjection,
        config: LatencyBudgetConfig,
        trigger: OutcomeTrigger,
    ) -> None:
        reference = trigger.reference_price
        if reference.symbol != self._symbol(root):
            raise OutcomeEvidenceError("reference price symbol differs from frozen observation")
        decision_at = self._decision_at(root)
        if trigger.trigger_type is OutcomeTriggerType.FIXED_HORIZON:
            expected_convention = config.counterfactual_methodology.reference_price
            if reference.reference_convention != expected_convention:
                raise OutcomeEvidenceError("reference-price convention drifted from frozen configuration")
            horizon = config.outcome_horizon
            if horizon.unit == "milliseconds":
                target = decision_at + timedelta(milliseconds=horizon.value)
                self._validate_time_horizon(reference, target)
            elif horizon.unit == "seconds":
                target = decision_at + timedelta(seconds=horizon.value)
                self._validate_time_horizon(reference, target)
            else:
                if reference.bars_elapsed != horizon.value:
                    raise OutcomeNotDueError("frozen bar horizon has not been reached exactly")
                if reference.target_at != reference.observed_at:
                    raise OutcomeEvidenceError("bar-horizon target must identify the selected horizon bar")
        else:
            if str(root.payload.get("decision", "")) != "ALLOW":
                raise OutcomeEvidenceError("rejected or deferred signals cannot have an actual strategy exit")
            if projection.executed_quantity <= 0 or projection.first_fill_at is None:
                raise OutcomeEvidenceError("actual exit requires actual executed quantity")
            if trigger.actual_exit_quantity != projection.executed_quantity:
                raise OutcomeEvidenceError("actual exit quantity differs from the executed quantity basis")
            if reference.reference_convention != "actual_exit_fill":
                raise OutcomeEvidenceError("actual exit must use actual_exit_fill evidence")
            if reference.observed_at < projection.first_fill_at:
                raise OutcomeNotDueError("actual strategy exit precedes the first fill")
            if reference.target_at != reference.observed_at:
                raise OutcomeEvidenceError("actual exit target and represented fill timestamp must match")

    @staticmethod
    def _validate_time_horizon(reference: ReferencePriceEvidence, target: datetime) -> None:
        target = normalize_timestamp(target)
        if reference.target_at != target:
            raise OutcomeEvidenceError("time-horizon target drifted from the frozen horizon")
        if reference.observed_at < target:
            raise OutcomeNotDueError("reference observation precedes the frozen horizon")

    def _build_payload(
        self,
        root: LedgerEvent,
        projection: OrderLifecycleProjection,
        config: LatencyBudgetConfig,
        trigger: OutcomeTrigger,
    ) -> dict[str, object]:
        decision = str(root.payload.get("decision", ""))
        if decision not in {"ALLOW", "REJECT", "DEFER"}:
            raise OutcomeEvidenceError("decision root is not finalised for Step 4")
        execution = projection.latest_execution_evaluation
        terminal = projection.terminal
        executed_quantity = projection.executed_quantity
        realised = executed_quantity > 0
        counterfactual = CounterfactualType.NONE
        diagnostic_label: str | None = None
        diagnostic_enabled = config.counterfactual_methodology.method == "fixed_horizon_markout"

        if decision in {"REJECT", "DEFER"}:
            outcome_type = OutcomeType.SIMULATED_DIAGNOSTIC
            counterfactual = CounterfactualType.REJECTED_SIGNAL
            diagnostic_label = "simulated diagnostic; not actual execution, fill, or realised trade"
        elif realised:
            outcome_type = OutcomeType.REALISED_EXECUTED
        else:
            outcome_type = OutcomeType.NO_REALISED_EXECUTION
            if (
                terminal is not None
                and projection.terminal_state is TerminalState.EXPIRED_UNFILLED
                and diagnostic_enabled
            ):
                counterfactual = CounterfactualType.APPROVED_UNFILLED
                diagnostic_label = (
                    "simulated unfilled-approved-opportunity diagnostic; "
                    "not rejected, not an actual fill, and not a realised trade"
                )

        realised_values = self._realised_values(projection, trigger) if realised else self._null_realised_values()
        diagnostic_values = (
            self._counterfactual_values(root, config, trigger)
            if counterfactual is not CounterfactualType.NONE and diagnostic_enabled
            else self._null_diagnostic_values(
                "disabled_by_frozen_policy" if counterfactual is not CounterfactualType.NONE else "not_applicable"
            )
        )
        comparison = self._forecast_comparison(root, projection, realised_values)
        horizon = config.outcome_horizon
        return {
            "outcome_event_version": STEP4_OUTCOME_EVENT_VERSION,
            "decision_id": root.decision_id,
            "evaluation_trigger": {
                "trigger_id": trigger.trigger_id,
                "trigger_type": trigger.trigger_type.value,
                "triggered_at": utc_iso(trigger.triggered_at),
                "actual_exit_id": trigger.actual_exit_id,
            },
            "evaluation_horizon": {
                "value": horizon.value,
                "unit": horizon.unit,
                "horizon_version": self.policy.horizon_version,
                "decision_anchor_at": utc_iso(self._decision_at(root)),
                "reference_observed_at": utc_iso(trigger.reference_price.observed_at),
                "bars_elapsed": trigger.reference_price.bars_elapsed,
            },
            "reference_prices": {
                "decision_reference_price": self._decision_reference_text(root),
                "actual_weighted_fill_price": realised_values["actual_weighted_fill_price"],
                "outcome_reference_price": format(trigger.reference_price.price, "f"),
                "reference_convention": trigger.reference_price.reference_convention,
            },
            "quantity_basis": {
                "method": self.policy.quantity_basis,
                "executed_quantity": decimal_text(executed_quantity),
                "unfilled_quantity": decimal_text(projection.unfilled_quantity),
                "partial_fill": realised and (projection.unfilled_quantity or Decimal("0")) > 0,
            },
            "outcome_type": outcome_type.value,
            "counterfactual_type": counterfactual.value,
            "counterfactual_label": diagnostic_label,
            "is_actual_execution": outcome_type is OutcomeType.REALISED_EXECUTED,
            "is_actual_fill": outcome_type is OutcomeType.REALISED_EXECUTED,
            "is_realised_trade": outcome_type is OutcomeType.REALISED_EXECUTED,
            "realised_strategy_outcome": realised_values,
            "simulated_diagnostic": diagnostic_values,
            "forecast_versus_reality": comparison,
            "data_provenance": trigger.reference_price.to_dict(),
            "config_version": config.config_version,
            "config_fingerprint": config.fingerprint,
            "outcome_policy": self.policy.to_dict(),
            "outcome_policy_fingerprint": self.policy.fingerprint,
            "evaluation_methodology_version": self.policy.methodology_version,
            "frozen_counterfactual_methodology": config.counterfactual_methodology.model_dump(mode="json"),
            "cost_convention": self._cost_convention(root, config, realised),
            "original_forecast_rewritten": False,
            "execution_evaluation_event_id": execution.event_id if execution else None,
            "terminal_event_id": terminal.event_id if terminal else None,
        }

    def _realised_values(
        self,
        projection: OrderLifecycleProjection,
        trigger: OutcomeTrigger,
    ) -> dict[str, object]:
        if not projection.fills:
            return self._null_realised_values()
        quantity = projection.executed_quantity
        weighted_fill = sum(
            (fill.price * fill.quantity for fill in projection.fills), Decimal("0")
        ) / quantity
        side = SignalSide(str(projection.fills[0].side))
        direction = Decimal("1") if side is SignalSide.BUY else Decimal("-1")
        reference = trigger.reference_price.price
        gross_amount = direction * (reference - weighted_fill) * quantity
        gross_return_bps = direction * ((reference / weighted_fill) - Decimal("1")) * _BPS
        execution = projection.latest_execution_evaluation
        execution_quality_cost = self._optional_decimal(
            execution.payload.get("realised_execution_cost") if execution else None
        )
        actual_fees = self._optional_decimal(execution.payload.get("actual_fees") if execution else None)
        cancellation_fee = self._optional_decimal(
            execution.payload.get("cancellation_fee") if execution else None
        )
        exit_cost = trigger.actual_exit_cost or Decimal("0")
        explicit_entry_cost = (
            actual_fees + (cancellation_fee or Decimal("0")) if actual_fees is not None else None
        )
        pnl_cost = explicit_entry_cost + exit_cost if explicit_entry_cost is not None else None
        net_amount = gross_amount - pnl_cost if pnl_cost is not None else None
        entry_notional = weighted_fill * quantity
        net_return_bps = net_amount / entry_notional * _BPS if net_amount is not None else None
        return {
            "outcome_status": "OBSERVED",
            "outcome_basis": (
                "actual_exit_return"
                if trigger.trigger_type is OutcomeTriggerType.ACTUAL_STRATEGY_EXIT
                else "fixed_horizon_markout"
            ),
            "actual_weighted_fill_price": decimal_text(weighted_fill),
            "observed_post_fill_return_bps": decimal_text(gross_return_bps),
            "gross_outcome_amount": decimal_text(gross_amount),
            "gross_outcome_bps": decimal_text(gross_return_bps),
            "actual_entry_execution_quality_cost": decimal_text(execution_quality_cost),
            "actual_entry_fees_and_cancellation": decimal_text(explicit_entry_cost),
            "actual_exit_cost": decimal_text(trigger.actual_exit_cost),
            "costs_applied_to_post_fill_pnl": decimal_text(pnl_cost),
            "costs_applied": decimal_text(pnl_cost),
            "net_outcome_amount": decimal_text(net_amount),
            "net_outcome_bps": decimal_text(net_return_bps),
            "realised_strategy_pnl": decimal_text(net_amount),
        }

    @staticmethod
    def _null_realised_values() -> dict[str, object]:
        return {
            "outcome_status": "NO_ACTUAL_FILL",
            "outcome_basis": None,
            "actual_weighted_fill_price": None,
            "observed_post_fill_return_bps": None,
            "gross_outcome_amount": None,
            "gross_outcome_bps": None,
            "actual_entry_execution_quality_cost": None,
            "actual_entry_fees_and_cancellation": None,
            "actual_exit_cost": None,
            "costs_applied_to_post_fill_pnl": None,
            "costs_applied": None,
            "net_outcome_amount": None,
            "net_outcome_bps": None,
            "realised_strategy_pnl": None,
        }

    def _counterfactual_values(
        self,
        root: LedgerEvent,
        config: LatencyBudgetConfig,
        trigger: OutcomeTrigger,
    ) -> dict[str, object]:
        start = self._decision_reference(root)
        if start is None:
            return self._null_diagnostic_values("original_decision_reference_price_unavailable")
        side = self._side(root)
        direction = Decimal("1") if side is SignalSide.BUY else Decimal("-1")
        gross_bps = direction * ((trigger.reference_price.price / start) - Decimal("1")) * _BPS
        cost_bps, source = self._frozen_counterfactual_cost_bps(root, config)
        net_bps = gross_bps - cost_bps
        return {
            "diagnostic_status": "SIMULATED",
            "diagnostic_methodology": config.counterfactual_methodology.methodology_version,
            "not_actual_execution": True,
            "not_actual_fill": True,
            "not_realised_trade": True,
            "start_reference_price": decimal_text(start),
            "horizon_reference_price": decimal_text(trigger.reference_price.price),
            "simulated_gross_outcome_bps": decimal_text(gross_bps),
            "frozen_simulated_cost_bps": decimal_text(cost_bps),
            "frozen_cost_source": source,
            "simulated_net_outcome_bps": decimal_text(net_bps),
            "simulated_outcome_amount": None,
        }

    @staticmethod
    def _null_diagnostic_values(status: str) -> dict[str, object]:
        return {
            "diagnostic_status": status,
            "diagnostic_methodology": None,
            "not_actual_execution": True,
            "not_actual_fill": True,
            "not_realised_trade": True,
            "start_reference_price": None,
            "horizon_reference_price": None,
            "simulated_gross_outcome_bps": None,
            "frozen_simulated_cost_bps": None,
            "frozen_cost_source": None,
            "simulated_net_outcome_bps": None,
            "simulated_outcome_amount": None,
        }

    def _forecast_comparison(
        self,
        root: LedgerEvent,
        projection: OrderLifecycleProjection,
        realised: Mapping[str, object],
    ) -> dict[str, object]:
        economics = root.payload.get("decision_economics")
        execution = projection.latest_execution_evaluation
        if not isinstance(economics, Mapping) or execution is None or projection.executed_quantity <= 0:
            return {
                "comparison_status": "UNAVAILABLE_NO_REALISED_EXECUTION_OR_FORECAST",
                "latency": self._null_error("actual minus forecast; positive means slower"),
                "cost": self._null_error("actual minus forecast; positive means more expensive"),
                "edge_at_fill": self._null_error("observed gross minus predicted edge-at-fill"),
                "net_edge": self._null_error("realised net minus predicted net edge"),
                "missing_value_policy": "counterfactuals never substitute for realised calibration",
            }
        latency_pairs = (
            ("decision", "forecast_decision_latency_ms", "realised_decision_latency_ms"),
            ("submission", "forecast_submission_latency_ms", "realised_submission_latency_ms"),
            ("acknowledgement", "forecast_ack_latency_ms", "realised_acknowledgement_latency_ms"),
            ("fill", "forecast_fill_latency_ms", "realised_first_fill_latency_ms"),
        )
        forecast_sum = Decimal("0")
        realised_sum = Decimal("0")
        included: list[str] = []
        missing: list[str] = ["risk_processing"]
        for name, forecast_key, realised_key in latency_pairs:
            forecast = self._optional_decimal(economics.get(forecast_key))
            actual = self._optional_decimal(execution.payload.get(realised_key))
            if forecast is None or actual is None:
                missing.append(name)
                continue
            forecast_sum += forecast
            realised_sum += actual
            included.append(name)
        latency = (
            self._error_payload(
                realised_sum,
                forecast_sum,
                direction="actual minus forecast; positive means slower",
                extra={"included_components": included, "missing_components": missing},
            )
            if included
            else self._null_error("actual minus forecast; positive means slower")
        )
        executed_notional = sum(
            (fill.price * fill.quantity for fill in projection.fills), Decimal("0")
        )
        actual_cost_amount = self._optional_decimal(execution.payload.get("realised_execution_cost"))
        actual_cost_bps = (
            actual_cost_amount / executed_notional * _BPS
            if actual_cost_amount is not None and executed_notional > 0
            else None
        )
        estimated_cost_bps = self._optional_decimal(economics.get("estimated_cost_bps"))
        cost = (
            self._error_payload(
                actual_cost_bps,
                estimated_cost_bps,
                direction="actual minus forecast; positive means more expensive",
            )
            if actual_cost_bps is not None and estimated_cost_bps is not None
            else self._null_error("actual minus forecast; positive means more expensive")
        )
        gross_actual = self._optional_decimal(realised.get("gross_outcome_bps"))
        net_actual = self._optional_decimal(realised.get("net_outcome_bps"))
        edge_forecast = self._optional_decimal(economics.get("edge_at_fill_bps"))
        net_forecast = self._optional_decimal(economics.get("net_edge_bps"))
        return {
            "comparison_status": "REALISED_COMPONENT_ALIGNED",
            "latency": latency,
            "cost": cost,
            "edge_at_fill": (
                self._error_payload(
                    gross_actual,
                    edge_forecast,
                    direction="observed gross minus predicted edge-at-fill",
                )
                if gross_actual is not None and edge_forecast is not None
                else self._null_error("observed gross minus predicted edge-at-fill")
            ),
            "net_edge": (
                self._error_payload(
                    net_actual,
                    net_forecast,
                    direction="realised net minus predicted net edge",
                )
                if net_actual is not None and net_forecast is not None
                else self._null_error("realised net minus predicted net edge")
            ),
            "missing_value_policy": (
                "compare only semantically aligned realised components; unsupported acknowledgements "
                "remain missing; counterfactuals never enter calibration"
            ),
        }

    @staticmethod
    def _error_payload(
        actual: Decimal,
        forecast: Decimal,
        *,
        direction: str,
        extra: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        signed = actual - forecast
        payload: dict[str, object] = {
            "forecast": decimal_text(forecast),
            "actual": decimal_text(actual),
            "signed_error": decimal_text(signed),
            "absolute_error": decimal_text(abs(signed)),
            "error_direction": direction,
        }
        payload.update(extra or {})
        return payload

    @staticmethod
    def _null_error(direction: str) -> dict[str, object]:
        return {
            "forecast": None,
            "actual": None,
            "signed_error": None,
            "absolute_error": None,
            "error_direction": direction,
        }

    @staticmethod
    def _optional_decimal(value: object) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    @staticmethod
    def _decision_at(root: LedgerEvent) -> datetime:
        value = root.payload.get("decision_at")
        if value is None:
            raise OutcomeEvidenceError("decision root lacks decision_at")
        return normalize_timestamp(str(value))

    @staticmethod
    def _symbol(root: LedgerEvent) -> str:
        observation = root.payload.get("observation")
        if not isinstance(observation, Mapping) or not observation.get("symbol"):
            raise OutcomeEvidenceError("decision root lacks a frozen symbol")
        return str(observation["symbol"])

    @staticmethod
    def _side(root: LedgerEvent) -> SignalSide:
        observation = root.payload.get("observation")
        if not isinstance(observation, Mapping):
            raise OutcomeEvidenceError("decision root lacks source observation")
        side = SignalSide(str(observation.get("side", "")))
        if side is SignalSide.FLAT:
            raise OutcomeEvidenceError("flat signals have no directional outcome")
        return side

    @staticmethod
    def _root_config(root: LedgerEvent) -> LatencyBudgetConfig:
        raw = root.payload.get("phase8_config")
        if not isinstance(raw, Mapping):
            raise OutcomeEvidenceError("decision root lacks its frozen Phase 8 configuration")
        config = LatencyBudgetConfig.model_validate(dict(raw))
        if root.payload.get("config_fingerprint") != config.fingerprint:
            raise OutcomeEvidenceError("frozen configuration fingerprint does not match its snapshot")
        return config

    @staticmethod
    def _decision_reference(root: LedgerEvent) -> Decimal | None:
        evidence = root.payload.get("gross_edge_evidence")
        if not isinstance(evidence, Mapping) or evidence.get("reference_price") is None:
            return None
        return exact_decimal(evidence["reference_price"], label="decision reference price", allow_zero=False)

    def _decision_reference_text(self, root: LedgerEvent) -> str | None:
        return decimal_text(self._decision_reference(root))

    @staticmethod
    def _frozen_counterfactual_cost_bps(
        root: LedgerEvent,
        config: LatencyBudgetConfig,
    ) -> tuple[Decimal, str]:
        economics = root.payload.get("decision_economics")
        if isinstance(economics, Mapping) and economics.get("estimated_cost_bps") is not None:
            return Decimal(str(economics["estimated_cost_bps"])), "immutable_decision_economics"
        costs = config.cost_assumptions
        total = Decimal(str(costs.taker_fee_bps))
        total += Decimal(str(costs.spread_bps))
        total += Decimal(str(costs.slippage_bps))
        total += Decimal(str(costs.impact_bps))
        if costs.convention == "round_trip_components":
            total *= Decimal("2")
        return total, "frozen_conservative_taker_cost_assumptions"

    @staticmethod
    def _cost_convention(
        root: LedgerEvent,
        config: LatencyBudgetConfig,
        realised: bool,
    ) -> dict[str, object]:
        economics = root.payload.get("decision_economics")
        return {
            "realised_values_use": (
                "actual fill price plus explicit entry fees/cancellation and actual exit cost; "
                "implementation shortfall is not subtracted twice"
                if realised
                else "not applicable"
            ),
            "simulated_values_use": "frozen decision cost or conservative frozen taker assumptions",
            "frozen_component_convention": config.cost_assumptions.convention,
            "frozen_estimated_cost_bps": (
                economics.get("estimated_cost_bps") if isinstance(economics, Mapping) else None
            ),
            "diagnostic_costs_never_enter_realised_pnl": True,
        }

    @staticmethod
    def _causation_event(projection: OrderLifecycleProjection) -> LedgerEvent:
        return projection.latest_execution_evaluation or projection.terminal or projection.root

    def _projection(self, decision_id: str) -> OrderLifecycleProjection:
        events = self.ledger.read(decision_id)
        if not events:
            raise OutcomeEvidenceError(f"unknown decision_id: {decision_id}")
        return self.lifecycle_projector.replay(events)

    def _outcome_from_ledger(self, decision_id: str) -> LedgerEvent | None:
        outcomes = [
            event for event in self.ledger.read(decision_id) if event.event_type is EventType.OUTCOME_EVALUATED
        ]
        if len(outcomes) > 1:
            raise OutcomeConflictError("decision aggregate contains multiple outcome evaluations")
        return outcomes[0] if outcomes else None

    def _append(self, event: LedgerEvent) -> AppendResult:
        for _ in range(self.max_append_retries):
            current = self.ledger.read(event.decision_id)
            prior = next(
                (item for item in current if item.event_type is EventType.OUTCOME_EVALUATED),
                None,
            )
            if prior is not None:
                if prior.idempotency_key != event.idempotency_key:
                    raise OutcomeConflictError("an outcome already exists under a different frozen policy")
                if prior.semantic_fingerprint != event.semantic_fingerprint:
                    raise OutcomeConflictError("an outcome retry contains different immutable evidence")
                return AppendResult(prior, appended=False)
            try:
                return self.ledger.append(event, expected_version=len(current))
            except ConcurrentAppendError:
                continue
        raise ConcurrentAppendError("outcome append retry budget exhausted")

    def _assert_same_evaluation(self, event: LedgerEvent, trigger: OutcomeTrigger) -> None:
        trigger_payload = event.payload.get("evaluation_trigger")
        provenance = event.payload.get("data_provenance")
        if not isinstance(trigger_payload, Mapping) or not isinstance(provenance, Mapping):
            raise OutcomeConflictError("existing outcome lacks deterministic trigger evidence")
        if event.payload.get("outcome_policy_fingerprint") != self.policy.fingerprint:
            raise OutcomeConflictError("existing outcome used a different frozen evaluation policy")
        if (
            trigger_payload.get("trigger_id") != trigger.trigger_id
            or trigger_payload.get("trigger_type") != trigger.trigger_type.value
            or trigger_payload.get("actual_exit_id") != trigger.actual_exit_id
            or provenance.get("query_fingerprint") != trigger.reference_price.query_fingerprint
            or provenance.get("price") != format(trigger.reference_price.price, "f")
            or provenance.get("observed_at") != utc_iso(trigger.reference_price.observed_at)
            or provenance.get("target_at") != utc_iso(trigger.reference_price.target_at)
            or provenance.get("reference_convention") != trigger.reference_price.reference_convention
            or provenance.get("dataset_version") != trigger.reference_price.dataset_version
            or provenance.get("source_event_id") != trigger.reference_price.source_event_id
        ):
            raise OutcomeConflictError("duplicate scheduler trigger attempts horizon or evidence drift")
        if trigger.trigger_type is OutcomeTriggerType.ACTUAL_STRATEGY_EXIT:
            quantity_basis = event.payload.get("quantity_basis")
            realised = event.payload.get("realised_strategy_outcome")
            if not isinstance(quantity_basis, Mapping) or not isinstance(realised, Mapping):
                raise OutcomeConflictError("existing actual-exit outcome lacks quantity or cost evidence")
            recorded_quantity = self._optional_decimal(quantity_basis.get("executed_quantity"))
            recorded_exit_cost = self._optional_decimal(realised.get("actual_exit_cost")) or Decimal("0")
            requested_exit_cost = trigger.actual_exit_cost or Decimal("0")
            if (
                recorded_quantity != trigger.actual_exit_quantity
                or recorded_exit_cost != requested_exit_cost
            ):
                raise OutcomeConflictError("actual-exit retry attempts quantity or cost drift")

    @staticmethod
    def _result(event: LedgerEvent, appended: bool) -> OutcomeCallbackResult:
        return OutcomeCallbackResult(
            event_id=event.event_id,
            appended=appended,
            aggregate_version=event.aggregate_version,
            outcome_type=OutcomeType(str(event.payload["outcome_type"])),
            counterfactual_type=CounterfactualType(str(event.payload["counterfactual_type"])),
        )
