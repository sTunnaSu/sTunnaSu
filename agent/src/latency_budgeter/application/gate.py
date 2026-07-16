"""Phase 8 Step 2 enabled decision-gate orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping

from src.latency_budgeter.application.classification import SharedCohortClassifier
from src.latency_budgeter.application.intake import Phase8IntakeService, PreparedIntake
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import (
    STEP2_EVALUATION_VERSION,
    ApprovedOpportunity,
    ComponentEstimateMode,
    ComponentForecast,
    DecisionEconomics,
    DecisionOutcome,
    DecisionReason,
    EstimatorMode,
    ExecutionCostEstimate,
    LatencyForecast,
    LiquidityRole,
    PreDecisionLatencyMeasurements,
)
from src.latency_budgeter.domain.errors import (
    ConcurrentAppendError,
    ConfigDriftError,
    DecisionEvidenceError,
    IdempotencyConflictError,
    InsufficientLatencyHistory,
)
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.history import LatencyComponent
from src.latency_budgeter.domain.models import MarketObservation, SignalSide
from src.latency_budgeter.domain.timestamps import TIMESTAMP_PRECISION, normalize_timestamp
from src.latency_budgeter.domain.values import BasisPoints, Milliseconds
from src.latency_budgeter.estimation.costs import FrozenExecutionCostEstimator
from src.latency_budgeter.estimation.decay import exponential_decay
from src.latency_budgeter.estimation.forecast import PriorOnlyLatencyForecaster
from src.latency_budgeter.policies.decision import decide_net_edge
from src.latency_budgeter.policies.timestamps import (
    TimestampValidation,
    is_fresh,
    validate_pre_decision_timestamps,
)
from src.latency_budgeter.ports.approved import ApprovedOpportunityPort
from src.latency_budgeter.ports.costs import ExecutionCostEstimator
from src.latency_budgeter.ports.edge import GrossEdgeEstimate, GrossEdgeEstimator
from src.latency_budgeter.ports.history import LatencyHistoryStore
from src.latency_budgeter.ports.ledger import EventLedger

DecisionClock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class GateResult:
    """Terminal Step 2 result; only ALLOW carries a Step 3 opportunity."""

    event: LedgerEvent
    outcome: DecisionOutcome
    reason: DecisionReason
    approved_opportunity: ApprovedOpportunity | None
    appended: bool
    approved_signal_count_increment: int


class LatencyBudgetDecisionGate:
    """Coordinate pure Step 2 policies and write one immutable root event."""

    def __init__(
        self,
        *,
        config: LatencyBudgetConfig,
        ledger: EventLedger,
        history: LatencyHistoryStore,
        edge_estimator: GrossEdgeEstimator,
        approved_port: ApprovedOpportunityPort,
        clock: DecisionClock | None = None,
        clock_source: str = "system_utc_clock",
        classifier: SharedCohortClassifier | None = None,
        cost_estimator: ExecutionCostEstimator | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("the Step 2 decision gate requires enabled=True; disabled baseline remains unchanged")
        if not str(clock_source).strip():
            raise ValueError("clock_source is required")
        self.config = config
        self.ledger = ledger
        self.edge_estimator = edge_estimator
        self.approved_port = approved_port
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.clock_source = str(clock_source).strip()
        self.cost_estimator = cost_estimator or FrozenExecutionCostEstimator()
        self.forecaster = PriorOnlyLatencyForecaster(history)
        self.intake = Phase8IntakeService(
            config=config,
            ledger=ledger,
            classifier=classifier,
            clock=self.clock,
        )

    def evaluate(
        self,
        *,
        observation: MarketObservation,
        run_id: str,
        strategy_requirements_met: bool,
        liquidity_role: LiquidityRole = LiquidityRole.TAKER,
        measurements: PreDecisionLatencyMeasurements | None = None,
        signal_key: str = "default",
        signal_metadata: Mapping[str, Any] | None = None,
    ) -> GateResult:
        """Evaluate once at the injected clock instant, never submitting an order."""
        decision_at = normalize_timestamp(self.clock())
        prepared = self.intake.prepare_raw_signal(
            observation=observation,
            run_id=run_id,
            decision_at=decision_at,
            strategy_requirements_met=strategy_requirements_met,
            signal_key=signal_key,
            signal_metadata=signal_metadata,
        )
        existing = self.ledger.read(prepared.decision_id)
        if existing:
            return self._existing_result(existing[0])

        timestamp_validation = validate_pre_decision_timestamps(observation, decision_at)
        if not timestamp_validation.valid:
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.REJECT,
                reason=DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER,
                diagnostics={"timestamp_reasons": list(timestamp_validation.reasons)},
            )
        data_age = timestamp_validation.durations.data_age_ms
        if data_age is None:
            raise DecisionEvidenceError("valid timestamp evidence must contain data_age_ms")
        if not is_fresh(data_age, self.config.freshness_limit_ms):
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.REJECT,
                reason=DecisionReason.REJECT_STALE_DATA,
                diagnostics={
                    "data_age_ms": data_age.to_float(),
                    "freshness_limit_ms": self.config.freshness_limit_ms,
                    "freshness_boundary": "data_age_ms <= freshness_limit_ms",
                },
            )
        if not prepared.classification.common_phase8_eligible:
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.REJECT,
                reason=DecisionReason.REJECT_COMMON_COHORT_INELIGIBLE,
                diagnostics={"cohort_reasons": list(prepared.classification.reasons)},
            )

        edge = self.edge_estimator.estimate(
            observation=observation,
            signal=prepared.signal,
            decision_at=decision_at,
        )
        if edge.estimated_at > decision_at:
            timestamp_validation = TimestampValidation(
                valid=False,
                reasons=(*timestamp_validation.reasons, "gross_edge_estimated_after_decision"),
                durations=timestamp_validation.durations,
                signed_duration_diagnostics_ms=timestamp_validation.signed_duration_diagnostics_ms,
            )
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.REJECT,
                reason=DecisionReason.REJECT_INVALID_TIMESTAMP_ORDER,
                edge=edge,
                diagnostics={"timestamp_reasons": ["gross_edge_estimated_after_decision"]},
            )
        if edge.gross_edge_bps.value <= 0:
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.REJECT,
                reason=DecisionReason.REJECT_NONPOSITIVE_GROSS_EDGE,
                edge=edge,
            )

        try:
            forecast = self.forecaster.forecast(
                decision_at=decision_at,
                current_decision_id=prepared.decision_id,
                data_age_ms=data_age,
                config=self.config,
                measurements=measurements,
            )
        except InsufficientLatencyHistory as exc:
            return self._finalize(
                prepared=prepared,
                observation=observation,
                decision_at=decision_at,
                timestamp_validation=timestamp_validation,
                outcome=DecisionOutcome.DEFER,
                reason=DecisionReason.DEFER_COLD_START,
                edge=edge,
                diagnostics={"cold_start_detail": str(exc), "reconsideration_allowed": False},
            )

        tau = Milliseconds(self.config.tau_ms)
        decay = exponential_decay(forecast.total_ms, tau)
        edge_at_fill = edge.gross_edge_bps * Decimal(str(decay))
        costs = self.cost_estimator.estimate(
            assumptions=self.config.cost_assumptions,
            side=observation.side,
            liquidity_role=liquidity_role,
        )
        net_edge = edge_at_fill - costs.total_bps
        required_buffer = BasisPoints(self.config.required_buffer_bps)
        outcome, reason = decide_net_edge(net_edge, required_buffer)
        economics = DecisionEconomics(
            gross_edge_bps=edge.gross_edge_bps,
            latency_forecast=forecast,
            tau_ms=tau,
            predicted_decay_factor=decay,
            edge_at_fill_bps=edge_at_fill,
            costs=costs,
            net_edge_bps=net_edge,
            required_buffer_bps=required_buffer,
            config_version=self.config.config_version,
            config_fingerprint=self.config.fingerprint,
        )
        approved = None
        if outcome is DecisionOutcome.ALLOW:
            approved = ApprovedOpportunity(
                decision_id=prepared.decision_id,
                run_id=run_id,
                signal_id=prepared.signal.signal_id,
                symbol=observation.symbol,
                side=observation.side,
                decision_at=decision_at,
                economics=economics,
            )
        return self._finalize(
            prepared=prepared,
            observation=observation,
            decision_at=decision_at,
            timestamp_validation=timestamp_validation,
            outcome=outcome,
            reason=reason,
            edge=edge,
            economics=economics,
            approved=approved,
        )

    def _finalize(
        self,
        *,
        prepared: PreparedIntake,
        observation: MarketObservation,
        decision_at: datetime,
        timestamp_validation: TimestampValidation,
        outcome: DecisionOutcome,
        reason: DecisionReason,
        edge: GrossEdgeEstimate | None = None,
        economics: DecisionEconomics | None = None,
        approved: ApprovedOpportunity | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> GateResult:
        payload = dict(prepared.payload)
        payload.update(
            {
                "decision": outcome.value,
                "reason_code": reason.value,
                "decision_at": decision_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "evaluation_version": STEP2_EVALUATION_VERSION,
                "approved_signal_count_increment": 1 if outcome is DecisionOutcome.ALLOW else 0,
                "matched_primary_denominator_increment": (1 if prepared.classification.common_phase8_eligible else 0),
                "timestamp_validation": {
                    "valid": timestamp_validation.valid,
                    "reasons": list(timestamp_validation.reasons),
                    "durations": timestamp_validation.durations.to_dict(),
                    "signed_duration_diagnostics_ms": timestamp_validation.signed_duration_diagnostics_ms,
                },
                "gross_edge_evidence": (
                    {
                        "gross_edge_bps": edge.gross_edge_bps.to_float(),
                        "estimated_at": edge.estimated_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                        "estimator_version": edge.estimator_version,
                        "reference_price": str(edge.reference_price) if edge.reference_price is not None else None,
                    }
                    if edge is not None
                    else None
                ),
                "decision_economics": economics.to_dict() if economics is not None else None,
                "approved_opportunity": approved.to_dict() if approved is not None else None,
                "diagnostics": dict(diagnostics or {}),
                "execution_events_emitted": False,
            }
        )
        event = LedgerEvent.create(
            event_type=EventType.DECISION_CREATED,
            occurred_at=decision_at,
            recorded_at=decision_at,
            decision_id=prepared.decision_id,
            run_id=prepared.run_id,
            signal_id=prepared.signal.signal_id,
            payload=payload,
            idempotency_key=f"decision_created:{prepared.decision_id}",
            source_metadata=observation.source_metadata.to_dict(),
            integrity_metadata={
                "classification_version": prepared.classification.classification_version,
                "timestamp_precision": TIMESTAMP_PRECISION,
                "step": "phase8-step2",
                "evaluation_version": STEP2_EVALUATION_VERSION,
                "enforcement_applied": True,
                "decision_clock_source": self.clock_source,
                "config_fingerprint": self.config.fingerprint,
                "order_submission_capability": False,
            },
        )
        try:
            append = self.ledger.append(event, expected_version=0)
        except (ConcurrentAppendError, IdempotencyConflictError):
            existing = self.ledger.read(prepared.decision_id)
            if not existing:
                raise
            return self._existing_result(existing[0])
        published = False
        if append.appended and approved is not None:
            published = self.approved_port.publish(approved)
        return GateResult(
            event=append.event,
            outcome=outcome,
            reason=reason,
            approved_opportunity=approved,
            appended=append.appended,
            approved_signal_count_increment=1 if published else 0,
        )

    def _existing_result(self, event: LedgerEvent) -> GateResult:
        payload = event.payload
        if payload.get("config_fingerprint") != self.config.fingerprint:
            raise ConfigDriftError("the existing immutable decision used a different configuration fingerprint")
        if payload.get("evaluation_version") != STEP2_EVALUATION_VERSION:
            raise DecisionEvidenceError("an existing non-Step-2 root cannot be mutated into a Step 2 decision")
        outcome = DecisionOutcome(str(payload["decision"]))
        reason = DecisionReason(str(payload["reason_code"]))
        approved = self._approved_from_payload(event) if outcome is DecisionOutcome.ALLOW else None
        published = self.approved_port.publish(approved) if approved is not None else False
        return GateResult(
            event=event,
            outcome=outcome,
            reason=reason,
            approved_opportunity=approved,
            appended=False,
            approved_signal_count_increment=1 if published else 0,
        )

    @staticmethod
    def _approved_from_payload(event: LedgerEvent) -> ApprovedOpportunity:
        raw = event.payload.get("approved_opportunity")
        economics_raw = event.payload.get("decision_economics")
        if not isinstance(raw, Mapping) or not isinstance(economics_raw, Mapping):
            raise DecisionEvidenceError("ALLOW decision is missing frozen approved economics")
        component_rows = economics_raw.get("component_estimates", ())
        if not isinstance(component_rows, (tuple, list)):
            raise DecisionEvidenceError("component estimates are malformed")
        by_component = {str(row["component"]): row for row in component_rows if isinstance(row, Mapping)}

        def component(name: LatencyComponent, total_key: str) -> ComponentForecast:
            row = by_component.get(name.value)
            if not isinstance(row, Mapping):
                raise DecisionEvidenceError(f"missing {name.value} component forecast")
            return ComponentForecast(
                component=name,
                value_ms=Milliseconds(economics_raw[total_key]),
                mode=ComponentEstimateMode(str(row["mode"])),
                prior_sample_count=int(row.get("prior_sample_count", 0)),
                window_oldest_available_at=(
                    normalize_timestamp(str(row["window_oldest_available_at"]))
                    if row.get("window_oldest_available_at")
                    else None
                ),
                window_newest_available_at=(
                    normalize_timestamp(str(row["window_newest_available_at"]))
                    if row.get("window_newest_available_at")
                    else None
                ),
                percentile=int(row["percentile"]) if row.get("percentile") is not None else None,
                measured_available_at=(
                    normalize_timestamp(str(row["measured_available_at"])) if row.get("measured_available_at") else None
                ),
                excluded_current_measurement_reason=str(row.get("excluded_current_measurement_reason", "")),
                component_definition_version=str(row.get("component_definition_version", "")),
                unit=str(row.get("unit", "ms")),
            )

        forecast = LatencyForecast(
            data_age_ms=Milliseconds(economics_raw["data_age_ms"]),
            decision=component(LatencyComponent.DECISION, "forecast_decision_latency_ms"),
            risk_processing=component(LatencyComponent.RISK_PROCESSING, "forecast_risk_latency_ms"),
            submission=component(LatencyComponent.SUBMISSION, "forecast_submission_latency_ms"),
            acknowledgement=component(LatencyComponent.ACKNOWLEDGEMENT, "forecast_ack_latency_ms"),
            fill=component(LatencyComponent.FILL, "forecast_fill_latency_ms"),
            total_ms=Milliseconds(economics_raw["forecast_total_latency_ms"]),
            estimator_mode=EstimatorMode(str(economics_raw["latency_estimator_mode"])),
            estimator_version=str(economics_raw["estimator_version"]),
        )
        side = SignalSide(str(economics_raw["side"]))
        costs = ExecutionCostEstimate(
            fee_bps=BasisPoints(economics_raw["fee_bps"]),
            spread_bps=BasisPoints(economics_raw["spread_bps"]),
            slippage_bps=BasisPoints(economics_raw["slippage_bps"]),
            impact_bps=BasisPoints(economics_raw["impact_bps"]),
            total_bps=BasisPoints(economics_raw["estimated_cost_bps"]),
            liquidity_role=LiquidityRole(str(economics_raw["liquidity_role"])),
            side=side,
            adverse_price_adjustment_bps=BasisPoints(economics_raw["adverse_price_adjustment_bps"]),
            convention=str(economics_raw["cost_convention"]),
            reference_price_convention=str(economics_raw["reference_price_convention"]),
        )
        economics = DecisionEconomics(
            gross_edge_bps=BasisPoints(economics_raw["gross_edge_bps"]),
            latency_forecast=forecast,
            tau_ms=Milliseconds(economics_raw["tau_ms"]),
            predicted_decay_factor=float(economics_raw["predicted_decay_factor"]),
            edge_at_fill_bps=BasisPoints(economics_raw["edge_at_fill_bps"]),
            costs=costs,
            net_edge_bps=BasisPoints(economics_raw["net_edge_bps"]),
            required_buffer_bps=BasisPoints(economics_raw["required_buffer_bps"]),
            config_version=str(economics_raw["config_version"]),
            config_fingerprint=str(economics_raw["config_fingerprint"]),
            evaluation_version=str(economics_raw["evaluation_version"]),
        )
        return ApprovedOpportunity(
            decision_id=event.decision_id,
            run_id=event.run_id,
            signal_id=event.signal_id,
            symbol=str(raw["symbol"]),
            side=SignalSide(str(raw["side"])),
            decision_at=normalize_timestamp(str(raw["decision_at"])),
            economics=economics,
        )
