"""Frozen GO, REVISE, and REJECT rules for the Step 4 holdout."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.research.experiment import MatchedExperimentResult
from src.latency_budgeter.research.policy import HoldoutDecisionPolicy

STEP4_DECISION_RULE_VERSION = "phase8-step4-release-rules-v1"


class ReleaseDecision(str, Enum):
    GO = "GO"
    REVISE = "REVISE"
    REJECT = "REJECT"


class EvidenceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNCERTAIN = "UNCERTAIN"
    NOT_REQUIRED = "NOT_REQUIRED"


@dataclass(frozen=True, slots=True)
class SupplementaryReleaseEvidence:
    """Predeclared non-primary evidence; never replaces the frozen result."""

    primary_setting_id: str
    nearby_setting_effects_bps: Mapping[str, Decimal]
    provenance_evidence_credible: bool | None
    cost_evidence_credible: bool | None
    calibration_reliable: bool | None
    regime_coverage_complete: bool | None
    evidence_version: str = "phase8-step4-supplementary-evidence-v1"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "nearby_setting_effects_bps",
            freeze_json(
                {
                    str(key): format(Decimal(str(value)), "f")
                    for key, value in sorted(self.nearby_setting_effects_bps.items())
                }
            ),
        )
        if not self.primary_setting_id or not self.evidence_version:
            raise ValueError("primary setting and evidence version are required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_version": self.evidence_version,
            "primary_setting_id": self.primary_setting_id,
            "nearby_setting_effects_bps": thaw_json(self.nearby_setting_effects_bps),
            "provenance_evidence_credible": self.provenance_evidence_credible,
            "cost_evidence_credible": self.cost_evidence_credible,
            "calibration_reliable": self.calibration_reliable,
            "regime_coverage_complete": self.regime_coverage_complete,
            "nearby_settings_are_diagnostic_only": True,
        }


@dataclass(frozen=True, slots=True)
class RuleEvidence:
    rule_id: str
    status: EvidenceStatus
    observed: Any
    threshold: Any
    reason: str
    required_for_go: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status.value,
            "observed": self.observed,
            "threshold": self.threshold,
            "reason": self.reason,
            "required_for_go": self.required_for_go,
        }


@dataclass(frozen=True, slots=True)
class ReleaseDecisionRecord:
    decision: ReleaseDecision
    decided_at: datetime
    rules: tuple[RuleEvidence, ...]
    failed_rule_ids: tuple[str, ...]
    uncertain_rule_ids: tuple[str, ...]
    experiment_fingerprint: str
    holdout_policy_fingerprint: str
    supplementary_evidence_fingerprint: str
    directional_benefit: bool
    rationale: str
    decision_rule_version: str = STEP4_DECISION_RULE_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "decided_at", normalize_timestamp(self.decided_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_rule_version": self.decision_rule_version,
            "decision": self.decision.value,
            "decided_at": utc_iso(self.decided_at),
            "experiment_fingerprint": self.experiment_fingerprint,
            "holdout_policy_fingerprint": self.holdout_policy_fingerprint,
            "supplementary_evidence_fingerprint": self.supplementary_evidence_fingerprint,
            "directional_benefit": self.directional_benefit,
            "rules": [rule.to_dict() for rule in self.rules],
            "failed_rule_ids": list(self.failed_rule_ids),
            "uncertain_rule_ids": list(self.uncertain_rule_ids),
            "rationale": self.rationale,
            "nearby_analysis_replaced_primary_result": False,
            "post_holdout_retuning_permitted": False,
        }


class FrozenHoldoutDecisionEngine:
    """Apply pre-registered rules with deterministic conflict resolution."""

    def decide(
        self,
        *,
        result: MatchedExperimentResult,
        policy: HoldoutDecisionPolicy,
        evidence: SupplementaryReleaseEvidence,
        decided_at: datetime,
    ) -> ReleaseDecisionRecord:
        decided_at = normalize_timestamp(decided_at)
        if decided_at < policy.holdout_end:
            raise ValueError("release decision cannot inspect an unfinished frozen holdout")
        if evidence.primary_setting_id != policy.primary_setting_id:
            raise ValueError("supplementary evidence points to a different primary setting")
        declared_nearby = set(policy.nearby_setting_ids)
        supplied_nearby = set(evidence.nearby_setting_effects_bps)
        if supplied_nearby - declared_nearby:
            raise ValueError("post-hoc nearby settings are forbidden")

        rules: list[RuleEvidence] = []
        rules.append(
            self._rule(
                "experiment_valid",
                EvidenceStatus.PASS if result.validity.valid else EvidenceStatus.FAIL,
                result.validity.valid,
                True,
                "matched arms must pass every frozen integrity check",
            )
        )
        baseline_expectancy = self._metric_decimal(
            result.baseline_metrics.economics, "net_expectancy_per_common_eligible_signal_bps"
        )
        budgeter_expectancy = self._metric_decimal(
            result.budgeter_metrics.economics, "net_expectancy_per_common_eligible_signal_bps"
        )
        effect = (
            budgeter_expectancy - baseline_expectancy
            if baseline_expectancy is not None and budgeter_expectancy is not None
            else None
        )
        minimum_effect = policy.minimum_effect_bps
        directional_benefit = effect is not None and effect > minimum_effect
        rules.append(
            self._rule(
                "primary_oos_net_expectancy_improved",
                (
                    EvidenceStatus.PASS
                    if directional_benefit
                    else EvidenceStatus.FAIL if effect is not None else EvidenceStatus.UNCERTAIN
                ),
                self._text(effect),
                f"> {self._text(minimum_effect)} bps",
                "budgeter minus baseline net expectancy per identical common opportunity",
            )
        )

        uncertainty = result.uncertainty
        interval_low = None if uncertainty is None else uncertainty.confidence_interval_low_bps
        interval_high = None if uncertainty is None else uncertainty.confidence_interval_high_bps
        if interval_low is None or interval_high is None:
            uncertainty_status = EvidenceStatus.UNCERTAIN
        elif Decimal(str(interval_low)) > minimum_effect:
            uncertainty_status = EvidenceStatus.PASS
        elif Decimal(str(interval_high)) <= minimum_effect:
            uncertainty_status = EvidenceStatus.FAIL
        else:
            uncertainty_status = EvidenceStatus.UNCERTAIN
        rules.append(
            self._rule(
                "dependence_aware_uncertainty",
                uncertainty_status,
                {"ci_low_bps": interval_low, "ci_high_bps": interval_high},
                f"familywise CI lower bound > {self._text(minimum_effect)} bps",
                "paired symbol-clustered moving-block bootstrap",
            )
        )
        sample_size = 0 if uncertainty is None else uncertainty.sample_size
        cluster_count = 0 if uncertainty is None else uncertainty.symbol_cluster_count
        rules.append(
            self._rule(
                "minimum_common_opportunities",
                EvidenceStatus.PASS if sample_size >= policy.minimum_common_opportunities else EvidenceStatus.UNCERTAIN,
                sample_size,
                policy.minimum_common_opportunities,
                "small samples cannot earn release confidence",
            )
        )
        rules.append(
            self._rule(
                "minimum_symbol_clusters",
                EvidenceStatus.PASS if cluster_count >= policy.minimum_symbol_clusters else EvidenceStatus.UNCERTAIN,
                cluster_count,
                policy.minimum_symbol_clusters,
                "repeated-symbol dependence requires multiple independent symbol clusters",
            )
        )

        retention = self._metric_decimal(result.budgeter_metrics.execution, "retention_rate")
        rules.append(
            self._minimum_rule(
                "retention",
                retention,
                policy.minimum_retention_rate,
                "executed common opportunities / all common eligible opportunities",
            )
        )
        drawdown = self._metric_decimal(result.budgeter_metrics.economics, "maximum_drawdown")
        rules.append(
            self._maximum_rule(
                "maximum_drawdown",
                drawdown,
                policy.maximum_drawdown,
                "actual opportunity-realisation equity path",
            )
        )
        concentration = self._metric_decimal(result.budgeter_metrics.economics, "concentration_hhi")
        rules.append(
            self._maximum_rule(
                "maximum_concentration",
                concentration,
                policy.maximum_concentration,
                "actual entry-fill notional HHI by symbol",
            )
        )

        baseline_slippage = self._metric_decimal(result.baseline_metrics.execution_quality, "p90_slippage_bps")
        budgeter_slippage = self._metric_decimal(result.budgeter_metrics.execution_quality, "p90_slippage_bps")
        slippage_worsening = (
            budgeter_slippage - baseline_slippage
            if baseline_slippage is not None and budgeter_slippage is not None
            else None
        )
        rules.append(
            self._maximum_rule(
                "execution_quality_non_worsening",
                slippage_worsening,
                policy.maximum_p90_slippage_worsening_bps,
                "budgeter minus baseline p90 actual slippage bps",
            )
        )

        common_count = int(result.budgeter_metrics.opportunity_flow.get("common_eligible_count", 0))
        rejected_count = int(result.budgeter_metrics.opportunity_flow.get("rejected_count", 0))
        deferred_count = int(result.budgeter_metrics.opportunity_flow.get("deferred_count", 0))
        rejection_rate = (
            Decimal(rejected_count + deferred_count) / Decimal(common_count) if common_count else None
        )
        rules.append(
            self._maximum_rule(
                "maximum_gate_rejection_rate",
                rejection_rate,
                policy.maximum_rejection_rate,
                "(rejected + deferred) / common eligible; guards selection-by-abstention",
            )
        )

        if not policy.require_nearby_agreement:
            nearby_status = EvidenceStatus.NOT_REQUIRED
            nearby_reason = "nearby agreement not required by the frozen policy"
        elif supplied_nearby != declared_nearby:
            nearby_status = EvidenceStatus.UNCERTAIN
            nearby_reason = "one or more pre-registered nearby settings are missing"
        elif all(Decimal(str(value)) > minimum_effect for value in evidence.nearby_setting_effects_bps.values()):
            nearby_status = EvidenceStatus.PASS
            nearby_reason = "all pre-registered nearby settings agree directionally"
        else:
            nearby_status = EvidenceStatus.FAIL
            nearby_reason = "benefit disappears under at least one pre-registered nearby setting"
        rules.append(
            self._rule(
                "nearby_setting_agreement",
                nearby_status,
                thaw_json(evidence.nearby_setting_effects_bps),
                {key: f"> {self._text(minimum_effect)} bps" for key in sorted(declared_nearby)},
                nearby_reason,
                required=policy.require_nearby_agreement,
            )
        )
        rules.append(
            self._boolean_rule(
                "credible_provenance_evidence",
                evidence.provenance_evidence_credible,
                policy.require_provenance_evidence,
            )
        )
        rules.append(
            self._boolean_rule(
                "credible_cost_evidence",
                evidence.cost_evidence_credible,
                policy.require_cost_evidence,
            )
        )
        rules.append(
            self._boolean_rule(
                "calibration_reliability",
                evidence.calibration_reliable,
                policy.calibration_requirement != "not_required",
            )
        )
        rules.append(
            self._boolean_rule(
                "regime_coverage",
                evidence.regime_coverage_complete,
                policy.require_regime_coverage,
            )
        )

        missing_outcomes = result.budgeter_metrics.data_quality.get("missing_common_outcome_keys", ())
        missing_executed = int(
            result.budgeter_metrics.data_quality.get("executed_outcome_missing_net_bps_count", 0)
        )
        data_complete = not missing_outcomes and missing_executed == 0
        rules.append(
            self._rule(
                "primary_outcome_completeness",
                EvidenceStatus.PASS if data_complete else EvidenceStatus.FAIL,
                {"missing_common_outcomes": list(missing_outcomes), "missing_executed_net": missing_executed},
                {"missing_common_outcomes": 0, "missing_executed_net": 0},
                "missing outcomes cannot be silently omitted from the frozen denominator",
            )
        )

        required_rules = tuple(rule for rule in rules if rule.required_for_go)
        failed = tuple(rule.rule_id for rule in required_rules if rule.status is EvidenceStatus.FAIL)
        uncertain = tuple(rule.rule_id for rule in required_rules if rule.status is EvidenceStatus.UNCERTAIN)
        if not failed and not uncertain:
            decision = ReleaseDecision.GO
            rationale = "all pre-registered primary and constraint evidence passed"
        else:
            hard_reject_rules = {
                "experiment_valid",
                "primary_oos_net_expectancy_improved",
                "maximum_drawdown",
                "maximum_concentration",
                "execution_quality_non_worsening",
                "maximum_gate_rejection_rate",
                "nearby_setting_agreement",
                "credible_provenance_evidence",
                "credible_cost_evidence",
                "calibration_reliability",
                "regime_coverage",
                "primary_outcome_completeness",
            }
            if not directional_benefit or hard_reject_rules.intersection(failed):
                decision = ReleaseDecision.REJECT
                rationale = "no robust admissible matched-cohort release case remains under frozen rules"
            else:
                decision = ReleaseDecision.REVISE
                rationale = "directional benefit exists, but finite pre-registered evidence is incomplete or uncertain"

        evidence_dict = evidence.to_dict()
        evidence_fingerprint = hashlib.sha256(canonical_json(evidence_dict).encode("utf-8")).hexdigest()
        return ReleaseDecisionRecord(
            decision=decision,
            decided_at=decided_at,
            rules=tuple(rules),
            failed_rule_ids=failed,
            uncertain_rule_ids=uncertain,
            experiment_fingerprint=result.experiment_fingerprint,
            holdout_policy_fingerprint=policy.fingerprint,
            supplementary_evidence_fingerprint=evidence_fingerprint,
            directional_benefit=directional_benefit,
            rationale=rationale,
        )

    @staticmethod
    def _metric_decimal(mapping: Mapping[str, Any], key: str) -> Decimal | None:
        value = mapping.get(key)
        return None if value is None else Decimal(str(value))

    @staticmethod
    def _text(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    @staticmethod
    def _rule(
        rule_id: str,
        status: EvidenceStatus,
        observed: Any,
        threshold: Any,
        reason: str,
        *,
        required: bool = True,
    ) -> RuleEvidence:
        return RuleEvidence(rule_id, status, observed, threshold, reason, required)

    def _minimum_rule(
        self,
        rule_id: str,
        observed: Decimal | None,
        threshold: Decimal,
        reason: str,
    ) -> RuleEvidence:
        status = (
            EvidenceStatus.UNCERTAIN
            if observed is None
            else EvidenceStatus.PASS if observed >= threshold else EvidenceStatus.FAIL
        )
        return self._rule(rule_id, status, self._text(observed), f">= {self._text(threshold)}", reason)

    def _maximum_rule(
        self,
        rule_id: str,
        observed: Decimal | None,
        threshold: Decimal,
        reason: str,
    ) -> RuleEvidence:
        status = (
            EvidenceStatus.UNCERTAIN
            if observed is None
            else EvidenceStatus.PASS if observed <= threshold else EvidenceStatus.FAIL
        )
        return self._rule(rule_id, status, self._text(observed), f"<= {self._text(threshold)}", reason)

    def _boolean_rule(self, rule_id: str, observed: bool | None, required: bool) -> RuleEvidence:
        if not required:
            status = EvidenceStatus.NOT_REQUIRED
        elif observed is None:
            status = EvidenceStatus.UNCERTAIN
        else:
            status = EvidenceStatus.PASS if observed else EvidenceStatus.FAIL
        return self._rule(
            rule_id,
            status,
            observed,
            True if required else "not required",
            "explicit frozen evidence flag; missing stays uncertain",
            required=required,
        )
