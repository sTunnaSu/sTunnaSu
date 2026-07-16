"""Append-only ledger, migration, idempotency, and concurrency tests."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from src.latency_budgeter.domain.errors import (
    AggregateIntegrityError,
    ConcurrentAppendError,
    IdempotencyConflictError,
    IdentifierCollisionError,
    LedgerMigrationError,
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)
from src.latency_budgeter.domain.events import EventType, LedgerEvent
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.persistence.memory import InMemoryEventLedger
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger


@pytest.mark.parametrize("ledger_kind", ["memory", "sqlite"])
def test_duplicate_append_is_idempotent(ledger_kind, tmp_path, event_factory) -> None:
    ledger = InMemoryEventLedger() if ledger_kind == "memory" else SQLiteEventLedger(tmp_path / "phase8.db")
    event = event_factory()

    first = ledger.append(event, expected_version=0)
    second = ledger.append(event, expected_version=0)

    assert first.appended is True
    assert second.appended is False
    assert second.event == first.event
    assert len(ledger.read(event.decision_id)) == 1
    ledger.close()


def test_conflicting_idempotency_key_is_rejected(event_factory) -> None:
    ledger = InMemoryEventLedger()
    first = event_factory(idempotency_key="same-key", payload={"value": 1})
    conflict = event_factory(idempotency_key="same-key", payload={"value": 2})
    ledger.append(first)

    with pytest.raises(IdempotencyConflictError):
        ledger.append(conflict)


def test_duplicate_event_identifier_with_different_content_is_rejected(event_factory) -> None:
    ledger = InMemoryEventLedger()
    event_id = Phase8IdentifierFactory.new_event_id()
    ledger.append(event_factory(event_id=event_id, payload={"value": 1}))

    with pytest.raises(IdentifierCollisionError):
        ledger.append(
            event_factory(
                event_id=event_id,
                idempotency_key="different-key",
                payload={"value": 2},
            )
        )

    same_content_new_key = event_factory(
        event_id=event_id,
        idempotency_key="another-key",
        payload={"value": 1},
    )
    with pytest.raises(IdentifierCollisionError):
        ledger.append(same_content_new_key)


def test_event_order_and_aggregate_identity_are_enforced(event_factory, identities) -> None:
    ledger = InMemoryEventLedger()
    first = ledger.append(event_factory(), expected_version=0).event
    second = ledger.append(
        event_factory(
            EventType.ORDER_SUBMITTED,
            occurred_offset_ms=1,
            idempotency_key="order-submitted",
        ),
        expected_version=1,
    ).event
    assert [event.aggregate_version for event in ledger.read(first.decision_id)] == [1, 2]
    assert second.aggregate_version == 2

    run_id, _, decision_id = identities
    foreign_signal = Phase8IdentifierFactory.signal_id(
        run_id=run_id,
        observation_fingerprint="b" * 64,
        strategy_version="v1",
        side="buy",
        signal_key="foreign",
    )
    mismatched = LedgerEvent.create(
        event_type=EventType.FILL_RECEIVED,
        occurred_at=second.occurred_at,
        recorded_at=second.recorded_at,
        decision_id=decision_id,
        run_id=run_id,
        signal_id=foreign_signal,
        payload={"quantity": 1},
        idempotency_key="foreign-signal",
    )
    with pytest.raises(AggregateIntegrityError):
        ledger.append(mismatched)


def test_event_serialization_round_trip_and_legacy_v1_defaults(event_factory) -> None:
    event = event_factory(payload={"nested": {"items": [1, 2]}})
    restored = LedgerEvent.from_dict(event.to_dict())
    assert restored == event

    legacy = event.to_dict()
    for key in (
        "aggregate_version",
        "source_metadata",
        "integrity_metadata",
        "correlation_id",
    ):
        legacy.pop(key)
    restored_legacy = LedgerEvent.from_dict(legacy)
    assert restored_legacy.aggregate_version == 0
    assert restored_legacy.correlation_id == event.decision_id


def test_complete_frozen_event_family_appends_and_replays(event_factory) -> None:
    ledger = InMemoryEventLedger()
    for expected_version, event_type in enumerate(EventType):
        event = event_factory(
            event_type,
            occurred_offset_ms=expected_version,
            idempotency_key=f"family:{event_type.value}",
            payload={"event": event_type.value},
        )
        result = ledger.append(event, expected_version=expected_version)
        assert result.event.aggregate_version == expected_version + 1

    stream = ledger.read(event.decision_id)
    assert tuple(item.event_type for item in stream) == tuple(EventType)


def test_unknown_event_type_and_schema_version_are_rejected(event_factory) -> None:
    raw = event_factory().to_dict()
    raw["event_type"] = "future_event"
    with pytest.raises(UnknownEventTypeError):
        LedgerEvent.from_dict(raw)

    raw["event_type"] = EventType.DECISION_CREATED.value
    raw["schema_version"] = 99
    with pytest.raises(UnsupportedSchemaVersionError):
        LedgerEvent.from_dict(raw)


def test_ledger_event_is_deeply_immutable(event_factory) -> None:
    event = event_factory(payload={"nested": {"value": 1}})
    with pytest.raises(FrozenInstanceError):
        event.event_id = "evt_" + "0" * 32  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.payload["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        event.payload["nested"]["value"] = 2  # type: ignore[index]


def test_sqlite_migration_and_database_triggers_prevent_overwrite(tmp_path, event_factory) -> None:
    path = tmp_path / "phase8.db"
    ledger = SQLiteEventLedger(path)
    event = ledger.append(event_factory()).event
    ledger.close()

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE phase8_events SET payload_json = '{}' WHERE event_id = ?",
            (event.event_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM phase8_events WHERE event_id = ?", (event.event_id,))
    connection.close()

    reopened = SQLiteEventLedger(path)
    assert reopened.read(event.decision_id) == (event,)
    reopened.close()


def test_newer_sqlite_schema_is_rejected(tmp_path) -> None:
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()

    with pytest.raises(LedgerMigrationError, match="newer than supported"):
        SQLiteEventLedger(path)


def test_concurrent_append_conflict_allows_exactly_one_writer(tmp_path, event_factory) -> None:
    path = tmp_path / "concurrent.db"
    first_ledger = SQLiteEventLedger(path)
    second_ledger = SQLiteEventLedger(path)
    first_event = event_factory(idempotency_key="writer-one", payload={"writer": 1})
    second_event = event_factory(idempotency_key="writer-two", payload={"writer": 2})

    def append(ledger, event):
        try:
            return ledger.append(event, expected_version=0).appended
        except ConcurrentAppendError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: append(*pair),
                [(first_ledger, first_event), (second_ledger, second_event)],
            )
        )

    assert sorted(results) == [False, True]
    assert len(first_ledger.read(first_event.decision_id)) == 1
    first_ledger.close()
    second_ledger.close()
