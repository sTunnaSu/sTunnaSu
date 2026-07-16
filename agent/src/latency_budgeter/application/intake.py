"""Application service for shared observation and raw-signal intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.latency_budgeter.application.classification import (
    BaselineActivityViews,
    SharedCohortClassifier,
)
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory, validate_identifier
from src.latency_budgeter.domain.models import (
    CohortClassification,
    MarketObservation,
    RawStrategySignal,
)
from src.latency_budgeter.domain.timestamps import TIMESTAMP_PRECISION, normalize_timestamp
from src.latency_budgeter.ports.ledger import EventLedger

Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class IntakeResult:
    """Evidence produced when one raw signal is recorded."""

    event: LedgerEvent
    signal: RawStrategySignal
    classification: CohortClassification
    baseline_views: BaselineActivityViews[RawStrategySignal]
    appended: bool
    raw_signal_count_increment: int


@dataclass(frozen=True, slots=True)
class PreparedIntake:
    """Shared Step 1 evidence prepared without writing an event."""

    run_id: str
    decision_id: str
    signal: RawStrategySignal
    classification: CohortClassification
    baseline_views: BaselineActivityViews[RawStrategySignal]
    payload: Mapping[str, Any]


class Phase8IntakeService:
    """Record Step 1 evidence without entering the execution decision path."""

    def __init__(
        self,
        *,
        config: LatencyBudgetConfig,
        ledger: EventLedger,
        identifier_factory: Phase8IdentifierFactory | None = None,
        classifier: SharedCohortClassifier | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.identifier_factory = identifier_factory or Phase8IdentifierFactory()
        self.classifier = classifier or SharedCohortClassifier(config.classifier_version)
        if self.classifier.classification_version != config.classifier_version:
            raise ValueError("classifier version must match the frozen configuration")
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def record_raw_signal(
        self,
        *,
        observation: MarketObservation,
        run_id: str,
        decision_at: datetime,
        strategy_requirements_met: bool,
        signal_key: str = "default",
        signal_metadata: Mapping[str, Any] | None = None,
        signal_id: str | None = None,
        decision_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> IntakeResult:
        """Persist one ``decision_created`` event for a raw strategy signal.

        An idempotent retry returns the original event and reports a zero count
        increment. Baseline activity is always retained in ``baseline_views``.
        """
        prepared = self.prepare_raw_signal(
            observation=observation,
            run_id=run_id,
            decision_at=decision_at,
            strategy_requirements_met=strategy_requirements_met,
            signal_key=signal_key,
            signal_metadata=signal_metadata,
            signal_id=signal_id,
            decision_id=decision_id,
        )
        decision_utc = normalize_timestamp(decision_at)
        payload = dict(prepared.payload)
        resolved_decision_id = prepared.decision_id
        signal = prepared.signal
        classification = prepared.classification
        baseline_views = prepared.baseline_views
        recorded_at = normalize_timestamp(self.clock())
        event = LedgerEvent.create(
            event_type=EventType.DECISION_CREATED,
            occurred_at=decision_utc,
            recorded_at=recorded_at,
            decision_id=resolved_decision_id,
            run_id=run_id,
            signal_id=signal.signal_id,
            payload=payload,
            idempotency_key=idempotency_key or f"decision_created:{resolved_decision_id}",
            source_metadata=observation.source_metadata.to_dict(),
            integrity_metadata={
                "classification_version": classification.classification_version,
                "timestamp_precision": TIMESTAMP_PRECISION,
                "step": "phase8-step1",
                "enforcement_applied": False,
            },
        )
        result = self.ledger.append(event, expected_version=0)
        return IntakeResult(
            event=result.event,
            signal=signal,
            classification=classification,
            baseline_views=baseline_views,
            appended=result.appended,
            raw_signal_count_increment=1 if result.appended else 0,
        )

    def prepare_raw_signal(
        self,
        *,
        observation: MarketObservation,
        run_id: str,
        decision_at: datetime,
        strategy_requirements_met: bool,
        signal_key: str = "default",
        signal_metadata: Mapping[str, Any] | None = None,
        signal_id: str | None = None,
        decision_id: str | None = None,
    ) -> PreparedIntake:
        """Build shared evidence once without persisting a placeholder root."""
        validate_identifier(run_id, "run")
        decision_utc = normalize_timestamp(decision_at)
        resolved_signal_id = signal_id or self.identifier_factory.signal_id(
            run_id=run_id,
            observation_fingerprint=observation.fingerprint,
            strategy_version=observation.strategy_version,
            side=observation.side.value,
            signal_key=str(signal_key),
        )
        validate_identifier(resolved_signal_id, "sig")
        resolved_decision_id = decision_id or self.identifier_factory.decision_id(
            run_id=run_id,
            signal_id=resolved_signal_id,
            config_version=self.config.config_version,
        )
        validate_identifier(resolved_decision_id, "dec")
        signal = RawStrategySignal(
            signal_id=resolved_signal_id,
            run_id=run_id,
            observation_fingerprint=observation.fingerprint,
            generated_at=decision_utc,
            symbol=observation.symbol,
            side=observation.side,
            strategy_version=observation.strategy_version,
            metadata=signal_metadata or {},
        )
        classification = self.classifier.classify(
            observation=observation,
            decision_at=decision_utc,
            freshness_limit_ms=self.config.freshness_limit_ms,
            strategy_requirements_met=strategy_requirements_met,
        )
        baseline_views = BaselineActivityViews.classify(signal, classification)
        payload: Mapping[str, Any] = {
            "observation": observation.to_dict(),
            "raw_strategy_signal": signal.to_dict(),
            "raw_signal_count_increment": 1,
            "shared_cohort_classification": classification.to_dict(),
            "baseline_compatibility": {
                "all_original_baseline_activity": True,
                "matched_baseline_activity": classification.common_phase8_eligible,
                "unmatched_integrity_activity": not classification.common_phase8_eligible,
                "baseline_execution_modified": False,
            },
            "config_version": self.config.config_version,
            "config_fingerprint": self.config.fingerprint,
            "phase8_config": self.config.model_dump(mode="json"),
        }
        return PreparedIntake(
            run_id=run_id,
            decision_id=resolved_decision_id,
            signal=signal,
            classification=classification,
            baseline_views=baseline_views,
            payload=payload,
        )
