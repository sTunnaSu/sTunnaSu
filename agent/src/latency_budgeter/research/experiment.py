"""Frozen matched-arm construction and deterministic Step 4 research metrics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

import numpy as np

from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.projections.research_summary import ResearchDecisionSummary
from src.latency_budgeter.research.policy import HoldoutDecisionPolicy
from src.latency_budgeter.research.statistics import (
    ClusteredBlockBootstrapResult,
    MatchedEffectObservation,
    clustered_moving_block_bootstrap,
)

STEP4_EXPERIMENT_VERSION = "phase8-step4-matched-experiment-v1"
_BPS = Decimal("10000")


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _decimal_text(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


def _ratio(numerator: int | Decimal, denominator: int | Decimal) -> Decimal | None:
    denominator_decimal = Decimal(str(denominator))
    if denominator_decimal == 0:
        return None
    return Decimal(str(numerator)) / denominator_decimal


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    return sum(values, Decimal("0")) / Decimal(len(values)) if values else None


def _percentile(values: Sequence[Decimal], percentile: float) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(float(np.percentile(np.asarray([float(value) for value in values]), percentile))))


def non_gate_config_fingerprint(config: Mapping[str, Any]) -> str:
    """Derive the controlled configuration identity, excluding only treatment."""
    material = thaw_json(freeze_json(config))
    material.pop("enabled", None)
    return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    """Pre-run identity for one arm; only ``gate_enabled`` may differ."""

    arm_name: Literal["baseline", "budgeter"]
    gate_enabled: bool
    strategy_version: str
    universe: tuple[str, ...]
    holdout_start: datetime
    holdout_end: datetime
    initial_capital: Decimal
    sizing_rule_version: str
    participation_rule_version: str
    order_model_version: str
    cost_model_version: str
    evaluation_version: str
    evaluation_policy_fingerprint: str
    holdout_policy_fingerprint: str
    dataset_version: str
    code_version: str
    config_version: str
    gate_config_fingerprint: str
    non_gate_config_fingerprint: str
    cohort_definition_version: str
    query_parameters: Mapping[str, Any]
    experiment_version: str = STEP4_EXPERIMENT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "holdout_start", normalize_timestamp(self.holdout_start))
        object.__setattr__(self, "holdout_end", normalize_timestamp(self.holdout_end))
        object.__setattr__(self, "initial_capital", Decimal(str(self.initial_capital)))
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("manifest universe must not contain duplicates")
        object.__setattr__(self, "universe", tuple(sorted(self.universe)))
        object.__setattr__(self, "query_parameters", freeze_json(self.query_parameters))
        if self.experiment_version != STEP4_EXPERIMENT_VERSION:
            raise ValueError(f"unsupported experiment version: {self.experiment_version!r}")
        if self.arm_name == "baseline" and self.gate_enabled:
            raise ValueError("baseline arm must keep the Phase 8 gate disabled")
        if self.arm_name == "budgeter" and not self.gate_enabled:
            raise ValueError("budgeter arm must enable the Phase 8 gate")
        if self.holdout_start >= self.holdout_end:
            raise ValueError("holdout_start must precede holdout_end")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        required = (
            self.strategy_version,
            *self.universe,
            self.sizing_rule_version,
            self.participation_rule_version,
            self.order_model_version,
            self.cost_model_version,
            self.evaluation_version,
            self.evaluation_policy_fingerprint,
            self.holdout_policy_fingerprint,
            self.dataset_version,
            self.code_version,
            self.config_version,
            self.gate_config_fingerprint,
            self.non_gate_config_fingerprint,
            self.cohort_definition_version,
        )
        if not self.universe or any(not str(value).strip() for value in required):
            raise ValueError("all manifest identities and a non-empty universe are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_version": self.experiment_version,
            "arm_name": self.arm_name,
            "gate_enabled": self.gate_enabled,
            "strategy_version": self.strategy_version,
            "universe": list(self.universe),
            "holdout_start": utc_iso(self.holdout_start),
            "holdout_end": utc_iso(self.holdout_end),
            "initial_capital": _decimal_text(self.initial_capital),
            "sizing_rule_version": self.sizing_rule_version,
            "participation_rule_version": self.participation_rule_version,
            "order_model_version": self.order_model_version,
            "cost_model_version": self.cost_model_version,
            "evaluation_version": self.evaluation_version,
            "evaluation_policy_fingerprint": self.evaluation_policy_fingerprint,
            "holdout_policy_fingerprint": self.holdout_policy_fingerprint,
            "dataset_version": self.dataset_version,
            "code_version": self.code_version,
            "config_version": self.config_version,
            "gate_config_fingerprint": self.gate_config_fingerprint,
            "non_gate_config_fingerprint": self.non_gate_config_fingerprint,
            "cohort_definition_version": self.cohort_definition_version,
            "query_parameters": thaw_json(self.query_parameters),
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @property
    def controlled_signature(self) -> str:
        material = self.to_dict()
        material.pop("arm_name")
        material.pop("gate_enabled")
        material.pop("gate_config_fingerprint")
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentArm:
    """One immutable manifest plus opportunity-grain summaries."""

    manifest: ExperimentManifest
    summaries: tuple[ResearchDecisionSummary, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "summaries",
            tuple(sorted(self.summaries, key=lambda item: (item.decision_at, item.opportunity_key))),
        )

    @property
    def common_summaries(self) -> tuple[ResearchDecisionSummary, ...]:
        return tuple(item for item in self.summaries if item.common_eligible)

    @property
    def unmatched_summaries(self) -> tuple[ResearchDecisionSummary, ...]:
        return tuple(item for item in self.summaries if not item.common_eligible)


@dataclass(frozen=True, slots=True)
class ExperimentValidity:
    """Release-blocking matched-cohort and protocol audit."""

    valid: bool
    failures: tuple[str, ...]
    baseline_only_keys: tuple[str, ...]
    budgeter_only_keys: tuple[str, ...]
    duplicate_baseline_keys: tuple[str, ...]
    duplicate_budgeter_keys: tuple[str, ...]
    unmatched_baseline_keys: tuple[str, ...]
    primary_improvement_claim_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "failures": list(self.failures),
            "baseline_only_keys": list(self.baseline_only_keys),
            "budgeter_only_keys": list(self.budgeter_only_keys),
            "duplicate_baseline_keys": list(self.duplicate_baseline_keys),
            "duplicate_budgeter_keys": list(self.duplicate_budgeter_keys),
            "unmatched_baseline_keys": list(self.unmatched_baseline_keys),
            "unmatched_baseline_count": len(self.unmatched_baseline_keys),
            "primary_improvement_claim_allowed": self.primary_improvement_claim_allowed,
            "repair_required": not self.valid,
        }


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    """Semantically separated deterministic metrics for one arm."""

    arm_name: str
    opportunity_flow: Mapping[str, Any]
    execution: Mapping[str, Any]
    economics: Mapping[str, Any]
    execution_quality: Mapping[str, Any]
    calibration: Mapping[str, Any]
    diagnostics: Mapping[str, Any]
    data_quality: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "opportunity_flow",
            "execution",
            "economics",
            "execution_quality",
            "calibration",
            "diagnostics",
            "data_quality",
        ):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "arm_name": self.arm_name,
            "opportunity_flow": thaw_json(self.opportunity_flow),
            "execution": thaw_json(self.execution),
            "economics": thaw_json(self.economics),
            "execution_quality": thaw_json(self.execution_quality),
            "calibration": thaw_json(self.calibration),
            "diagnostics": thaw_json(self.diagnostics),
            "data_quality": thaw_json(self.data_quality),
        }


@dataclass(frozen=True, slots=True)
class MatchedExperimentResult:
    """Complete frozen primary comparison and separately labelled diagnostics."""

    baseline_manifest: ExperimentManifest
    budgeter_manifest: ExperimentManifest
    validity: ExperimentValidity
    baseline_metrics: ArmMetrics
    budgeter_metrics: ArmMetrics
    effect_observations: tuple[MatchedEffectObservation, ...]
    uncertainty: ClusteredBlockBootstrapResult | None
    unmatched_baseline_activity: Mapping[str, Any]
    matched_opportunity_count: int
    baseline_ledger_snapshot_hash: str
    budgeter_ledger_snapshot_hash: str
    experiment_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "unmatched_baseline_activity", freeze_json(self.unmatched_baseline_activity))

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_version": STEP4_EXPERIMENT_VERSION,
            "experiment_fingerprint": self.experiment_fingerprint,
            "baseline_manifest": self.baseline_manifest.to_dict(),
            "budgeter_manifest": self.budgeter_manifest.to_dict(),
            "validity": self.validity.to_dict(),
            "matched_opportunity_count": self.matched_opportunity_count,
            "baseline_ledger_snapshot_hash": self.baseline_ledger_snapshot_hash,
            "budgeter_ledger_snapshot_hash": self.budgeter_ledger_snapshot_hash,
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "budgeter_metrics": self.budgeter_metrics.to_dict(),
            "effect_observations": [
                {
                    "opportunity_key": item.opportunity_key,
                    "timestamp": utc_iso(item.timestamp),
                    "symbol": item.symbol,
                    "baseline_net_bps": _decimal_text(item.baseline_net_bps),
                    "budgeter_net_bps": _decimal_text(item.budgeter_net_bps),
                    "effect_bps": _decimal_text(item.effect_bps),
                    "regime": item.regime,
                }
                for item in self.effect_observations
            ],
            "uncertainty": self.uncertainty.to_dict() if self.uncertainty is not None else None,
            "unmatched_baseline_activity": thaw_json(self.unmatched_baseline_activity),
            "primary_realised_pnl_excludes_diagnostics": True,
        }


class MatchedExperimentBuilder:
    """Validate matched arms, calculate metrics, and block invalid claims."""

    def build(
        self,
        *,
        baseline: ExperimentArm,
        budgeter: ExperimentArm,
        holdout_policy: HoldoutDecisionPolicy,
    ) -> MatchedExperimentResult:
        failures: list[str] = []
        if baseline.manifest.arm_name != "baseline" or budgeter.manifest.arm_name != "budgeter":
            failures.append("arm_role_mismatch")
        if baseline.manifest.holdout_policy_fingerprint != holdout_policy.fingerprint:
            failures.append("baseline_holdout_policy_fingerprint_mismatch")
        if budgeter.manifest.holdout_policy_fingerprint != holdout_policy.fingerprint:
            failures.append("budgeter_holdout_policy_fingerprint_mismatch")
        if holdout_policy.holdout_start != baseline.manifest.holdout_start or holdout_policy.holdout_end != baseline.manifest.holdout_end:
            failures.append("baseline_holdout_window_mismatch")
        if holdout_policy.holdout_start != budgeter.manifest.holdout_start or holdout_policy.holdout_end != budgeter.manifest.holdout_end:
            failures.append("budgeter_holdout_window_mismatch")
        if baseline.manifest.controlled_signature != budgeter.manifest.controlled_signature:
            failures.append("non_gate_experiment_configuration_differs")

        baseline_duplicates = self._duplicates(baseline.common_summaries)
        budgeter_duplicates = self._duplicates(budgeter.common_summaries)
        if baseline_duplicates:
            failures.append("duplicate_baseline_common_opportunities")
        if budgeter_duplicates:
            failures.append("duplicate_budgeter_common_opportunities")
        baseline_by_key = self._by_key(baseline.common_summaries)
        budgeter_by_key = self._by_key(budgeter.common_summaries)
        baseline_only = tuple(sorted(set(baseline_by_key) - set(budgeter_by_key)))
        budgeter_only = tuple(sorted(set(budgeter_by_key) - set(baseline_by_key)))
        if baseline_only or budgeter_only:
            failures.append("common_opportunity_sets_differ")

        shared_keys = tuple(sorted(set(baseline_by_key) & set(budgeter_by_key)))
        for key in shared_keys:
            self._validate_pair(key, baseline_by_key[key], budgeter_by_key[key], failures)
        for arm in (baseline, budgeter):
            for summary in arm.common_summaries:
                if not (arm.manifest.holdout_start <= summary.decision_at < arm.manifest.holdout_end):
                    failures.append(f"{arm.manifest.arm_name}_opportunity_outside_frozen_holdout:{summary.opportunity_key}")
                if summary.strategy_version != arm.manifest.strategy_version:
                    failures.append(f"{arm.manifest.arm_name}_strategy_version_mismatch:{summary.opportunity_key}")
                if summary.symbol not in arm.manifest.universe:
                    failures.append(f"{arm.manifest.arm_name}_symbol_outside_universe:{summary.opportunity_key}")
                if summary.integrity_failures:
                    failures.append(
                        f"lifecycle_integrity_failure:{arm.manifest.arm_name}:{summary.opportunity_key}"
                    )
                self._validate_summary_manifest(summary, arm.manifest, failures)

        validity = ExperimentValidity(
            valid=not failures,
            failures=tuple(dict.fromkeys(failures)),
            baseline_only_keys=baseline_only,
            budgeter_only_keys=budgeter_only,
            duplicate_baseline_keys=baseline_duplicates,
            duplicate_budgeter_keys=budgeter_duplicates,
            unmatched_baseline_keys=tuple(item.opportunity_key for item in baseline.unmatched_summaries),
            primary_improvement_claim_allowed=not failures,
        )
        baseline_metrics = self._metrics(baseline)
        budgeter_metrics = self._metrics(budgeter)
        observations = tuple(
            MatchedEffectObservation(
                opportunity_key=key,
                timestamp=baseline_by_key[key].decision_at,
                symbol=baseline_by_key[key].symbol,
                baseline_net_bps=self._primary_net_bps(baseline_by_key[key]),
                budgeter_net_bps=self._primary_net_bps(budgeter_by_key[key]),
                regime=baseline_by_key[key].regime,
            )
            for key in shared_keys
            if self._pair_has_complete_outcomes(baseline_by_key[key], budgeter_by_key[key])
        )
        uncertainty = (
            clustered_moving_block_bootstrap(
                observations,
                iterations=holdout_policy.bootstrap_iterations,
                block_length=holdout_policy.bootstrap_block_length,
                confidence_level=float(holdout_policy.confidence_level),
                seed=holdout_policy.random_seed,
                multiple_comparison_count=holdout_policy.multiple_comparison_count,
                minimum_sample_size=holdout_policy.minimum_common_opportunities,
                minimum_clusters=holdout_policy.minimum_symbol_clusters,
            )
            if validity.valid
            else None
        )
        unmatched = self._unmatched_baseline_activity(baseline.unmatched_summaries)
        baseline_snapshot_hash = self._arm_snapshot_hash(baseline.summaries)
        budgeter_snapshot_hash = self._arm_snapshot_hash(budgeter.summaries)
        fingerprint_material = {
            "baseline_manifest": baseline.manifest.to_dict(),
            "budgeter_manifest": budgeter.manifest.to_dict(),
            "validity": validity.to_dict(),
            "baseline_ledger_snapshot_hash": baseline_snapshot_hash,
            "budgeter_ledger_snapshot_hash": budgeter_snapshot_hash,
            "holdout_policy_fingerprint": holdout_policy.fingerprint,
        }
        fingerprint = hashlib.sha256(canonical_json(fingerprint_material).encode("utf-8")).hexdigest()
        return MatchedExperimentResult(
            baseline_manifest=baseline.manifest,
            budgeter_manifest=budgeter.manifest,
            validity=validity,
            baseline_metrics=baseline_metrics,
            budgeter_metrics=budgeter_metrics,
            effect_observations=observations,
            uncertainty=uncertainty,
            unmatched_baseline_activity=unmatched,
            matched_opportunity_count=len(shared_keys),
            baseline_ledger_snapshot_hash=baseline_snapshot_hash,
            budgeter_ledger_snapshot_hash=budgeter_snapshot_hash,
            experiment_fingerprint=fingerprint,
        )

    @staticmethod
    def _arm_snapshot_hash(summaries: Sequence[ResearchDecisionSummary]) -> str:
        material = [
            {
                "opportunity_key": item.opportunity_key,
                "ledger_snapshot_hash": item.ledger_snapshot_hash,
                "aggregate_version": item.aggregate_version,
            }
            for item in sorted(summaries, key=lambda value: value.opportunity_key)
        ]
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()

    @staticmethod
    def _duplicates(summaries: Sequence[ResearchDecisionSummary]) -> tuple[str, ...]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in summaries:
            if item.opportunity_key in seen:
                duplicates.add(item.opportunity_key)
            seen.add(item.opportunity_key)
        return tuple(sorted(duplicates))

    @staticmethod
    def _by_key(summaries: Sequence[ResearchDecisionSummary]) -> dict[str, ResearchDecisionSummary]:
        result: dict[str, ResearchDecisionSummary] = {}
        for item in summaries:
            result.setdefault(item.opportunity_key, item)
        return result

    @staticmethod
    def _pair_has_complete_outcomes(
        baseline: ResearchDecisionSummary,
        budgeter: ResearchDecisionSummary,
    ) -> bool:
        return baseline.outcome_evaluated and budgeter.outcome_evaluated

    @classmethod
    def _validate_pair(
        cls,
        key: str,
        baseline: ResearchDecisionSummary,
        budgeter: ResearchDecisionSummary,
        failures: list[str],
    ) -> None:
        if (
            baseline.decision_at != budgeter.decision_at
            or baseline.symbol != budgeter.symbol
            or baseline.side != budgeter.side
            or baseline.strategy_version != budgeter.strategy_version
        ):
            failures.append(f"opportunity_identity_mismatch:{key}")
        if baseline.outcome_evaluated != budgeter.outcome_evaluated:
            failures.append(f"missing_arm_outcome_asymmetry:{key}")
            return
        if not baseline.outcome_evaluated:
            failures.append(f"unevaluated_common_opportunity:{key}")
            return
        baseline_outcome = baseline.outcome_evaluation or {}
        budgeter_outcome = budgeter.outcome_evaluation or {}
        baseline_horizon = baseline_outcome.get("evaluation_horizon")
        budgeter_horizon = budgeter_outcome.get("evaluation_horizon")
        if not isinstance(baseline_horizon, Mapping) or not isinstance(budgeter_horizon, Mapping):
            failures.append(f"evaluation_horizon_missing:{key}")
        elif canonical_json(baseline_horizon) != canonical_json(budgeter_horizon):
            failures.append(f"evaluation_horizon_differs:{key}")
        for field, label in (
            ("evaluation_methodology_version", "evaluation_methodology"),
            ("frozen_counterfactual_methodology", "counterfactual_methodology"),
        ):
            if canonical_json(baseline_outcome.get(field)) != canonical_json(budgeter_outcome.get(field)):
                failures.append(f"{label}_differs:{key}")
        baseline_cost = baseline_outcome.get("cost_convention")
        budgeter_cost = budgeter_outcome.get("cost_convention")
        frozen_cost_fields = (
            "simulated_values_use",
            "frozen_component_convention",
            "frozen_estimated_cost_bps",
            "diagnostic_costs_never_enter_realised_pnl",
        )
        if not isinstance(baseline_cost, Mapping) or not isinstance(budgeter_cost, Mapping):
            failures.append(f"cost_convention_missing:{key}")
        elif any(baseline_cost.get(field) != budgeter_cost.get(field) for field in frozen_cost_fields):
            failures.append(f"cost_convention_differs:{key}")
        baseline_provenance = baseline_outcome.get("data_provenance")
        budgeter_provenance = budgeter_outcome.get("data_provenance")
        if not isinstance(baseline_provenance, Mapping) or not isinstance(budgeter_provenance, Mapping):
            failures.append(f"outcome_provenance_missing:{key}")
        elif canonical_json(baseline_provenance) != canonical_json(budgeter_provenance):
            failures.append(f"outcome_provenance_differs:{key}")
        baseline_references = baseline_outcome.get("reference_prices")
        budgeter_references = budgeter_outcome.get("reference_prices")
        frozen_reference_fields = (
            "decision_reference_price",
            "outcome_reference_price",
            "reference_convention",
        )
        if not isinstance(baseline_references, Mapping) or not isinstance(budgeter_references, Mapping):
            failures.append(f"outcome_reference_prices_missing:{key}")
        elif any(
            baseline_references.get(field) != budgeter_references.get(field)
            for field in frozen_reference_fields
        ):
            failures.append(f"outcome_reference_evidence_differs:{key}")
        baseline_config = baseline.original_decision.get("phase8_config")
        budgeter_config = budgeter.original_decision.get("phase8_config")
        if isinstance(baseline_config, Mapping) and isinstance(budgeter_config, Mapping):
            if non_gate_config_fingerprint(baseline_config) != non_gate_config_fingerprint(
                budgeter_config
            ):
                failures.append(f"non_gate_root_configuration_differs:{key}")

    @staticmethod
    def _validate_summary_manifest(
        summary: ResearchDecisionSummary,
        manifest: ExperimentManifest,
        failures: list[str],
    ) -> None:
        prefix = f"{manifest.arm_name}:{summary.opportunity_key}"
        root_config = summary.original_decision.get("phase8_config")
        if not isinstance(root_config, Mapping):
            failures.append(f"frozen_root_config_missing:{prefix}")
        elif bool(root_config.get("enabled")) != manifest.gate_enabled:
            failures.append(f"gate_enabled_manifest_mismatch:{prefix}")
        elif non_gate_config_fingerprint(root_config) != manifest.non_gate_config_fingerprint:
            failures.append(f"non_gate_config_fingerprint_mismatch:{prefix}")
        root_fingerprint = summary.original_decision.get("config_fingerprint")
        if root_fingerprint != manifest.gate_config_fingerprint:
            failures.append(f"gate_config_fingerprint_mismatch:{prefix}")
        if summary.original_decision.get("config_version") != manifest.config_version:
            failures.append(f"config_version_mismatch:{prefix}")
        if summary.cohort_flags.get("classification_version") != manifest.cohort_definition_version:
            failures.append(f"cohort_definition_version_mismatch:{prefix}")
        outcome = summary.outcome_evaluation
        if outcome is None:
            return
        if outcome.get("config_fingerprint") != manifest.gate_config_fingerprint:
            failures.append(f"outcome_config_fingerprint_mismatch:{prefix}")
        if outcome.get("outcome_policy_fingerprint") != manifest.evaluation_policy_fingerprint:
            failures.append(f"evaluation_policy_fingerprint_mismatch:{prefix}")
        if outcome.get("evaluation_methodology_version") != manifest.evaluation_version:
            failures.append(f"evaluation_version_mismatch:{prefix}")
        provenance = outcome.get("data_provenance")
        if not isinstance(provenance, Mapping) or provenance.get("dataset_version") != manifest.dataset_version:
            failures.append(f"dataset_manifest_mismatch:{prefix}")

    @classmethod
    def _metrics(cls, arm: ExperimentArm) -> ArmMetrics:
        all_summaries = arm.summaries
        common = arm.common_summaries
        approved = tuple(item for item in common if item.decision == "ALLOW")
        rejected = tuple(item for item in common if item.decision == "REJECT")
        deferred = tuple(item for item in common if item.decision == "DEFER")
        submitted = tuple(item for item in common if item.submitted)
        executed = tuple(item for item in common if item.executed)
        no_fill = tuple(item for item in submitted if not item.executed)
        partial = tuple(
            item
            for item in executed
            if Decimal(str(item.execution_state.get("unfilled_quantity", "0") or "0")) > 0
        )
        expired = tuple(
            item
            for item in common
            if "EXPIRED" in str(item.execution_state.get("terminal_state", ""))
        )
        evaluated_common = tuple(item for item in common if item.outcome_evaluated)
        missing_outcomes = tuple(item.opportunity_key for item in common if not item.outcome_evaluated)
        realised_bps = [value for item in executed if (value := item.realised_net_outcome_bps) is not None]
        common_bps = [cls._primary_net_bps(item) for item in common if item.outcome_evaluated]
        realised_amounts = [
            value for item in executed if (value := item.realised_net_outcome_amount) is not None
        ]
        notionals = [value for item in executed if (value := item.executed_notional) is not None]
        total_notional = sum(notionals, Decimal("0"))
        symbol_notionals: dict[str, Decimal] = {}
        for item in executed:
            if item.executed_notional is not None:
                symbol_notionals[item.symbol] = symbol_notionals.get(item.symbol, Decimal("0")) + item.executed_notional
        concentration = (
            sum(
                ((value / total_notional) ** 2 for value in symbol_notionals.values()),
                Decimal("0"),
            )
            if total_notional > 0
            else None
        )
        max_drawdown = cls._maximum_drawdown(common, arm.manifest.initial_capital)
        slippage_bps = cls._execution_bps(executed, "actual_slippage")
        shortfall_bps = cls._execution_bps(executed, "implementation_shortfall")
        cost_bps = cls._execution_bps(executed, "realised_execution_cost")
        actual_cost_amounts = cls._execution_amounts(executed, "realised_execution_cost")
        calibration = cls._calibration(executed)
        diagnostic_rows = [row for item in common for row in item.diagnostic_counterfactuals]
        rejected_diagnostics = [
            row for row in diagnostic_rows if row.get("counterfactual_type") == "rejected_signal_counterfactual"
        ]
        unfilled_diagnostics = [
            row for row in diagnostic_rows if row.get("counterfactual_type") == "approved_unfilled_counterfactual"
        ]
        reasons: dict[str, int] = {}
        for item in common:
            reason = str(item.original_decision.get("reason_code", "UNKNOWN"))
            reasons[reason] = reasons.get(reason, 0) + 1
        integrity_failure_count = sum(len(item.integrity_failures) for item in common)
        primary_retention = _ratio(len(executed), len(common))
        approval_retention = _ratio(len(approved), len(common))
        fill_given_approval = _ratio(len(executed), len(approved))
        opportunity_flow = {
            "counting_grain": "unique opportunity_key (one raw strategy decision)",
            "deduplication_key": "stable opportunity_key independent of arm and retries",
            "raw_signal_count": len(all_summaries),
            "common_eligible_count": len(common),
            "arm_approved_count": len(approved),
            "budgeter_approved_count": len(approved) if arm.manifest.arm_name == "budgeter" else None,
            "rejected_count": len(rejected),
            "deferred_count": len(deferred),
            "unmatched_activity_count": len(arm.unmatched_summaries),
            "four_counts": {
                "raw_signals": len(all_summaries),
                "common_phase8_eligible_signals": len(common),
                "budgeter_approved_signals": (
                    len(approved) if arm.manifest.arm_name == "budgeter" else None
                ),
                "executed_orders_with_actual_fill": len(executed),
            },
        }
        execution = {
            "counting_grain": "one unique common-cohort opportunity/order; fill events never increment counts",
            "submitted_order_count": len(submitted),
            "executed_order_count": len(executed),
            "no_fill_count": len(no_fill),
            "partial_fill_count": len(partial),
            "expiry_count": len(expired),
            "retention_rate": _decimal_text(primary_retention),
            "retention_definition": "executed common opportunities / all common eligible opportunities",
            "approval_rate": _decimal_text(approval_retention),
            "fill_rate_given_approval": _decimal_text(fill_given_approval),
        }
        economics = {
            "realised_pnl_source": "actual fills only; diagnostic counterfactuals excluded",
            "unexecuted_common_opportunity_contribution_bps": "0",
            "net_expectancy_per_common_eligible_signal_bps": _decimal_text(_mean(common_bps)),
            "net_expectancy_per_executed_order_bps": _decimal_text(_mean(realised_bps)),
            "realised_net_outcome_amount": _decimal_text(sum(realised_amounts, Decimal("0"))),
            "entry_turnover": _decimal_text(total_notional / arm.manifest.initial_capital),
            "turnover_definition": "actual entry fill notional / frozen initial capital; exits unavailable",
            "exposure": None,
            "exposure_status": "UNAVAILABLE_WITHOUT_POSITION_TIME_SERIES",
            "maximum_drawdown": _decimal_text(max_drawdown),
            "maximum_drawdown_definition": "peak-to-trough opportunity-realisation equity path from actual net amounts",
            "concentration_hhi": _decimal_text(concentration),
            "concentration_definition": "HHI of actual entry fill notional by symbol",
        }
        execution_quality = {
            "actual_execution_only": True,
            "p90_slippage_bps": _decimal_text(_percentile(slippage_bps, 90)),
            "mean_implementation_shortfall_bps": _decimal_text(_mean(shortfall_bps)),
            "mean_actual_execution_cost_bps": _decimal_text(_mean(cost_bps)),
            "total_actual_execution_cost_amount": _decimal_text(sum(actual_cost_amounts, Decimal("0"))),
            "missing_slippage_count": len(executed) - len(slippage_bps),
            "missing_shortfall_count": len(executed) - len(shortfall_bps),
            "missing_execution_cost_count": len(executed) - len(cost_bps),
        }
        diagnostics = {
            "excluded_from_realised_pnl": True,
            "rejected_signal_counterfactual_count": len(rejected_diagnostics),
            "approved_unfilled_counterfactual_count": len(unfilled_diagnostics),
            "rejected_signal_mean_simulated_net_bps": _decimal_text(
                cls._diagnostic_mean(rejected_diagnostics)
            ),
            "approved_unfilled_mean_simulated_net_bps": _decimal_text(
                cls._diagnostic_mean(unfilled_diagnostics)
            ),
            "reason_code_distribution": dict(sorted(reasons.items())),
            "lifecycle_integrity_failure_count": integrity_failure_count,
        }
        data_quality = {
            "common_outcome_evaluated_count": len(evaluated_common),
            "missing_common_outcome_keys": list(missing_outcomes),
            "executed_outcome_missing_net_bps_count": len(executed) - len(realised_bps),
            "executed_outcome_missing_net_amount_count": len(executed) - len(realised_amounts),
            "integrity_failure_count": integrity_failure_count,
        }
        return ArmMetrics(
            arm_name=arm.manifest.arm_name,
            opportunity_flow=opportunity_flow,
            execution=execution,
            economics=economics,
            execution_quality=execution_quality,
            calibration=calibration,
            diagnostics=diagnostics,
            data_quality=data_quality,
        )

    @staticmethod
    def _primary_net_bps(summary: ResearchDecisionSummary) -> Decimal:
        if summary.executed and summary.realised_net_outcome_bps is not None:
            return summary.realised_net_outcome_bps
        return Decimal("0")

    @staticmethod
    def _maximum_drawdown(
        summaries: Sequence[ResearchDecisionSummary],
        initial_capital: Decimal,
    ) -> Decimal | None:
        equity = initial_capital
        peak = initial_capital
        maximum = Decimal("0")
        has_amount = False
        for item in sorted(summaries, key=lambda value: (value.outcome_reference_at or value.decision_at, value.opportunity_key)):
            amount = item.realised_net_outcome_amount if item.executed else Decimal("0")
            if amount is None:
                continue
            has_amount = has_amount or item.executed
            equity += amount
            peak = max(peak, equity)
            if peak > 0:
                maximum = max(maximum, (peak - equity) / peak)
        return maximum if has_amount else None

    @staticmethod
    def _execution_amounts(
        summaries: Sequence[ResearchDecisionSummary], key: str
    ) -> list[Decimal]:
        values: list[Decimal] = []
        for item in summaries:
            if item.execution_evaluation is None:
                continue
            value = _decimal(item.execution_evaluation.get(key))
            if value is not None:
                values.append(value)
        return values

    @classmethod
    def _execution_bps(
        cls, summaries: Sequence[ResearchDecisionSummary], key: str
    ) -> list[Decimal]:
        values: list[Decimal] = []
        for item in summaries:
            amount = None if item.execution_evaluation is None else _decimal(item.execution_evaluation.get(key))
            notional = item.executed_notional
            if amount is not None and notional is not None and notional > 0:
                values.append(amount / notional * _BPS)
        return values

    @staticmethod
    def _diagnostic_mean(rows: Sequence[Mapping[str, Any]]) -> Decimal | None:
        values: list[Decimal] = []
        for row in rows:
            diagnostic = row.get("simulated_diagnostic")
            if isinstance(diagnostic, Mapping):
                value = _decimal(diagnostic.get("simulated_net_outcome_bps"))
                if value is not None:
                    values.append(value)
        return _mean(values)

    @staticmethod
    def _calibration(summaries: Sequence[ResearchDecisionSummary]) -> dict[str, Any]:
        output: dict[str, Any] = {
            "aggregation_grain": "equal-weight executed common opportunity",
            "missing_value_policy": "missing actuals remain missing; diagnostics never substitute",
        }
        for key in ("latency", "cost", "edge_at_fill", "net_edge"):
            signed: list[Decimal] = []
            absolute: list[Decimal] = []
            for item in summaries:
                outcome = item.outcome_evaluation
                comparison = outcome.get("forecast_versus_reality") if outcome else None
                row = comparison.get(key) if isinstance(comparison, Mapping) else None
                if not isinstance(row, Mapping):
                    continue
                signed_value = _decimal(row.get("signed_error"))
                absolute_value = _decimal(row.get("absolute_error"))
                if signed_value is not None and absolute_value is not None:
                    signed.append(signed_value)
                    absolute.append(absolute_value)
            output[key] = {
                "mean_signed_error": _decimal_text(_mean(signed)),
                "mean_absolute_error": _decimal_text(_mean(absolute)),
                "available_count": len(signed),
                "missing_count": len(summaries) - len(signed),
            }
        return output

    @staticmethod
    def _unmatched_baseline_activity(
        summaries: Sequence[ResearchDecisionSummary],
    ) -> dict[str, Any]:
        return {
            "label": "baseline activity outside common cohort; excluded from primary causal comparison",
            "opportunity_count": len(summaries),
            "opportunity_keys": [item.opportunity_key for item in summaries],
            "executed_count": sum(item.executed for item in summaries),
            "included_in_primary_effect": False,
        }
