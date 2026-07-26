"""Focused tests for the Phase 8-gated Alpaca paper bridge."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.events import EventType
from src.latency_budgeter.persistence.history_memory import InMemoryLatencyHistoryStore
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.tools import build_registry
from src.trading.phase8_paper import (
    PAPER_PROFILE_ID,
    Phase8AlpacaPaperExecutionService,
    Phase8PaperOrderRequest,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _config() -> LatencyBudgetConfig:
    return LatencyBudgetConfig(
        enabled=True,
        tau_ms=1_000_000,
        freshness_limit_ms=5_000,
        minimum_prior_samples=2,
        rolling_history_window=10,
        cold_start_policy="fallback_p90",
        fallback_p90_latency_ms=10,
        required_buffer_bps=3,
        cost_assumptions={
            "maker_fee_bps": 1,
            "taker_fee_bps": 2,
            "spread_bps": 0,
            "slippage_bps": 2,
            "impact_bps": 1,
        },
    )


def _quote(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
    return {
        "status": "ok",
        "feed": "paper-test",
        "quote": {
            "bid": 100,
            "ask": 100.02,
            "bid_size": 1,
            "ask_size": 1,
            "time": (NOW - timedelta(milliseconds=100)).isoformat(),
        },
    }


def _request(**overrides) -> Phase8PaperOrderRequest:
    values = {
        "symbol": "BTC/USD",
        "side": "buy",
        "quantity": Decimal("0.0001"),
        "gross_edge_bps": Decimal("100"),
        "strategy_version": "paper-smoke-v1",
        "strategy_requirements_met": True,
        "submit": False,
        "poll_timeout_seconds": 0,
    }
    values.update(overrides)
    return Phase8PaperOrderRequest(**values)


def _service(**overrides) -> tuple[Phase8AlpacaPaperExecutionService, InMemoryEventLedger]:
    ledger = InMemoryEventLedger()
    values = {
        "config": _config(),
        "ledger": ledger,
        "history": InMemoryLatencyHistoryStore(),
        "clock": lambda: NOW,
        "clock_reader": lambda *_args, **_kwargs: {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "timestamp": NOW.isoformat(),
        },
        "quote_reader": _quote,
        "sleeper": lambda _seconds: None,
    }
    values.update(overrides)
    return Phase8AlpacaPaperExecutionService(**values), ledger


def test_reject_is_persisted_and_never_reaches_broker() -> None:
    calls = []

    def unexpected_submit(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((args, kwargs))
        raise AssertionError("REJECT must never submit")

    service, ledger = _service(order_submitter=unexpected_submit)
    result = service.execute(_request(strategy_requirements_met=False, submit=True))

    assert result["phase8_outcome"] == "REJECT"
    assert result["execution_status"] == "blocked_by_phase8"
    assert result["order_submitted"] is False
    assert result["persisted_event_types"] == [EventType.DECISION_CREATED.value]
    assert len(ledger.read(result["decision_id"])) == 1
    assert calls == []


def test_allow_dry_run_persists_decision_without_submission() -> None:
    service, _ = _service(order_submitter=lambda *_args, **_kwargs: pytest.fail("dry run submitted an order"))

    result = service.execute(_request())

    assert result["phase8_outcome"] == "ALLOW"
    assert result["execution_status"] == "allow_dry_run"
    assert result["decision_persisted"] is True
    assert result["order_submitted"] is False
    json.dumps(result)


def test_allow_uses_decision_id_and_records_real_paper_lifecycle() -> None:
    submitted = []

    def fake_submit(symbol, profile_id, **kwargs):  # noqa: ANN001, ANN202
        submitted.append((symbol, profile_id, kwargs))
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "order_id": "paper-order-1",
            "client_order_id": kwargs["client_order_id"],
            "order_status": "accepted",
            "quantity": kwargs["quantity"],
            "filled_qty": "0",
            "submitted_at": NOW.isoformat(),
        }

    def fake_orders(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return {
            "status": "ok",
            "open_orders": [],
            "executions": [
                {
                    "order_id": "paper-order-1",
                    "client_order_id": submitted[0][2]["client_order_id"],
                    "status": "filled",
                    "quantity": "0.0001",
                    "filled_qty": "0.0001",
                    "filled_avg_price": "100.03",
                    "submitted_at": NOW.isoformat(),
                    "filled_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
            ],
        }

    ticks = iter((0.0, 0.0, 1.0))
    service, ledger = _service(
        order_submitter=fake_submit,
        orders_reader=fake_orders,
        monotonic=lambda: next(ticks, 1.0),
    )

    result = service.execute(_request(submit=True, poll_timeout_seconds=1, poll_interval_seconds=0.1))

    assert result["phase8_outcome"] == "ALLOW"
    assert submitted[0][0:2] == ("BTC/USD", PAPER_PROFILE_ID)
    assert submitted[0][2]["client_order_id"] == result["decision_id"]
    assert result["client_order_id"] == result["decision_id"]
    assert result["execution_status"] == "filled"
    assert result["filled_average_price"] == "100.03"
    assert result["fill_recorded"] is True
    assert result["terminal_recorded"] is True
    event_types = [event.event_type for event in ledger.read(result["decision_id"])]
    assert event_types[:5] == [
        EventType.DECISION_CREATED,
        EventType.ORDER_SUBMITTED,
        EventType.BROKER_ACKNOWLEDGED,
        EventType.FILL_RECEIVED,
        EventType.ORDER_TERMINAL,
    ]
    assert EventType.EXECUTION_EVALUATED in event_types


def test_exact_client_identity_is_persistable_before_broker_submission() -> None:
    ordering: list[str] = []

    def before_submit(authorization) -> None:  # noqa: ANN001
        ordering.append(f"intent:{authorization.client_order_id}")

    def fake_submit(_symbol, _profile_id, **kwargs):  # noqa: ANN001, ANN202
        ordering.append(f"broker:{kwargs['client_order_id']}")
        return {
            "status": "error",
            "error": "explicit broker rejection",
        }

    service, _ = _service(order_submitter=fake_submit)
    result = service.execute(_request(submit=True), before_submit=before_submit)

    assert result["execution_status"] == "broker_submission_failed"
    assert ordering == [
        f"intent:{result['decision_id']}",
        f"broker:{result['decision_id']}",
    ]


def test_failed_pre_submit_persistence_aborts_before_broker() -> None:
    calls: list[str] = []

    def fail_persistence(_authorization) -> None:  # noqa: ANN001
        raise RuntimeError("intent store unavailable")

    service, _ = _service(order_submitter=lambda *_args, **_kwargs: calls.append("broker"))
    with pytest.raises(RuntimeError, match="intent store unavailable"):
        service.execute(_request(submit=True), before_submit=fail_persistence)
    assert calls == []


def test_broker_timeout_is_ambiguous_and_never_blindly_retried() -> None:
    calls = 0

    def timeout(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls
        calls += 1
        raise TimeoutError("response lost")

    service, _ = _service(order_submitter=timeout)
    result = service.execute(_request(submit=True), before_submit=lambda _authorization: None)

    assert calls == 1
    assert result["execution_status"] == "submission_ambiguous"
    assert result["reconciliation_required"] is True
    assert result["client_order_id"] == result["decision_id"]


def test_ambiguous_submission_recovers_by_client_id_without_resubmission() -> None:
    calls = 0
    client_id = ""

    def timeout(*_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal calls, client_id
        calls += 1
        client_id = kwargs["client_order_id"]
        raise TimeoutError("accepted response lost")

    def recovered_orders(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return {
            "status": "ok",
            "open_orders": [],
            "executions": [
                {
                    "order_id": "recovered-order",
                    "client_order_id": client_id,
                    "status": "filled",
                    "order_type": "market",
                    "quantity": "0.0001",
                    "filled_qty": "0.0001",
                    "filled_avg_price": "100.03",
                    "submitted_at": NOW.isoformat(),
                    "filled_at": NOW.isoformat(),
                    "updated_at": NOW.isoformat(),
                }
            ],
        }

    service, _ = _service(order_submitter=timeout, orders_reader=recovered_orders)
    ambiguous = service.execute(_request(submit=True), before_submit=lambda _authorization: None)
    recovered = service.reconcile(ambiguous["decision_id"])

    assert calls == 1
    assert recovered["execution_status"] == "filled"
    assert recovered["order_id"] == "recovered-order"
    assert recovered["client_order_id"] == ambiguous["decision_id"]


def test_non_paper_profile_fails_before_quote_or_order() -> None:
    calls = []

    class LiveProfile:
        id = "alpaca-live-trade"
        connector = "alpaca"
        environment = "live"
        readonly = False

    service, _ = _service(
        profile_id="alpaca-live-trade",
        profile_resolver=lambda _profile_id: LiveProfile(),
        quote_reader=lambda *_args, **_kwargs: calls.append("quote"),
        order_submitter=lambda *_args, **_kwargs: calls.append("order"),
    )

    result = service.execute(_request(submit=True))

    assert result["status"] == "error"
    assert result["error_code"] == "paper_profile_required"
    assert result["order_submitted"] is False
    assert calls == []


def test_phase8_bridge_redacts_secrets_from_errors() -> None:
    service, _ = _service(
        profile_resolver=lambda _profile_id: (_ for _ in ()).throw(
            RuntimeError("Authorization: Bearer fake-secret-token")
        )
    )
    result = service.execute(_request(submit=False))
    assert result["status"] == "error"
    assert "fake-secret-token" not in result["error"]
    assert "REDACTED" in result["error"]


def test_phase8_paper_tool_is_registered() -> None:
    names = build_registry(include_shell_tools=False).tool_names
    assert "trading_phase8_paper_order" in names
    assert "trading_phase8_paper_reconcile" in names


def test_phase8_paper_tool_rejects_direct_submission() -> None:
    from src.tools.phase8_paper_tool import TradingPhase8PaperOrderTool

    result = json.loads(
        TradingPhase8PaperOrderTool().execute(
            symbol="BTC/USD",
            side="buy",
            quantity=0.001,
            gross_edge_bps=20,
            strategy_version="test:v1",
            strategy_requirements_met=True,
            submit=True,
        )
    )
    assert result["status"] == "error"
    assert "direct Phase 8 paper submission is disabled" in result["error"]
