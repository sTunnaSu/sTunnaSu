"""Generous microbenchmarks that detect pathological Step 2 regressions."""

from __future__ import annotations

from datetime import timedelta
from statistics import median
from time import perf_counter_ns

from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.history import HistoryQuery, LatencyComponent, LatencySample
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.values import Milliseconds
from src.latency_budgeter.estimation.percentile import nearest_rank_percentile
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.memory import InMemoryEventLedger

from .conftest import NOW
from .test_step2_gate import build_gate, run


def _median_call_us(callable_, repetitions: int) -> float:
    samples = []
    for _ in range(repetitions):
        started = perf_counter_ns()
        callable_()
        samples.append((perf_counter_ns() - started) / 1_000)
    return median(samples)


def test_history_percentile_and_event_append_microbenchmarks() -> None:
    history = InMemoryLatencyHistoryStore()
    for index in range(1_000):
        available = NOW - timedelta(milliseconds=index + 1)
        history.add(
            LatencySample(
                sample_id=f"sample-{index:04d}",
                decision_id=f"decision-{index:04d}",
                component=LatencyComponent.FILL,
                value_ms=Milliseconds(index % 100),
                component_available_at=available,
                recorded_at=available,
                component_definition_version="definition-v1",
                estimator_schema_version="estimator-v1",
            )
        )
    query = HistoryQuery(
        component=LatencyComponent.FILL,
        decision_at=NOW,
        current_decision_id="current",
        rolling_window=100,
        component_definition_version="definition-v1",
        estimator_schema_version="estimator-v1",
    )
    window = history.prior_window(query)
    history_us = _median_call_us(lambda: history.prior_window(query), 200)
    percentile_us = _median_call_us(
        lambda: nearest_rank_percentile((row.value_ms for row in window.samples), 90),
        500,
    )

    ledger = InMemoryEventLedger()
    factory = Phase8IdentifierFactory()
    append_durations = []
    for index in range(200):
        run_id = factory.new_run_id()
        signal_id = factory.signal_id(
            run_id=run_id,
            observation_fingerprint=f"{index:064x}",
            strategy_version="benchmark-v1",
            side="buy",
            signal_key="benchmark",
        )
        decision_id = factory.decision_id(
            run_id=run_id,
            signal_id=signal_id,
            config_version="phase8-step1-v1",
        )
        event = LedgerEvent.create(
            event_type=EventType.DECISION_CREATED,
            occurred_at=NOW,
            recorded_at=NOW,
            decision_id=decision_id,
            run_id=run_id,
            signal_id=signal_id,
            payload={"benchmark": index},
            idempotency_key=f"benchmark:{decision_id}",
        )
        started = perf_counter_ns()
        ledger.append(event, expected_version=0)
        append_durations.append((perf_counter_ns() - started) / 1_000)
    append_us = median(append_durations)

    gate, _, _, _, _ = build_gate()
    decision_durations = []
    for _ in range(100):
        started = perf_counter_ns()
        run(gate)
        decision_durations.append((perf_counter_ns() - started) / 1_000)
    decision_us = median(decision_durations)

    print(
        f"phase8 microbenchmarks median_us: history={history_us:.2f}, "
        f"percentile={percentile_us:.2f}, decision={decision_us:.2f}, "
        f"event_append={append_us:.2f}"
    )
    assert len(window.samples) == 100
    assert history_us < 100_000
    assert percentile_us < 20_000
    assert decision_us < 200_000
    assert append_us < 20_000
