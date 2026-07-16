"""Shared deterministic Phase 8 test fixtures."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.models import MarketObservation, SourceMetadata

NOW = datetime(2026, 7, 16, 12, 0, 0, 123456, tzinfo=timezone.utc)


@pytest.fixture
def phase8_config() -> LatencyBudgetConfig:
    """Return a disabled, deterministic Step 1 configuration."""
    return LatencyBudgetConfig(
        enabled=False,
        freshness_limit_ms=5_000,
        cost_assumptions={
            "maker_fee_bps": 1.0,
            "taker_fee_bps": 2.0,
            "spread_bps": 3.0,
            "slippage_bps": 4.0,
            "impact_bps": 1.0,
        },
    )


@pytest.fixture
def observation() -> MarketObservation:
    """Return a fresh, fully attributed BTC observation."""
    return MarketObservation.from_input(
        source="alpaca-iex",
        observed_at=NOW - timedelta(milliseconds=900),
        source_capture_at=NOW - timedelta(milliseconds=400),
        timezone="UTC",
        symbol="BTC/USD",
        side="buy",
        strategy_version="strategy-v1",
        source_metadata=SourceMetadata(
            provider="alpaca",
            feed="iex-paper",
            venue="alpaca-paper",
            source_event_id="quote-123",
            capture_method="sdk",
        ),
        raw_payload={"bid": 64_000.0, "ask": 64_001.0},
    )


@pytest.fixture
def identities() -> tuple[str, str, str]:
    """Return one valid run/signal/decision identity chain."""
    factory = Phase8IdentifierFactory()
    run_id = factory.new_run_id()
    signal_id = factory.signal_id(
        run_id=run_id,
        observation_fingerprint="a" * 64,
        strategy_version="strategy-v1",
        side="buy",
        signal_key="fixture",
    )
    decision_id = factory.decision_id(
        run_id=run_id,
        signal_id=signal_id,
        config_version="phase8-step1-v1",
    )
    return run_id, signal_id, decision_id


@pytest.fixture
def event_factory(identities):
    """Create deterministic-time events with fresh IDs."""
    run_id, signal_id, decision_id = identities

    def make(
        event_type: EventType = EventType.DECISION_CREATED,
        *,
        payload=None,
        idempotency_key: str | None = None,
        event_id: str | None = None,
        occurred_offset_ms: int = 0,
    ) -> LedgerEvent:
        return LedgerEvent.create(
            event_type=event_type,
            occurred_at=NOW + timedelta(milliseconds=occurred_offset_ms),
            recorded_at=NOW + timedelta(seconds=1),
            decision_id=decision_id,
            run_id=run_id,
            signal_id=signal_id,
            payload=payload or {"value": 1},
            idempotency_key=idempotency_key or f"{event_type.value}:{occurred_offset_ms}",
            event_id=event_id,
        )

    return make
