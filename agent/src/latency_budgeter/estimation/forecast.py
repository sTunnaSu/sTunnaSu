"""Strict-prior end-to-end latency forecasting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import (
    AvailableLatencyMeasurement,
    ComponentEstimateMode,
    ComponentForecast,
    EstimatorMode,
    LatencyForecast,
    PreDecisionLatencyMeasurements,
)
from src.latency_budgeter.domain.errors import InsufficientLatencyHistory
from src.latency_budgeter.domain.history import HistoryQuery, LatencyComponent
from src.latency_budgeter.domain.timestamps import normalize_timestamp
from src.latency_budgeter.domain.values import Milliseconds
from src.latency_budgeter.estimation.percentile import nearest_rank_percentile
from src.latency_budgeter.ports.history import LatencyHistoryStore


@dataclass(frozen=True, slots=True)
class _EstimateResult:
    forecast: ComponentForecast
    used_fallback: bool


class PriorOnlyLatencyForecaster:
    """Forecast components without current-row or post-decision leakage."""

    def __init__(self, history: LatencyHistoryStore) -> None:
        self.history = history

    def _history_or_fallback(
        self,
        *,
        component: LatencyComponent,
        decision_at: datetime,
        current_decision_id: str,
        config: LatencyBudgetConfig,
        excluded_current_measurement_reason: str = "",
    ) -> _EstimateResult:
        query = HistoryQuery(
            component=component,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            rolling_window=config.rolling_history_window,
            component_definition_version=config.component_definition_version,
            estimator_schema_version=config.estimator_schema_version,
        )
        window = self.history.prior_window(query)
        if len(window.samples) >= config.minimum_prior_samples:
            value = nearest_rank_percentile(
                (sample.value_ms for sample in window.samples),
                config.latency_percentile,
            )
            return _EstimateResult(
                ComponentForecast(
                    component=component,
                    value_ms=value,
                    mode=ComponentEstimateMode.ROLLING_PRIOR_ONLY,
                    prior_sample_count=len(window.samples),
                    window_oldest_available_at=window.oldest_available_at,
                    window_newest_available_at=window.newest_available_at,
                    percentile=config.latency_percentile,
                    excluded_current_measurement_reason=excluded_current_measurement_reason,
                    component_definition_version=config.component_definition_version,
                ),
                used_fallback=False,
            )
        if config.cold_start_policy == "fallback_p90" and config.fallback_p90_latency_ms > 0:
            return _EstimateResult(
                ComponentForecast(
                    component=component,
                    value_ms=Milliseconds(config.fallback_p90_latency_ms),
                    mode=ComponentEstimateMode.COLD_START_FALLBACK,
                    prior_sample_count=len(window.samples),
                    window_oldest_available_at=window.oldest_available_at,
                    window_newest_available_at=window.newest_available_at,
                    percentile=90,
                    excluded_current_measurement_reason=excluded_current_measurement_reason,
                    component_definition_version=config.component_definition_version,
                ),
                used_fallback=True,
            )
        raise InsufficientLatencyHistory(
            f"{component.value} has {len(window.samples)} valid prior samples; {config.minimum_prior_samples} required"
        )

    def _measured_or_estimated(
        self,
        *,
        component: LatencyComponent,
        measurement: AvailableLatencyMeasurement | None,
        decision_at: datetime,
        current_decision_id: str,
        config: LatencyBudgetConfig,
    ) -> _EstimateResult:
        excluded_reason = ""
        if measurement is not None:
            if measurement.component is not component:
                raise ValueError(f"measurement component must be {component.value}")
            if not measurement.valid:
                excluded_reason = "current_measurement_invalid"
            elif measurement.available_at >= decision_at:
                excluded_reason = "current_measurement_unavailable_at_decision"
            elif measurement.component_definition_version != config.component_definition_version:
                excluded_reason = "current_measurement_incompatible_definition"
            elif measurement.unit != "ms":
                excluded_reason = "current_measurement_incompatible_unit"
            else:
                return _EstimateResult(
                    ComponentForecast(
                        component=component,
                        value_ms=measurement.value_ms,
                        mode=ComponentEstimateMode.MEASURED_PRE_DECISION,
                        measured_available_at=measurement.available_at,
                        component_definition_version=measurement.component_definition_version,
                        unit=measurement.unit,
                    ),
                    used_fallback=False,
                )
        return self._history_or_fallback(
            component=component,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            config=config,
            excluded_current_measurement_reason=excluded_reason,
        )

    def forecast(
        self,
        *,
        decision_at: datetime,
        current_decision_id: str,
        data_age_ms: Milliseconds,
        config: LatencyBudgetConfig,
        measurements: PreDecisionLatencyMeasurements | None = None,
    ) -> LatencyForecast:
        """Build a conservative forecast from direct, measured, or strict-prior data."""
        decision_at = normalize_timestamp(decision_at)
        measurements = measurements or PreDecisionLatencyMeasurements()
        decision = self._measured_or_estimated(
            component=LatencyComponent.DECISION,
            measurement=measurements.decision,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            config=config,
        )
        risk = self._measured_or_estimated(
            component=LatencyComponent.RISK_PROCESSING,
            measurement=measurements.risk_processing,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            config=config,
        )
        submission = self._history_or_fallback(
            component=LatencyComponent.SUBMISSION,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            config=config,
        )
        if config.acknowledgement_mode == "unsupported":
            acknowledgement = _EstimateResult(
                ComponentForecast(
                    component=LatencyComponent.ACKNOWLEDGEMENT,
                    value_ms=Milliseconds(0),
                    mode=ComponentEstimateMode.UNSUPPORTED_BY_VENUE,
                    component_definition_version=config.component_definition_version,
                ),
                used_fallback=False,
            )
        else:
            acknowledgement = self._history_or_fallback(
                component=LatencyComponent.ACKNOWLEDGEMENT,
                decision_at=decision_at,
                current_decision_id=current_decision_id,
                config=config,
            )
        fill = self._history_or_fallback(
            component=LatencyComponent.FILL,
            decision_at=decision_at,
            current_decision_id=current_decision_id,
            config=config,
        )
        results = (decision, risk, submission, acknowledgement, fill)
        total = data_age_ms
        for result in results:
            total = total + result.forecast.value_ms
        mode = (
            EstimatorMode.COLD_START_FALLBACK
            if any(result.used_fallback for result in results)
            else EstimatorMode.ROLLING_PRIOR_ONLY
        )
        return LatencyForecast(
            data_age_ms=data_age_ms,
            decision=decision.forecast,
            risk_processing=risk.forecast,
            submission=submission.forecast,
            acknowledgement=acknowledgement.forecast,
            fill=fill.forecast,
            total_ms=total,
            estimator_mode=mode,
            estimator_version=config.estimator_schema_version,
        )
