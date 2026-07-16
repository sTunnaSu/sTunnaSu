"""Strict-prior history, percentile, migration, and concurrency tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from src.latency_budgeter.domain.history import (
    HistoryQuery,
    LatencyComponent,
    LatencySample,
)
from src.latency_budgeter.domain.values import Milliseconds
from src.latency_budgeter.estimation.percentile import (
    PERCENTILE_DEFINITION,
    nearest_rank_percentile,
)
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.history_sqlite import SQLiteLatencyHistoryStore

from .conftest import NOW

DEFINITION = "phase8-latency-component-v1"
ESTIMATOR = "phase8-prior-percentile-v1"


def sample(
    sample_id: str,
    *,
    value: float = 10,
    available_offset_ms: int = -1,
    decision_id: str = "dec-prior",
    component: LatencyComponent = LatencyComponent.FILL,
    valid: bool = True,
    definition: str = DEFINITION,
    estimator: str = ESTIMATOR,
) -> LatencySample:
    available = NOW + timedelta(milliseconds=available_offset_ms)
    return LatencySample(
        sample_id=sample_id,
        decision_id=decision_id,
        component=component,
        value_ms=Milliseconds(value),
        component_available_at=available,
        recorded_at=available,
        component_definition_version=definition,
        estimator_schema_version=estimator,
        valid=valid,
        invalid_reason="invalid" if not valid else "",
    )


def query(*, window: int = 10) -> HistoryQuery:
    return HistoryQuery(
        component=LatencyComponent.FILL,
        decision_at=NOW,
        current_decision_id="dec-current",
        rolling_window=window,
        component_definition_version=DEFINITION,
        estimator_schema_version=ESTIMATOR,
    )


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_history_excludes_current_future_equal_invalid_incompatible_and_invalidated(
    store_kind: str,
    tmp_path,
) -> None:
    store = (
        InMemoryLatencyHistoryStore() if store_kind == "memory" else SQLiteLatencyHistoryStore(tmp_path / "phase8.db")
    )
    store.add(sample("eligible", value=11, available_offset_ms=-1))
    store.add(sample("current", decision_id="dec-current", available_offset_ms=-2))
    store.add(sample("equal", available_offset_ms=0))
    store.add(sample("future", available_offset_ms=1))
    store.add(sample("invalid", available_offset_ms=-3, valid=False))
    store.add(sample("wrong-definition", available_offset_ms=-4, definition="v2"))
    store.add(sample("wrong-estimator", available_offset_ms=-5, estimator="v2"))
    late_recorded = sample("late-recorded", available_offset_ms=-7)
    late_recorded = LatencySample(
        **{
            **late_recorded.to_dict(),
            "recorded_at": NOW + timedelta(microseconds=1),
            "component": late_recorded.component,
            "value_ms": late_recorded.value_ms,
            "component_available_at": late_recorded.component_available_at,
        }
    )
    store.add(late_recorded)
    store.add(sample("invalidated", available_offset_ms=-6))
    store.invalidate(
        "invalidated",
        invalidated_at=NOW,
        reason="integrity_failure",
        integrity_event_id="evt-integrity",
    )

    result = store.prior_window(query())

    assert [row.sample_id for row in result.samples] == ["eligible"]
    store.close()


def test_rolling_window_is_applied_after_filtering_with_deterministic_ties() -> None:
    store = InMemoryLatencyHistoryStore()
    for sample_id, offset in (("a", -3), ("b", -2), ("c", -1), ("d", -1)):
        store.add(sample(sample_id, available_offset_ms=offset))

    result = store.prior_window(query(window=2))

    assert [row.sample_id for row in result.samples] == ["d", "c"]
    assert result.oldest_available_at == NOW - timedelta(milliseconds=1)
    assert result.newest_available_at == NOW - timedelta(milliseconds=1)


def test_nearest_rank_percentile_definition_and_duplicates() -> None:
    values = [Milliseconds(value) for value in (1, 2, 2, 4, 100)]

    assert nearest_rank_percentile(values, 75) == Milliseconds(4)
    assert nearest_rank_percentile(values, 90) == Milliseconds(100)
    assert PERCENTILE_DEFINITION == "nearest-rank-v1:sorted-ascending[ceil(p*n/100)-1]"


def test_invalidation_after_decision_does_not_rewrite_prior_information() -> None:
    store = InMemoryLatencyHistoryStore()
    store.add(sample("later-invalidated", value=20))
    store.invalidate(
        "later-invalidated",
        invalidated_at=NOW + timedelta(milliseconds=1),
        reason="later_audit",
        integrity_event_id="evt-later",
    )

    assert [row.sample_id for row in store.prior_window(query()).samples] == ["later-invalidated"]


def test_concurrent_insertions_never_admit_non_prior_rows() -> None:
    store = InMemoryLatencyHistoryStore()
    store.add(sample("prior", value=7, available_offset_ms=-1))

    rows = [sample(f"future-{idx}", available_offset_ms=idx + 1) for idx in range(30)] + [
        sample(f"equal-{idx}", available_offset_ms=0) for idx in range(30)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(store.add, row) for row in rows]
        snapshots = [pool.submit(store.prior_window, query()) for _ in range(30)]
        for future in futures:
            assert future.result() is True
        for snapshot in snapshots:
            assert [row.sample_id for row in snapshot.result().samples] == ["prior"]


def test_sqlite_history_schema_can_share_event_database(tmp_path) -> None:
    from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger

    path = tmp_path / "shared.db"
    event_ledger = SQLiteEventLedger(path)
    history = SQLiteLatencyHistoryStore(path)
    history.add(sample("shared", value=9))

    assert history.prior_window(query()).samples[0].value_ms == Milliseconds(9)
    history.close()
    event_ledger.close()
