"""Ledger-to-report reproducibility integration for Phase 8 Step 4."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from src.latency_budgeter.projections.research_summary import ResearchDecisionSummaryProjector
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
from tests.latency_budgeter.test_step4_outcomes import (
    BASE,
    add_execution,
    build_context,
    fixed_trigger,
)


def _manifest(arm: str, context, policy: HoldoutDecisionPolicy) -> ExperimentManifest:
    return ExperimentManifest(
        arm_name=arm,
        gate_enabled=arm == "budgeter",
        strategy_version="strategy-v1",
        universe=("BTC/USD",),
        holdout_start=policy.holdout_start,
        holdout_end=policy.holdout_end,
        initial_capital=Decimal("10000"),
        sizing_rule_version="sizing-v1",
        participation_rule_version="participation-v1",
        order_model_version="order-v1",
        cost_model_version="cost-v1",
        evaluation_version=context.service.policy.methodology_version,
        evaluation_policy_fingerprint=context.service.policy.fingerprint,
        holdout_policy_fingerprint=policy.fingerprint,
        dataset_version="dataset-v1",
        code_version="test-code-v1",
        config_version=context.config.config_version,
        gate_config_fingerprint=context.config.fingerprint,
        non_gate_config_fingerprint=non_gate_config_fingerprint(
            context.config.model_dump(mode="json")
        ),
        cohort_definition_version="phase8-cohort-v1",
        query_parameters={"case": "immutable-ledger-replay"},
    )


def test_immutable_ledgers_rebuild_the_same_matched_report_bytes() -> None:
    baseline_context = build_context(enabled=False)
    budgeter_context = build_context(enabled=True)
    add_execution(baseline_context)
    add_execution(budgeter_context)
    baseline_context.service.evaluate(baseline_context.root.decision_id, fixed_trigger(price="102"))
    budgeter_context.service.evaluate(budgeter_context.root.decision_id, fixed_trigger(price="102"))
    projector = ResearchDecisionSummaryProjector()
    baseline_summary = projector.replay(
        baseline_context.ledger.read(baseline_context.root.decision_id)
    )
    budgeter_summary = projector.replay(
        budgeter_context.ledger.read(budgeter_context.root.decision_id)
    )
    assert baseline_summary.opportunity_key == budgeter_summary.opportunity_key
    policy = HoldoutDecisionPolicy(
        preregistered_at=BASE - timedelta(days=2),
        holdout_start=BASE - timedelta(days=1),
        holdout_end=BASE + timedelta(days=1),
        primary_setting_id="primary-v1",
        minimum_common_opportunities=2,
        minimum_symbol_clusters=2,
        minimum_retention_rate=Decimal("0.5"),
        maximum_concentration=Decimal("1"),
        bootstrap_iterations=100,
        bootstrap_block_length=1,
        require_nearby_agreement=False,
        require_regime_coverage=False,
    )
    result = MatchedExperimentBuilder().build(
        baseline=ExperimentArm(
            _manifest("baseline", baseline_context, policy), (baseline_summary,)
        ),
        budgeter=ExperimentArm(
            _manifest("budgeter", budgeter_context, policy), (budgeter_summary,)
        ),
        holdout_policy=policy,
    )
    assert result.validity.valid is True
    evidence = SupplementaryReleaseEvidence(
        primary_setting_id="primary-v1",
        nearby_setting_effects_bps={},
        provenance_evidence_credible=True,
        cost_evidence_credible=True,
        calibration_reliable=True,
        regime_coverage_complete=None,
    )
    decision = FrozenHoldoutDecisionEngine().decide(
        result=result,
        policy=policy,
        evidence=evidence,
        decided_at=policy.holdout_end,
    )
    assert decision.decision is ReleaseDecision.REJECT
    generated_at = policy.holdout_end + timedelta(seconds=1)
    first = Step4ReportGenerator().generate(
        result=result,
        policy=policy,
        evidence=evidence,
        decision=decision,
        report_generated_at=generated_at,
    )

    rebuilt_baseline = projector.replay(
        baseline_context.ledger.read(baseline_context.root.decision_id)
    )
    rebuilt_budgeter = projector.replay(
        budgeter_context.ledger.read(budgeter_context.root.decision_id)
    )
    rebuilt_result = MatchedExperimentBuilder().build(
        baseline=ExperimentArm(
            _manifest("baseline", baseline_context, policy), (rebuilt_baseline,)
        ),
        budgeter=ExperimentArm(
            _manifest("budgeter", budgeter_context, policy), (rebuilt_budgeter,)
        ),
        holdout_policy=policy,
    )
    rebuilt_decision = FrozenHoldoutDecisionEngine().decide(
        result=rebuilt_result,
        policy=policy,
        evidence=evidence,
        decided_at=policy.holdout_end,
    )
    second = Step4ReportGenerator().generate(
        result=rebuilt_result,
        policy=policy,
        evidence=evidence,
        decision=rebuilt_decision,
        report_generated_at=generated_at,
    )

    assert first.json_bytes() == second.json_bytes()
    assert first.markdown_bytes() == second.markdown_bytes()
    assert first.to_dict()["reproducibility_manifest"]["baseline_ledger_snapshot_hash"]
    assert first.to_dict()["reproducibility_manifest"]["budgeter_ledger_snapshot_hash"]
