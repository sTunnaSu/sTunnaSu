"""SQLite append-only Phase 8 event ledger with v1 migration."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator

from src.latency_budgeter.domain.errors import (
    AggregateIntegrityError,
    ConcurrentAppendError,
    IdempotencyConflictError,
    IdentifierCollisionError,
    LedgerMigrationError,
)
from src.latency_budgeter.domain.events import LedgerEvent
from src.latency_budgeter.domain.json_values import canonical_json, thaw_json
from src.latency_budgeter.domain.timestamps import utc_iso
from src.latency_budgeter.ports.ledger import AppendResult

LEDGER_SCHEMA_VERSION = 1


class SQLiteEventLedger:
    """Durable append-only ledger with transactional aggregate sequencing."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(self.database_path), check_same_thread=False, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _migrate(self) -> None:
        """Create or verify the dedicated v1 ledger schema."""
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > LEDGER_SCHEMA_VERSION:
                raise LedgerMigrationError(f"ledger schema {version} is newer than supported {LEDGER_SCHEMA_VERSION}")
            if version == 0:
                self._connection.executescript(
                    """
                    CREATE TABLE phase8_events (
                        event_id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        occurred_at TEXT NOT NULL,
                        recorded_at TEXT NOT NULL,
                        decision_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        signal_id TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        aggregate_version INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        semantic_fingerprint TEXT NOT NULL,
                        causation_id TEXT,
                        correlation_id TEXT NOT NULL,
                        source_metadata_json TEXT NOT NULL,
                        integrity_metadata_json TEXT NOT NULL,
                        UNIQUE(decision_id, aggregate_version)
                    );
                    CREATE INDEX idx_phase8_events_run
                        ON phase8_events(run_id, recorded_at, decision_id, aggregate_version);
                    CREATE TRIGGER phase8_events_no_update
                    BEFORE UPDATE ON phase8_events
                    BEGIN
                        SELECT RAISE(ABORT, 'phase8_events is append-only');
                    END;
                    CREATE TRIGGER phase8_events_no_delete
                    BEFORE DELETE ON phase8_events
                    BEGIN
                        SELECT RAISE(ABORT, 'phase8_events is append-only');
                    END;
                    PRAGMA user_version=1;
                    """
                )
                self._connection.commit()
            required_table = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'phase8_events'"
            ).fetchone()
            if required_table is None:
                raise LedgerMigrationError("ledger schema version is set but phase8_events is missing")

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> LedgerEvent:
        return LedgerEvent.from_dict(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "recorded_at": row["recorded_at"],
                "decision_id": row["decision_id"],
                "run_id": row["run_id"],
                "signal_id": row["signal_id"],
                "schema_version": row["schema_version"],
                "aggregate_version": row["aggregate_version"],
                "payload": json.loads(row["payload_json"]),
                "idempotency_key": row["idempotency_key"],
                "causation_id": row["causation_id"],
                "correlation_id": row["correlation_id"],
                "source_metadata": json.loads(row["source_metadata_json"]),
                "integrity_metadata": json.loads(row["integrity_metadata_json"]),
            }
        )

    def append(
        self,
        event: LedgerEvent,
        *,
        expected_version: int | None = None,
    ) -> AppendResult:
        """Append atomically; retries are content-checked and idempotent."""
        with self._lock, self._write_transaction():
            idempotent_row = self._connection.execute(
                "SELECT * FROM phase8_events WHERE idempotency_key = ?",
                (event.idempotency_key,),
            ).fetchone()
            if idempotent_row is not None:
                prior = self._row_to_event(idempotent_row)
                if idempotent_row["semantic_fingerprint"] != event.semantic_fingerprint:
                    raise IdempotencyConflictError(f"idempotency key {event.idempotency_key!r} has different content")
                return AppendResult(prior, appended=False)

            event_id_row = self._connection.execute(
                "SELECT * FROM phase8_events WHERE event_id = ?", (event.event_id,)
            ).fetchone()
            if event_id_row is not None:
                prior = self._row_to_event(event_id_row)
                if (
                    event_id_row["idempotency_key"] != event.idempotency_key
                    or event_id_row["semantic_fingerprint"] != event.semantic_fingerprint
                ):
                    raise IdentifierCollisionError(f"event_id {event.event_id!r} already identifies different content")
                return AppendResult(prior, appended=False)

            identity_row = self._connection.execute(
                """
                SELECT run_id, signal_id, aggregate_version AS current_version
                FROM phase8_events WHERE decision_id = ?
                ORDER BY aggregate_version DESC LIMIT 1
                """,
                (event.decision_id,),
            ).fetchone()
            current_version = int(identity_row["current_version"]) if identity_row else 0
            if expected_version is not None and expected_version != current_version:
                raise ConcurrentAppendError(f"expected aggregate version {expected_version}, found {current_version}")
            if identity_row is not None and (
                identity_row["run_id"] != event.run_id or identity_row["signal_id"] != event.signal_id
            ):
                raise AggregateIntegrityError("run_id and signal_id cannot change within a decision aggregate")
            stored = replace(event, aggregate_version=current_version + 1)
            try:
                self._connection.execute(
                    """
                    INSERT INTO phase8_events (
                        event_id, event_type, occurred_at, recorded_at,
                        decision_id, run_id, signal_id, schema_version,
                        aggregate_version, payload_json, idempotency_key,
                        semantic_fingerprint, causation_id, correlation_id,
                        source_metadata_json, integrity_metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.event_id,
                        stored.event_type.value,
                        utc_iso(stored.occurred_at),
                        utc_iso(stored.recorded_at),
                        stored.decision_id,
                        stored.run_id,
                        stored.signal_id,
                        stored.schema_version,
                        stored.aggregate_version,
                        canonical_json(thaw_json(stored.payload)),
                        stored.idempotency_key,
                        stored.semantic_fingerprint,
                        stored.causation_id,
                        stored.correlation_id,
                        canonical_json(thaw_json(stored.source_metadata)),
                        canonical_json(thaw_json(stored.integrity_metadata)),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConcurrentAppendError("concurrent append violated ledger uniqueness") from exc
            return AppendResult(stored, appended=True)

    def read(self, decision_id: str) -> tuple[LedgerEvent, ...]:
        """Read one decision aggregate in immutable sequence order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM phase8_events
                WHERE decision_id = ? ORDER BY aggregate_version ASC
                """,
                (decision_id,),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def read_run(self, run_id: str) -> tuple[LedgerEvent, ...]:
        """Read one run in deterministic recorded/aggregate order."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM phase8_events WHERE run_id = ?
                ORDER BY recorded_at, decision_id, aggregate_version
                """,
                (run_id,),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            self._connection.close()
