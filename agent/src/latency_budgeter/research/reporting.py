"""Deterministic machine and human reporting for Phase 8 Step 4."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.research.decision_rules import (
    ReleaseDecisionRecord,
    SupplementaryReleaseEvidence,
)
from src.latency_budgeter.research.experiment import MatchedExperimentResult
from src.latency_budgeter.research.policy import HoldoutDecisionPolicy

STEP4_REPORT_VERSION = "phase8-step4-research-report-v1"


@dataclass(frozen=True, slots=True)
class Step4ResearchReport:
    """Byte-reproducible report products from frozen inputs."""

    payload: Mapping[str, Any]
    markdown: str
    report_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self.payload)

    def json_bytes(self) -> bytes:
        return (canonical_json(self.payload) + "\n").encode("utf-8")

    def markdown_bytes(self) -> bytes:
        return self.markdown.encode("utf-8")

    def write(self, output_directory: str | Path, *, stem: str = "phase8_step4_report") -> tuple[Path, Path]:
        directory = Path(output_directory)
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{stem}.json"
        markdown_path = directory / f"{stem}.md"
        json_path.write_bytes(self.json_bytes())
        markdown_path.write_bytes(self.markdown_bytes())
        return json_path, markdown_path


class Step4ReportGenerator:
    """Generate every required output from immutable experiment evidence."""

    def generate(
        self,
        *,
        result: MatchedExperimentResult,
        policy: HoldoutDecisionPolicy,
        evidence: SupplementaryReleaseEvidence,
        decision: ReleaseDecisionRecord,
        report_generated_at: datetime,
    ) -> Step4ResearchReport:
        generated_at = normalize_timestamp(report_generated_at)
        if decision.experiment_fingerprint != result.experiment_fingerprint:
            raise ValueError("decision record does not belong to this matched experiment")
        if decision.holdout_policy_fingerprint != policy.fingerprint:
            raise ValueError("decision record does not use this frozen holdout policy")
        if generated_at < decision.decided_at:
            raise ValueError("report generation cannot precede the frozen decision")

        opportunity_table = self._arm_table(
            result,
            "opportunity_flow",
            (
                "raw_signal_count",
                "common_eligible_count",
                "budgeter_approved_count",
                "rejected_count",
                "deferred_count",
            ),
        )
        execution_table = self._arm_table(
            result,
            "execution",
            (
                "submitted_order_count",
                "executed_order_count",
                "no_fill_count",
                "partial_fill_count",
                "expiry_count",
                "retention_rate",
                "approval_rate",
                "fill_rate_given_approval",
            ),
        )
        economics_table = self._arm_table(
            result,
            "economics",
            (
                "net_expectancy_per_common_eligible_signal_bps",
                "net_expectancy_per_executed_order_bps",
                "realised_net_outcome_amount",
                "entry_turnover",
                "exposure",
                "maximum_drawdown",
                "concentration_hhi",
            ),
        )
        execution_quality_table = self._arm_table(
            result,
            "execution_quality",
            (
                "p90_slippage_bps",
                "mean_implementation_shortfall_bps",
                "mean_actual_execution_cost_bps",
                "total_actual_execution_cost_amount",
            ),
        )
        calibration_table = self._nested_arm_table(result, "calibration")
        diagnostics_table = self._arm_table(
            result,
            "diagnostics",
            (
                "rejected_signal_counterfactual_count",
                "approved_unfilled_counterfactual_count",
                "rejected_signal_mean_simulated_net_bps",
                "approved_unfilled_mean_simulated_net_bps",
                "reason_code_distribution",
                "lifecycle_integrity_failure_count",
            ),
        )
        matched_table = self._matched_table(result)
        holdout_table = [rule.to_dict() for rule in decision.rules]
        manifest = {
            "report_version": STEP4_REPORT_VERSION,
            "report_generated_at": utc_iso(generated_at),
            "ledger_snapshot_method": (
                "SHA-256 over sorted opportunity_key, per-decision immutable event-stream hash, "
                "and aggregate version"
            ),
            "baseline_ledger_snapshot_hash": result.baseline_ledger_snapshot_hash,
            "budgeter_ledger_snapshot_hash": result.budgeter_ledger_snapshot_hash,
            "experiment_fingerprint": result.experiment_fingerprint,
            "code_version": result.budgeter_manifest.code_version,
            "strategy_version": result.budgeter_manifest.strategy_version,
            "configuration_version": result.budgeter_manifest.config_version,
            "baseline_configuration_fingerprint": result.baseline_manifest.gate_config_fingerprint,
            "budgeter_configuration_fingerprint": result.budgeter_manifest.gate_config_fingerprint,
            "non_gate_configuration_fingerprint": result.budgeter_manifest.non_gate_config_fingerprint,
            "dataset_version": result.budgeter_manifest.dataset_version,
            "evaluation_version": result.budgeter_manifest.evaluation_version,
            "evaluation_policy_fingerprint": result.budgeter_manifest.evaluation_policy_fingerprint,
            "holdout_policy_version": policy.policy_version,
            "holdout_policy_fingerprint": policy.fingerprint,
            "random_seed": policy.random_seed,
            "cohort_definition": result.budgeter_manifest.cohort_definition_version,
            "query_parameters": thaw_json(result.budgeter_manifest.query_parameters),
            "supplementary_evidence": evidence.to_dict(),
            "decision_rule_version": decision.decision_rule_version,
        }
        payload_without_fingerprint: dict[str, Any] = {
            "report_version": STEP4_REPORT_VERSION,
            "decision_summary": {
                "decision": decision.decision.value,
                "rationale": decision.rationale,
                "directional_benefit": decision.directional_benefit,
                "matched_opportunity_count": result.matched_opportunity_count,
                "experiment_valid": result.validity.valid,
                "failed_rule_ids": list(decision.failed_rule_ids),
                "uncertain_rule_ids": list(decision.uncertain_rule_ids),
            },
            "opportunity_flow_table": opportunity_table,
            "execution_table": execution_table,
            "economics_table": economics_table,
            "execution_quality_table": execution_quality_table,
            "calibration_table": calibration_table,
            "diagnostics_table": diagnostics_table,
            "matched_baseline_vs_budgeter_table": matched_table,
            "matched_opportunity_outcomes": result.to_dict()["effect_observations"],
            "holdout_rule_table": holdout_table,
            "decision_record": decision.to_dict(),
            "methodology_metadata": {
                "primary_estimand": (
                    "budgeter minus baseline net outcome bps per identical common eligible opportunity"
                ),
                "weighting": "equal common opportunity",
                "unexecuted_realised_pnl_contribution": "zero",
                "partial_fill_handling": "actual executed quantity only",
                "counterfactuals": "diagnostic only; excluded from realised P&L and primary effect",
                "uncertainty": (
                    result.uncertainty.to_dict() if result.uncertainty is not None else None
                ),
                "nearby_settings": "pre-registered sensitivity evidence; never replaces primary result",
            },
            "reproducibility_manifest": manifest,
            "experiment_validity_audit": result.validity.to_dict(),
            "unmatched_baseline_activity": thaw_json(result.unmatched_baseline_activity),
            "data_quality_and_integrity_appendix": {
                "baseline": result.baseline_metrics.to_dict()["data_quality"],
                "budgeter": result.budgeter_metrics.to_dict()["data_quality"],
                "baseline_diagnostic_reason_codes": result.baseline_metrics.to_dict()["diagnostics"].get(
                    "reason_code_distribution", {}
                ),
                "budgeter_diagnostic_reason_codes": result.budgeter_metrics.to_dict()["diagnostics"].get(
                    "reason_code_distribution", {}
                ),
                "original_forecasts_rewritten": False,
                "diagnostics_in_realised_pnl": False,
                "matched_denominator_changed": False,
            },
        }
        report_fingerprint = hashlib.sha256(
            canonical_json(payload_without_fingerprint).encode("utf-8")
        ).hexdigest()
        payload = {
            **payload_without_fingerprint,
            "report_fingerprint": report_fingerprint,
        }
        markdown = self._markdown(payload)
        return Step4ResearchReport(payload, markdown, report_fingerprint)

    @staticmethod
    def _arm_table(
        result: MatchedExperimentResult,
        section: str,
        fields: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        baseline = getattr(result.baseline_metrics, section)
        budgeter = getattr(result.budgeter_metrics, section)
        return [
            {
                "metric": field,
                "baseline": thaw_json(baseline.get(field)),
                "budgeter": thaw_json(budgeter.get(field)),
            }
            for field in fields
        ]

    @staticmethod
    def _nested_arm_table(result: MatchedExperimentResult, section: str) -> list[dict[str, Any]]:
        baseline = getattr(result.baseline_metrics, section)
        budgeter = getattr(result.budgeter_metrics, section)
        keys = sorted((set(baseline) | set(budgeter)) - {"aggregation_grain", "missing_value_policy"})
        return [
            {
                "metric": key,
                "baseline": thaw_json(baseline.get(key)),
                "budgeter": thaw_json(budgeter.get(key)),
            }
            for key in keys
        ]

    @staticmethod
    def _matched_table(result: MatchedExperimentResult) -> list[dict[str, Any]]:
        baseline = result.baseline_metrics.economics.get("net_expectancy_per_common_eligible_signal_bps")
        budgeter = result.budgeter_metrics.economics.get("net_expectancy_per_common_eligible_signal_bps")
        effect = None
        if baseline is not None and budgeter is not None:
            from decimal import Decimal

            effect = format(Decimal(str(budgeter)) - Decimal(str(baseline)), "f")
        uncertainty = result.uncertainty
        return [
            {
                "estimand": "net_expectancy_per_common_eligible_signal_bps",
                "baseline": baseline,
                "budgeter": budgeter,
                "effect_bps": effect,
                "ci_low_bps": None if uncertainty is None else uncertainty.confidence_interval_low_bps,
                "ci_high_bps": None if uncertainty is None else uncertainty.confidence_interval_high_bps,
                "matched_opportunity_count": result.matched_opportunity_count,
                "valid_comparison": result.validity.valid,
            }
        ]

    @classmethod
    def _markdown(cls, payload: Mapping[str, Any]) -> str:
        summary = payload["decision_summary"]
        lines = [
            "# Phase 8 Step 4 Frozen Holdout Report",
            "",
            f"Decision: **{summary['decision']}**",
            "",
            str(summary["rationale"]),
            "",
            f"Report fingerprint: `{payload['report_fingerprint']}`",
            "",
            "## Opportunity flow",
            "",
            cls._markdown_table(payload["opportunity_flow_table"]),
            "",
            "## Execution",
            "",
            cls._markdown_table(payload["execution_table"]),
            "",
            "## Economics (realised only)",
            "",
            cls._markdown_table(payload["economics_table"]),
            "",
            "## Execution quality",
            "",
            cls._markdown_table(payload["execution_quality_table"]),
            "",
            "## Calibration",
            "",
            cls._markdown_table(payload["calibration_table"]),
            "",
            "## Diagnostics (simulated; excluded from realised P&L)",
            "",
            cls._markdown_table(payload["diagnostics_table"]),
            "",
            "## Matched baseline versus budgeter",
            "",
            cls._markdown_table(payload["matched_baseline_vs_budgeter_table"]),
            "",
            "## Frozen holdout rules",
            "",
            cls._markdown_table(payload["holdout_rule_table"]),
            "",
            "## Integrity",
            "",
            f"Experiment valid: `{payload['experiment_validity_audit']['valid']}`",
            "",
            "Unmatched baseline activity is reported separately and excluded from the primary effect. "
            "Counterfactual diagnostics never enter realised P&L.",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _markdown_table(rows: Any) -> str:
        if not isinstance(rows, (tuple, list)) or not rows:
            return "_No rows._"
        columns = tuple(rows[0].keys())
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join("---" for _ in columns) + " |"

        def cell(value: Any) -> str:
            if isinstance(value, (dict, list, tuple)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return str(value).replace("|", "\\|").replace("\n", " ")

        body = ["| " + " | ".join(cell(row.get(column)) for column in columns) + " |" for row in rows]
        return "\n".join((header, separator, *body))
