"""Matched experiment, frozen release, and reporting tests for Step 4."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.latency_budgeter.projections.research_summary import ResearchDecisionSummary
from src.latency_budgeter.research.decision_rules import (
    FrozenHoldoutDecisionEngine,
    ReleaseDecision,
    SupplementaryReleaseEvidence,
)
from src.latency_budgeter.research.experiment import (
    ExperimentArm,
    ExperimentManifest,
    MatchedExperimentBuilder,
    non_gate_config_fingerprint,
)
from src.latency_budgeter.research.policy import HoldoutDecisionPolicy
from src.latency_budgeter.research.reporting import Step4ReportGenerator
from src.latency_budgeter.research.statistics import (
    MatchedEffectObservation,
    clustered_moving_block_bootstrap,
)

BASE = datetime(2025, 1, 1, tzinfo=timezone.utc)
END = BASE + timedelta(days=10)


def holdout_policy(
    *,
    minimum_common: int = 2,
    minimum_clusters: int = 2,
    minimum_retention: str = "0.5",
    maximum_concentration: str = "0.6",
) -> HoldoutDecisionPolicy:
    return HoldoutDecisionPolicy(
        preregistered_at=BASE - timedelta(days=1),
        holdout_start=BASE,
        holdout_end=END,
        primary_setting_id="primary-v1",
        nearby_setting_ids=("nearby-v1",),
        minimum_effect_bps=Decimal("0"),
        minimum_retention_rate=Decimal(minimum_retention),
        maximum_drawdown=Decimal("0.20"),
        maximum_concentration=Decimal(maximum_concentration),
        maximum_p90_slippage_worsening_bps=Decimal("0"),
        maximum_rejection_rate=Decimal("0.75"),
        minimum_common_opportunities=minimum_common,
        minimum_symbol_clusters=minimum_clusters,
        bootstrap_iterations=100,
        bootstrap_block_length=2,
        random_seed=123,
        require_nearby_agreement=True,
        require_regime_coverage=True,
    )


def outcome_payload(
    *,
    arm: str,
    decision_at: datetime,
    executed: bool,
    net_bps: str | None,
    net_amount: str | None,
    diagnostic_type: str | None = None,
    diagnostic_bps: str = "-3",
    horizon_value: int = 1,
) -> dict:
    counterfactual_type = diagnostic_type or "NONE"
    outcome_type = "REALISED_EXECUTED" if executed else (
        "SIMULATED_DIAGNOSTIC" if diagnostic_type == "rejected_signal_counterfactual" else "NO_REALISED_EXECUTION"
    )
    return {
        "outcome_type": outcome_type,
        "counterfactual_type": counterfactual_type,
        "evaluation_horizon": {
            "value": horizon_value,
            "unit": "bars",
            "horizon_version": "horizon-v1",
            "decision_anchor_at": decision_at.isoformat(),
            "reference_observed_at": (decision_at + timedelta(days=1)).isoformat(),
            "bars_elapsed": horizon_value,
        },
        "evaluation_methodology_version": "outcome-v1",
        "outcome_policy_fingerprint": "evaluation-policy-fp",
        "frozen_counterfactual_methodology": {
            "method": "fixed_horizon_markout",
            "methodology_version": "shadow-v1",
            "reference_price": "next_open",
        },
        "cost_convention": {
            "realised_values_use": "actual" if executed else "not applicable",
            "simulated_values_use": "frozen decision cost",
            "frozen_component_convention": "one_way_components",
            "frozen_estimated_cost_bps": 2,
            "diagnostic_costs_never_enter_realised_pnl": True,
        },
        "data_provenance": {
            "dataset_version": "dataset-v1",
            "query_fingerprint": "query",
            "provider": "test",
        },
        "reference_prices": {
            "decision_reference_price": "100",
            "actual_weighted_fill_price": "100" if executed else None,
            "outcome_reference_price": "101",
            "reference_convention": "next_open",
        },
        "quantity_basis": {
            "executed_quantity": "1" if executed else "0",
            "unfilled_quantity": "0" if executed else "1",
            "partial_fill": False,
        },
        "realised_strategy_outcome": {
            "net_outcome_bps": net_bps,
            "net_outcome_amount": net_amount,
            "gross_outcome_bps": net_bps,
        },
        "simulated_diagnostic": {
            "diagnostic_status": "SIMULATED" if diagnostic_type else "not_applicable",
            "simulated_net_outcome_bps": diagnostic_bps if diagnostic_type else None,
            "not_actual_execution": True,
            "not_actual_fill": True,
            "not_realised_trade": True,
        },
        "forecast_versus_reality": {
            "latency": {"signed_error": "1", "absolute_error": "1"} if executed else {},
            "cost": {"signed_error": "0.5", "absolute_error": "0.5"} if executed else {},
            "edge_at_fill": {"signed_error": "2", "absolute_error": "2"} if executed else {},
            "net_edge": {"signed_error": "1", "absolute_error": "1"} if executed else {},
        },
        "original_forecast_rewritten": False,
        "config_fingerprint": f"{arm}-config-fp",
    }


def make_summary(
    key: str,
    *,
    arm: str,
    index: int,
    symbol: str = "BTC/USD",
    decision: str = "ALLOW",
    common: bool = True,
    submitted: bool = True,
    executed: bool = True,
    net_bps: str = "5",
    net_amount: str = "5",
    slippage_bps: str = "1",
    shortfall_bps: str = "1",
    cost_bps: str = "2",
    unfilled_quantity: str = "0",
    terminal_state: str = "FULLY_FILLED",
    diagnostic_type: str | None = None,
    outcome_evaluated: bool = True,
    integrity_failures: int = 0,
    fill_event_count: int = 1,
    horizon_value: int = 1,
    regime: str = "normal",
) -> ResearchDecisionSummary:
    decision_at = BASE + timedelta(hours=index)
    actual_notional = Decimal("100") if executed else Decimal("0")

    def amount_from_bps(value: str) -> str:
        return format(Decimal(value) * actual_notional / Decimal("10000"), "f")

    outcome = (
        outcome_payload(
            arm=arm,
            decision_at=decision_at,
            executed=executed,
            net_bps=net_bps if executed else None,
            net_amount=net_amount if executed else None,
            diagnostic_type=diagnostic_type,
            horizon_value=horizon_value,
        )
        if outcome_evaluated
        else None
    )
    strategy_outcome = outcome if outcome is not None and outcome["outcome_type"] != "SIMULATED_DIAGNOSTIC" else None
    diagnostic_rows = (outcome,) if outcome is not None and diagnostic_type is not None else ()
    execution_evaluation = (
        {
            "execution_outcome": "FULLY_FILLED" if unfilled_quantity == "0" else "PARTIALLY_FILLED_EXPIRED",
            "actual_slippage": amount_from_bps(slippage_bps),
            "implementation_shortfall": amount_from_bps(shortfall_bps),
            "realised_execution_cost": amount_from_bps(cost_bps),
            "lifecycle_audit": {"valid": integrity_failures == 0, "issues": []},
        }
        if submitted
        else None
    )
    original = {
        "decision": decision,
        "reason_code": "ALLOW_REASON" if decision == "ALLOW" else f"{decision}_REASON",
        "decision_at": decision_at.isoformat(),
        "config_version": "phase8-step1-v1",
        "config_fingerprint": f"{arm}-config-fp",
        "phase8_config": {"enabled": arm == "budgeter"},
        "raw_strategy_signal": {
            "strategy_version": "strategy-v1",
            "metadata": {"regime": regime},
        },
    }
    return ResearchDecisionSummary(
        decision_id=f"dec_{arm}_{key}",
        run_id=f"run_{arm}",
        signal_id=f"sig_{key}",
        opportunity_key=key,
        decision_at=decision_at,
        symbol=symbol,
        side="buy",
        strategy_version="strategy-v1",
        provenance={
            "config_version": "phase8-step1-v1",
            "config_fingerprint": f"{arm}-config-fp",
        },
        cohort_flags={
            "common_phase8_eligible": common,
            "classification_version": "phase8-cohort-v1",
        },
        original_decision=original,
        frozen_forecast_economics={"net_edge_bps": "4"},
        execution_state={
            "submitted": submitted,
            "submission_count": 1 if submitted else 0,
            "fill_event_count": fill_event_count if executed else 0,
            "executed_quantity": "1" if executed else "0",
            "unfilled_quantity": unfilled_quantity,
            "terminal_state": terminal_state if submitted else None,
            "execution_outcome": (
                "FULLY_FILLED" if executed and unfilled_quantity == "0" else "NO_FILL" if not executed else "PARTIAL"
            ),
        },
        component_validity={},
        execution_evaluation=execution_evaluation,
        strategy_outcome=strategy_outcome,
        diagnostic_counterfactuals=diagnostic_rows,
        integrity_failures=tuple({"failure": position} for position in range(integrity_failures)),
        matched_cohort_member=common,
        event_ids=(f"evt_{arm}_{key}",),
        aggregate_version=fill_event_count + 3,
        ledger_snapshot_hash=f"{arm}-{key}-snapshot",
    )


def manifest(arm: str, policy: HoldoutDecisionPolicy) -> ExperimentManifest:
    return ExperimentManifest(
        arm_name=arm,
        gate_enabled=arm == "budgeter",
        strategy_version="strategy-v1",
        universe=("BTC/USD", "ETH/USD"),
        holdout_start=BASE,
        holdout_end=END,
        initial_capital=Decimal("10000"),
        sizing_rule_version="sizing-v1",
        participation_rule_version="participation-v1",
        order_model_version="order-v1",
        cost_model_version="cost-v1",
        evaluation_version="outcome-v1",
        evaluation_policy_fingerprint="evaluation-policy-fp",
        holdout_policy_fingerprint=policy.fingerprint,
        dataset_version="dataset-v1",
        code_version="commit-abc",
        config_version="phase8-step1-v1",
        gate_config_fingerprint=f"{arm}-config-fp",
        non_gate_config_fingerprint=non_gate_config_fingerprint(
            {"enabled": arm == "budgeter"}
        ),
        cohort_definition_version="phase8-cohort-v1",
        query_parameters={"from": BASE.isoformat(), "to": END.isoformat()},
    )


def build_result(
    baseline_summaries: tuple[ResearchDecisionSummary, ...],
    budgeter_summaries: tuple[ResearchDecisionSummary, ...],
    policy: HoldoutDecisionPolicy,
    *,
    baseline_manifest: ExperimentManifest | None = None,
    budgeter_manifest: ExperimentManifest | None = None,
):
    return MatchedExperimentBuilder().build(
        baseline=ExperimentArm(baseline_manifest or manifest("baseline", policy), baseline_summaries),
        budgeter=ExperimentArm(budgeter_manifest or manifest("budgeter", policy), budgeter_summaries),
        holdout_policy=policy,
    )


def good_evidence(**overrides) -> SupplementaryReleaseEvidence:
    values = {
        "primary_setting_id": "primary-v1",
        "nearby_setting_effects_bps": {"nearby-v1": Decimal("3")},
        "provenance_evidence_credible": True,
        "cost_evidence_credible": True,
        "calibration_reliable": True,
        "regime_coverage_complete": True,
    }
    values.update(overrides)
    return SupplementaryReleaseEvidence(**values)


def two_arm_summaries(
    *,
    baseline_bps: tuple[str, ...] = ("1", "1"),
    budgeter_bps: tuple[str, ...] = ("5", "5"),
) -> tuple[tuple[ResearchDecisionSummary, ...], tuple[ResearchDecisionSummary, ...]]:
    symbols = ("BTC/USD", "ETH/USD")
    baseline = tuple(
        make_summary(f"opp-{index}", arm="baseline", index=index, symbol=symbols[index % 2], net_bps=value, net_amount="5")
        for index, value in enumerate(baseline_bps)
    )
    budgeter = tuple(
        make_summary(f"opp-{index}", arm="budgeter", index=index, symbol=symbols[index % 2], net_bps=value, net_amount="5")
        for index, value in enumerate(budgeter_bps)
    )
    return baseline, budgeter


def test_four_counts_retention_and_expectancy_use_unique_common_opportunities() -> None:
    policy = holdout_policy(minimum_common=2, minimum_clusters=1)
    baseline = tuple(
        make_summary(f"opp-{index}", arm="baseline", index=index, fill_event_count=3)
        for index in range(4)
    )
    budgeter = (
        make_summary("opp-0", arm="budgeter", index=0, net_bps="10", fill_event_count=4),
        make_summary("opp-1", arm="budgeter", index=1, net_bps="-2", fill_event_count=2),
        make_summary(
            "opp-2",
            arm="budgeter",
            index=2,
            decision="REJECT",
            submitted=False,
            executed=False,
            diagnostic_type="rejected_signal_counterfactual",
        ),
        make_summary(
            "opp-3",
            arm="budgeter",
            index=3,
            submitted=True,
            executed=False,
            unfilled_quantity="1",
            terminal_state="EXPIRED_UNFILLED",
            diagnostic_type="approved_unfilled_counterfactual",
        ),
    )
    result = build_result(baseline, budgeter, policy)

    assert result.validity.valid is True
    counts = result.budgeter_metrics.opportunity_flow["four_counts"]
    assert counts == {
        "raw_signals": 4,
        "common_phase8_eligible_signals": 4,
        "budgeter_approved_signals": 3,
        "executed_orders_with_actual_fill": 2,
    }
    assert result.budgeter_metrics.execution["retention_rate"] == "0.5"
    assert Decimal(result.budgeter_metrics.economics["net_expectancy_per_common_eligible_signal_bps"]) == Decimal("2")
    assert Decimal(result.budgeter_metrics.economics["net_expectancy_per_executed_order_bps"]) == Decimal("4")
    assert result.budgeter_metrics.diagnostics["rejected_signal_counterfactual_count"] == 1
    assert result.budgeter_metrics.diagnostics["approved_unfilled_counterfactual_count"] == 1


def test_unmatched_baseline_is_reported_separately_without_invalidating_primary_match() -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    unmatched = make_summary("baseline-extra", arm="baseline", index=3, common=False)

    result = build_result((*baseline, unmatched), budgeter, policy)

    assert result.validity.valid is True
    assert result.unmatched_baseline_activity["opportunity_count"] == 1
    assert result.unmatched_baseline_activity["included_in_primary_effect"] is False
    assert result.matched_opportunity_count == 2


def test_manifest_gate_claim_must_match_each_frozen_decision_root() -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    changed_root = {
        **budgeter[0].original_decision,
        "phase8_config": {"enabled": False},
    }
    budgeter = (replace(budgeter[0], original_decision=changed_root), budgeter[1])

    result = build_result(baseline, budgeter, policy)

    assert result.validity.valid is False
    assert "gate_enabled_manifest_mismatch:budgeter:opp-0" in result.validity.failures
    assert result.validity.primary_improvement_claim_allowed is False


@pytest.mark.parametrize(
    "mutation,expected_failure",
    [
        ("missing_opportunity", "common_opportunity_sets_differ"),
        ("duplicate", "duplicate_budgeter_common_opportunities"),
        ("cost_model", "non_gate_experiment_configuration_differs"),
        ("horizon", "evaluation_horizon_differs:opp-1"),
        ("missing_outcome", "missing_arm_outcome_asymmetry:opp-1"),
    ],
)
def test_invalid_experiments_cannot_claim_improvement(mutation: str, expected_failure: str) -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    budget_manifest = manifest("budgeter", policy)
    if mutation == "missing_opportunity":
        budgeter = budgeter[:1]
    elif mutation == "duplicate":
        budgeter = (*budgeter, budgeter[0])
    elif mutation == "cost_model":
        budget_manifest = replace(budget_manifest, cost_model_version="different-cost-model")
    elif mutation == "horizon":
        budgeter = (
            budgeter[0],
            make_summary("opp-1", arm="budgeter", index=1, symbol="ETH/USD", horizon_value=2),
        )
    elif mutation == "missing_outcome":
        budgeter = (
            budgeter[0],
            make_summary("opp-1", arm="budgeter", index=1, symbol="ETH/USD", outcome_evaluated=False),
        )

    result = build_result(baseline, tuple(budgeter), policy, budgeter_manifest=budget_manifest)

    assert result.validity.valid is False
    assert expected_failure in result.validity.failures
    assert result.validity.primary_improvement_claim_allowed is False
    assert result.uncertainty is None


def test_maximum_drawdown_concentration_calibration_and_integrity_are_reported() -> None:
    policy = holdout_policy(minimum_clusters=1, maximum_concentration="1")
    baseline = (
        make_summary("opp-0", arm="baseline", index=0, net_amount="-100", symbol="BTC/USD"),
        make_summary("opp-1", arm="baseline", index=1, net_amount="50", symbol="BTC/USD", integrity_failures=1),
    )
    budgeter = (
        make_summary("opp-0", arm="budgeter", index=0, net_amount="-100", symbol="BTC/USD"),
        make_summary("opp-1", arm="budgeter", index=1, net_amount="50", symbol="BTC/USD", integrity_failures=2),
    )

    result = build_result(baseline, budgeter, policy)

    assert Decimal(result.budgeter_metrics.economics["maximum_drawdown"]) == Decimal("0.01")
    assert Decimal(result.budgeter_metrics.economics["concentration_hhi"]) == Decimal("1")
    assert result.budgeter_metrics.calibration["latency"]["available_count"] == 2
    assert result.budgeter_metrics.calibration["cost"]["mean_signed_error"] == "0.5"
    assert result.budgeter_metrics.diagnostics["lifecycle_integrity_failure_count"] == 2


def test_seeded_clustered_block_bootstrap_is_deterministic_and_warns_on_small_sample() -> None:
    observations = tuple(
        MatchedEffectObservation(
            opportunity_key=f"opp-{index}",
            timestamp=BASE + timedelta(hours=index),
            symbol="BTC/USD" if index < 3 else "ETH/USD",
            baseline_net_bps=Decimal("0"),
            budgeter_net_bps=Decimal(str((index % 3) - 1)),
            regime="high_vol" if index % 2 else "normal",
        )
        for index in range(6)
    )
    kwargs = {
        "iterations": 200,
        "block_length": 2,
        "confidence_level": 0.95,
        "seed": 77,
        "multiple_comparison_count": 3,
        "minimum_sample_size": 30,
        "minimum_clusters": 3,
    }

    first = clustered_moving_block_bootstrap(observations, **kwargs)
    second = clustered_moving_block_bootstrap(observations, **kwargs)

    assert first.to_dict() == second.to_dict()
    assert first.method == "paired_symbol_clustered_circular_moving_block_bootstrap_v1"
    assert "sample_size=6" in (first.small_sample_warning or "")
    assert "symbol_clusters=2" in (first.small_sample_warning or "")
    assert first.familywise_confidence_level > first.requested_confidence_level
    assert set(first.regime_effects_bps) == {"high_vol", "normal"}


def test_go_requires_every_frozen_constraint_to_pass() -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    result = build_result(baseline, budgeter, policy)
    engine = FrozenHoldoutDecisionEngine()

    record = engine.decide(
        result=result,
        policy=policy,
        evidence=good_evidence(),
        decided_at=END,
    )

    assert record.decision is ReleaseDecision.GO
    assert not record.failed_rule_ids
    assert not record.uncertain_rule_ids
    assert all(
        rule.status.value in {"PASS", "NOT_REQUIRED"}
        for rule in record.rules
        if rule.required_for_go
    )


def test_one_failed_go_constraint_blocks_release() -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    result = build_result(baseline, budgeter, policy)

    record = FrozenHoldoutDecisionEngine().decide(
        result=result,
        policy=policy,
        evidence=good_evidence(provenance_evidence_credible=False),
        decided_at=END,
    )

    assert record.decision is ReleaseDecision.REJECT
    assert "credible_provenance_evidence" in record.failed_rule_ids


def test_revise_requires_directional_benefit_with_only_incomplete_evidence() -> None:
    policy = holdout_policy(minimum_common=30)
    baseline, budgeter = two_arm_summaries()
    result = build_result(baseline, budgeter, policy)

    record = FrozenHoldoutDecisionEngine().decide(
        result=result,
        policy=policy,
        evidence=good_evidence(),
        decided_at=END,
    )

    assert record.directional_benefit is True
    assert record.decision is ReleaseDecision.REVISE
    assert "minimum_common_opportunities" in record.uncertain_rule_ids


def test_negative_primary_result_is_rejected_even_when_nearby_diagnostics_are_positive() -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries(baseline_bps=("5", "5"), budgeter_bps=("1", "1"))
    result = build_result(baseline, budgeter, policy)

    record = FrozenHoldoutDecisionEngine().decide(
        result=result,
        policy=policy,
        evidence=good_evidence(nearby_setting_effects_bps={"nearby-v1": Decimal("100")}),
        decided_at=END,
    )

    assert record.decision is ReleaseDecision.REJECT
    assert record.directional_benefit is False
    assert "primary_oos_net_expectancy_improved" in record.failed_rule_ids
    assert record.to_dict()["nearby_analysis_replaced_primary_result"] is False


def test_frozen_holdout_policy_cannot_be_mutated_or_decided_early() -> None:
    policy = holdout_policy()
    with pytest.raises(ValidationError):
        policy.minimum_retention_rate = Decimal("0")
    baseline, budgeter = two_arm_summaries()
    result = build_result(baseline, budgeter, policy)

    with pytest.raises(ValueError, match="unfinished frozen holdout"):
        FrozenHoldoutDecisionEngine().decide(
            result=result,
            policy=policy,
            evidence=good_evidence(),
            decided_at=END - timedelta(seconds=1),
        )


def test_report_is_byte_reproducible_and_contains_every_required_product(tmp_path) -> None:
    policy = holdout_policy()
    baseline, budgeter = two_arm_summaries()
    unmatched = make_summary("baseline-extra", arm="baseline", index=3, common=False)
    result = build_result((*baseline, unmatched), budgeter, policy)
    evidence = good_evidence()
    decision = FrozenHoldoutDecisionEngine().decide(
        result=result,
        policy=policy,
        evidence=evidence,
        decided_at=END,
    )
    generator = Step4ReportGenerator()

    first = generator.generate(
        result=result,
        policy=policy,
        evidence=evidence,
        decision=decision,
        report_generated_at=END + timedelta(seconds=1),
    )
    second = generator.generate(
        result=result,
        policy=policy,
        evidence=evidence,
        decision=decision,
        report_generated_at=END + timedelta(seconds=1),
    )
    json_path, markdown_path = first.write(tmp_path)

    assert first.json_bytes() == second.json_bytes()
    assert first.markdown_bytes() == second.markdown_bytes()
    assert json_path.read_bytes() == first.json_bytes()
    assert markdown_path.read_bytes() == first.markdown_bytes()
    parsed = json.loads(first.json_bytes())
    required = {
        "decision_summary",
        "opportunity_flow_table",
        "execution_table",
        "economics_table",
        "execution_quality_table",
        "calibration_table",
        "diagnostics_table",
        "matched_baseline_vs_budgeter_table",
        "holdout_rule_table",
        "decision_record",
        "methodology_metadata",
        "reproducibility_manifest",
        "experiment_validity_audit",
        "data_quality_and_integrity_appendix",
    }
    assert required <= set(parsed)
    assert parsed["unmatched_baseline_activity"]["included_in_primary_effect"] is False
    assert parsed["methodology_metadata"]["counterfactuals"].startswith("diagnostic only")
    assert parsed["reproducibility_manifest"]["random_seed"] == 123
    assert parsed["report_fingerprint"] == first.report_fingerprint
