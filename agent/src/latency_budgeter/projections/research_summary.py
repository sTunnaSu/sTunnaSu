"""Deterministic Step 4 decision-summary reconstruction."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from src.latency_budgeter.domain.errors import ProjectionError
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.outcomes import CounterfactualType, OutcomeType
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.projections.order_lifecycle import OrderLifecycleProjector


@dataclass(frozen=True, slots=True)
class ResearchDecisionSummary:
    """One opportunity-grain read model rebuilt only from immutable events."""

    decision_id: str
    run_id: str
    signal_id: str
    opportunity_key: str
    decision_at: datetime
    symbol: str
    side: str
    strategy_version: str
    provenance: Mapping[str, Any]
    cohort_flags: Mapping[str, Any]
    original_decision: Mapping[str, Any]
    frozen_forecast_economics: Mapping[str, Any] | None
    execution_state: Mapping[str, Any]
    component_validity: Mapping[str, Any]
    execution_evaluation: Mapping[str, Any] | None
    strategy_outcome: Mapping[str, Any] | None
    diagnostic_counterfactuals: tuple[Mapping[str, Any], ...]
    integrity_failures: tuple[Mapping[str, Any], ...]
    matched_cohort_member: bool
    event_ids: tuple[str, ...]
    aggregate_version: int
    ledger_snapshot_hash: str

    @property
    def decision(self) -> str:
        return str(self.original_decision.get("decision", ""))

    @property
    def common_eligible(self) -> bool:
        return bool(self.matched_cohort_member)

    @property
    def submitted(self) -> bool:
        return bool(self.execution_state.get("submitted", False))

    @property
    def executed(self) -> bool:
        return Decimal(str(self.execution_state.get("executed_quantity", "0"))) > 0

    @property
    def realised_net_outcome_bps(self) -> Decimal | None:
        if self.strategy_outcome is None:
            return None
        realised = self.strategy_outcome.get("realised_strategy_outcome")
        if not isinstance(realised, Mapping) or realised.get("net_outcome_bps") is None:
            return None
        return Decimal(str(realised["net_outcome_bps"]))

    @property
    def realised_net_outcome_amount(self) -> Decimal | None:
        if self.strategy_outcome is None:
            return None
        realised = self.strategy_outcome.get("realised_strategy_outcome")
        if not isinstance(realised, Mapping) or realised.get("net_outcome_amount") is None:
            return None
        return Decimal(str(realised["net_outcome_amount"]))

    @property
    def outcome_evaluation(self) -> Mapping[str, Any] | None:
        if self.strategy_outcome is not None:
            return self.strategy_outcome
        return self.diagnostic_counterfactuals[0] if self.diagnostic_counterfactuals else None

    @property
    def outcome_evaluated(self) -> bool:
        return self.outcome_evaluation is not None

    @property
    def regime(self) -> str:
        raw_signal = self.original_decision.get("raw_strategy_signal")
        if not isinstance(raw_signal, Mapping):
            return "unclassified"
        metadata = raw_signal.get("metadata")
        if not isinstance(metadata, Mapping):
            return "unclassified"
        return str(metadata.get("regime", "unclassified"))

    @property
    def executed_notional(self) -> Decimal | None:
        if self.strategy_outcome is None:
            return None
        references = self.strategy_outcome.get("reference_prices")
        quantity_basis = self.strategy_outcome.get("quantity_basis")
        if not isinstance(references, Mapping) or not isinstance(quantity_basis, Mapping):
            return None
        price = references.get("actual_weighted_fill_price")
        quantity = quantity_basis.get("executed_quantity")
        if price is None or quantity is None:
            return None
        return Decimal(str(price)) * Decimal(str(quantity))

    @property
    def outcome_reference_at(self) -> datetime | None:
        evaluation = self.outcome_evaluation
        if evaluation is None:
            return None
        horizon = evaluation.get("evaluation_horizon")
        if not isinstance(horizon, Mapping) or not horizon.get("reference_observed_at"):
            return None
        return normalize_timestamp(str(horizon["reference_observed_at"]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": {
                "decision_id": self.decision_id,
                "run_id": self.run_id,
                "signal_id": self.signal_id,
                "opportunity_key": self.opportunity_key,
                "decision_at": utc_iso(self.decision_at),
                "symbol": self.symbol,
                "side": self.side,
                "strategy_version": self.strategy_version,
            },
            "provenance": thaw_json(self.provenance),
            "cohort_flags": thaw_json(self.cohort_flags),
            "original_decision": thaw_json(self.original_decision),
            "frozen_forecast_economics": (
                thaw_json(self.frozen_forecast_economics)
                if self.frozen_forecast_economics is not None
                else None
            ),
            "execution_state": thaw_json(self.execution_state),
            "component_validity": thaw_json(self.component_validity),
            "execution_evaluation": (
                thaw_json(self.execution_evaluation) if self.execution_evaluation is not None else None
            ),
            "strategy_outcome": (
                thaw_json(self.strategy_outcome) if self.strategy_outcome is not None else None
            ),
            "diagnostic_counterfactuals": [thaw_json(value) for value in self.diagnostic_counterfactuals],
            "integrity_failures": [thaw_json(value) for value in self.integrity_failures],
            "matched_cohort_member": self.matched_cohort_member,
            "event_ids": list(self.event_ids),
            "aggregate_version": self.aggregate_version,
            "ledger_snapshot_hash": self.ledger_snapshot_hash,
        }


class ResearchDecisionSummaryProjector:
    """Rebuild the complete Step 4 read model from an empty store."""

    def replay(self, events: Sequence[LedgerEvent]) -> ResearchDecisionSummary:
        if not events:
            raise ProjectionError("cannot build a research summary from an empty stream")
        ordered = tuple(events)
        lifecycle = OrderLifecycleProjector().replay(ordered)
        root = lifecycle.root
        raw_signal = root.payload.get("raw_strategy_signal")
        observation = root.payload.get("observation")
        classification = root.payload.get("shared_cohort_classification")
        if not isinstance(raw_signal, Mapping) or not isinstance(observation, Mapping):
            raise ProjectionError("Step 4 requires frozen raw-signal and observation evidence")
        if not isinstance(classification, Mapping):
            raise ProjectionError("Step 4 requires shared cohort classification")
        outcome_events = [event for event in ordered if event.event_type is EventType.OUTCOME_EVALUATED]
        if len(outcome_events) > 1:
            raise ProjectionError("a decision may contain at most one frozen Step 4 outcome")
        outcome = outcome_events[0] if outcome_events else None
        if outcome is not None:
            if outcome.payload.get("config_fingerprint") != root.payload.get("config_fingerprint"):
                raise ProjectionError("outcome configuration differs from the frozen decision")
            if outcome.payload.get("original_forecast_rewritten") is not False:
                raise ProjectionError("outcome does not affirm immutable forecast evidence")

        opportunity_key = self._opportunity_key(raw_signal, observation)
        failures = tuple(
            freeze_json(event.payload)
            for event in ordered
            if event.event_type is EventType.LIFECYCLE_INTEGRITY_FAILURE
        )
        execution_events = [
            event for event in ordered if event.event_type is EventType.EXECUTION_EVALUATED
        ]
        latest_execution = execution_events[-1] if execution_events else None
        strategy_outcome: Mapping[str, Any] | None = None
        diagnostics: tuple[Mapping[str, Any], ...] = ()
        if outcome is not None:
            outcome_type = OutcomeType(str(outcome.payload["outcome_type"]))
            counterfactual = CounterfactualType(str(outcome.payload["counterfactual_type"]))
            if outcome_type is not OutcomeType.SIMULATED_DIAGNOSTIC:
                strategy_outcome = freeze_json(outcome.payload)
            if counterfactual is not CounterfactualType.NONE:
                diagnostics = (freeze_json(outcome.payload),)

        component_validity = self._component_validity(ordered, failures)
        execution_state = freeze_json(
            {
                "submitted": lifecycle.submission is not None,
                "submission_count": sum(
                    event.event_type is EventType.ORDER_SUBMITTED for event in ordered
                ),
                "fill_event_count": len(lifecycle.fills),
                "executed_quantity": format(lifecycle.executed_quantity, "f"),
                "unfilled_quantity": (
                    format(lifecycle.unfilled_quantity, "f")
                    if lifecycle.unfilled_quantity is not None
                    else None
                ),
                "terminal_state": (
                    lifecycle.terminal_state.value if lifecycle.terminal_state is not None else None
                ),
                "terminal_at": utc_iso(lifecycle.terminal_at) if lifecycle.terminal_at else None,
                "execution_outcome": (
                    latest_execution.payload.get("execution_outcome")
                    if latest_execution is not None
                    else None
                ),
            }
        )
        snapshot_hash = hashlib.sha256(
            canonical_json([event.to_dict() for event in ordered]).encode("utf-8")
        ).hexdigest()
        return ResearchDecisionSummary(
            decision_id=root.decision_id,
            run_id=root.run_id,
            signal_id=root.signal_id,
            opportunity_key=opportunity_key,
            decision_at=normalize_timestamp(str(root.payload["decision_at"])),
            symbol=str(observation.get("symbol", "")),
            side=str(observation.get("side", "")),
            strategy_version=str(raw_signal.get("strategy_version", "")),
            provenance=freeze_json(
                {
                    "observation": observation,
                    "event_source_metadata": root.source_metadata,
                    "config_version": root.payload.get("config_version"),
                    "config_fingerprint": root.payload.get("config_fingerprint"),
                }
            ),
            cohort_flags=freeze_json(classification),
            original_decision=freeze_json(root.payload),
            frozen_forecast_economics=(
                freeze_json(root.payload["decision_economics"])
                if isinstance(root.payload.get("decision_economics"), Mapping)
                else None
            ),
            execution_state=execution_state,
            component_validity=component_validity,
            execution_evaluation=(
                freeze_json(latest_execution.payload) if latest_execution is not None else None
            ),
            strategy_outcome=strategy_outcome,
            diagnostic_counterfactuals=diagnostics,
            integrity_failures=failures,
            matched_cohort_member=bool(classification.get("common_phase8_eligible", False)),
            event_ids=tuple(event.event_id for event in ordered),
            aggregate_version=ordered[-1].aggregate_version,
            ledger_snapshot_hash=snapshot_hash,
        )

    @staticmethod
    def _opportunity_key(
        raw_signal: Mapping[str, Any],
        observation: Mapping[str, Any],
    ) -> str:
        metadata = raw_signal.get("metadata")
        cross_arm_id = (
            metadata.get("cross_arm_opportunity_id")
            if isinstance(metadata, Mapping)
            else None
        )
        material = {
            "observation_fingerprint": raw_signal.get("observation_fingerprint"),
            "generated_at": raw_signal.get("generated_at"),
            "strategy_version": raw_signal.get("strategy_version"),
            "symbol": observation.get("symbol"),
            "side": observation.get("side"),
            "cross_arm_opportunity_id": cross_arm_id,
        }
        required = {key: value for key, value in material.items() if key != "cross_arm_opportunity_id"}
        if any(value in (None, "") for value in required.values()):
            raise ProjectionError("cannot derive a stable cross-arm opportunity key")
        return f"opp_{hashlib.sha256(canonical_json(material).encode('utf-8')).hexdigest()}"

    @staticmethod
    def _component_validity(
        events: Sequence[LedgerEvent],
        failures: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        status: dict[str, dict[str, Any]] = {}
        for event in events:
            validation = event.payload.get("component_validation")
            if not isinstance(validation, Mapping):
                continue
            component = str(validation.get("component_type", ""))
            if component:
                status[component] = dict(validation)
        for failure in failures:
            affected = failure.get("affected_latency_components", ())
            if not isinstance(affected, (tuple, list)):
                continue
            for component in affected:
                name = str(component)
                prior = status.setdefault(name, {})
                prior["status"] = "INVALIDATED"
                prior["invalidation_event_detected_at"] = failure.get("detected_at")
        return freeze_json(status)
