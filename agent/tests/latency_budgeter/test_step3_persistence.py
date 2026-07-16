"""SQLite migration and process-restart tests for Step 3."""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from decimal import Decimal

from src.latency_budgeter.application.lifecycle import ExecutionLifecycleService
from src.latency_budgeter.domain.events import EventType
from src.latency_budgeter.domain.lifecycle import (
    FillObservation,
    HandoffState,
    SubmissionObservation,
    TerminalObservation,
    TerminalReason,
    TerminalState,
)
from src.latency_budgeter.persistence.history_sqlite import (
    HISTORY_SCHEMA_NAME,
    HISTORY_SCHEMA_VERSION,
    SQLiteLatencyHistoryStore,
)
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger

from .test_step3_lifecycle import BASE, build_context


def test_history_v1_migrates_additively_to_order_correlated_v2(tmp_path) -> None:
    path = tmp_path / "history-v1.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE phase8_schema_versions (
            schema_name TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL
        );
        INSERT INTO phase8_schema_versions(schema_name, schema_version)
        VALUES ('phase8_latency_history', 1);
        CREATE TABLE phase8_latency_samples (
            sample_id TEXT PRIMARY KEY,
            decision_id TEXT NOT NULL,
            component TEXT NOT NULL,
            value_ms TEXT NOT NULL,
            component_available_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            component_definition_version TEXT NOT NULL,
            estimator_schema_version TEXT NOT NULL,
            valid INTEGER NOT NULL,
            invalid_reason TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            unit TEXT NOT NULL,
            semantic_fingerprint TEXT NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    store = SQLiteLatencyHistoryStore(path)
    check = sqlite3.connect(path)
    columns = {row[1] for row in check.execute("PRAGMA table_info(phase8_latency_samples)")}
    version = check.execute(
        "SELECT schema_version FROM phase8_schema_versions WHERE schema_name = ?",
        (HISTORY_SCHEMA_NAME,),
    ).fetchone()[0]
    check.close()
    store.close()

    assert "order_id" in columns
    assert version == HISTORY_SCHEMA_VERSION == 2


def test_sqlite_restart_replays_submission_and_completes_without_duplicate_order(tmp_path) -> None:
    source = build_context()
    event_path = tmp_path / "events.db"
    history_path = tmp_path / "history.db"
    ledger1 = SQLiteEventLedger(event_path)
    history1 = SQLiteLatencyHistoryStore(history_path)
    ledger1.append(source.root, expected_version=0)
    service1 = ExecutionLifecycleService(
        config=source.config,
        ledger=ledger1,
        history=history1,
        clock=source.clock,
    )
    authorization = service1.authorize(source.root.decision_id)
    submitted = SubmissionObservation(
        order_id="order-1",
        client_order_id=authorization.client_order_id,
        submitted_at=BASE + timedelta(milliseconds=200),
        quantity=Decimal("1"),
        order_type="market",
    )
    service1.record_submission(authorization, submitted)
    ledger1.close()
    history1.close()

    ledger2 = SQLiteEventLedger(event_path)
    history2 = SQLiteLatencyHistoryStore(history_path)
    service2 = ExecutionLifecycleService(
        config=source.config,
        ledger=ledger2,
        history=history2,
        clock=source.clock,
    )
    recovered = service2.authorize(source.root.decision_id)
    assert recovered.state is HandoffState.ALREADY_SUBMITTED
    duplicate = service2.record_submission(recovered, submitted)
    assert duplicate.appended is False
    service2.record_fill(
        recovered,
        FillObservation(
            order_id="order-1",
            fill_id="fill-1",
            fill_at=BASE + timedelta(milliseconds=300),
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("100"),
            cumulative_filled_quantity=Decimal("1"),
            unfilled_quantity=Decimal("0"),
            decision_price=Decimal("100"),
            fee=Decimal("0"),
        ),
    )
    service2.record_terminal(
        recovered,
        TerminalObservation(
            order_id="order-1",
            terminal_at=BASE + timedelta(milliseconds=300),
            terminal_state=TerminalState.FULLY_FILLED,
            reason_code=TerminalReason.FILLED,
            executed_quantity=Decimal("1"),
            unfilled_quantity=Decimal("0"),
            engine_status="filled",
        ),
    )

    assert [event.event_type for event in ledger2.read(source.root.decision_id)].count(
        EventType.ORDER_SUBMITTED
    ) == 1
    assert service2.projection(source.root.decision_id).terminal_state is TerminalState.FULLY_FILLED
    ledger2.close()
    history2.close()

