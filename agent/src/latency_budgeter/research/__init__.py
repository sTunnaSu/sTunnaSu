"""Causal research, validation, release rules, and reporting for Step 4."""

from src.latency_budgeter.research.decision_rules import (
    EvidenceStatus,
    FrozenHoldoutDecisionEngine,
    ReleaseDecision,
    ReleaseDecisionRecord,
    RuleEvidence,
    SupplementaryReleaseEvidence,
)
from src.latency_budgeter.research.experiment import (
    ArmMetrics,
    ExperimentArm,
    ExperimentManifest,
    ExperimentValidity,
    MatchedExperimentBuilder,
    MatchedExperimentResult,
)
from src.latency_budgeter.research.policy import HoldoutDecisionPolicy
from src.latency_budgeter.research.reporting import Step4ReportGenerator, Step4ResearchReport
from src.latency_budgeter.research.statistics import (
    ClusteredBlockBootstrapResult,
    MatchedEffectObservation,
    clustered_moving_block_bootstrap,
)

__all__ = [
    "ArmMetrics",
    "ClusteredBlockBootstrapResult",
    "EvidenceStatus",
    "ExperimentArm",
    "ExperimentManifest",
    "ExperimentValidity",
    "FrozenHoldoutDecisionEngine",
    "HoldoutDecisionPolicy",
    "MatchedEffectObservation",
    "MatchedExperimentBuilder",
    "MatchedExperimentResult",
    "ReleaseDecision",
    "ReleaseDecisionRecord",
    "RuleEvidence",
    "Step4ReportGenerator",
    "Step4ResearchReport",
    "SupplementaryReleaseEvidence",
    "clustered_moving_block_bootstrap",
]
