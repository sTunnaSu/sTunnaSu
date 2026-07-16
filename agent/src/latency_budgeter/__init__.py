"""Phase 8 evidence foundation and point-in-time enabled decision gate."""

from src.latency_budgeter.application.gate import GateResult, LatencyBudgetDecisionGate
from src.latency_budgeter.application.intake import IntakeResult, Phase8IntakeService
from src.latency_budgeter.application.classification import (
    BaselineActivityViews,
    SharedCohortClassifier,
)
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.decisions import (
    ApprovedOpportunity,
    DecisionOutcome,
    DecisionReason,
)
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.models import MarketObservation, RawStrategySignal
from src.latency_budgeter.persistence.approved_memory import InMemoryApprovedOpportunityPort
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.history_sqlite import SQLiteLatencyHistoryStore
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger
from src.latency_budgeter.projections.decision_summary import (
    DecisionSummary,
    DecisionSummaryProjector,
)

__all__ = [
    "BaselineActivityViews",
    "ApprovedOpportunity",
    "DecisionSummary",
    "DecisionSummaryProjector",
    "EventType",
    "DecisionOutcome",
    "DecisionReason",
    "GateResult",
    "InMemoryApprovedOpportunityPort",
    "InMemoryEventLedger",
    "InMemoryLatencyHistoryStore",
    "IntakeResult",
    "LatencyBudgetConfig",
    "LatencyBudgetDecisionGate",
    "LedgerEvent",
    "MarketObservation",
    "Phase8IdentifierFactory",
    "Phase8IntakeService",
    "RawStrategySignal",
    "SQLiteEventLedger",
    "SQLiteLatencyHistoryStore",
    "SharedCohortClassifier",
]
