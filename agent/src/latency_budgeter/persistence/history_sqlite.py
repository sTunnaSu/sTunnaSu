"""SQLite strict-prior latency history with an isolated schema migration."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from src.latency_budgeter.domain.errors import HistoryConflictError, HistoryMigrationError
from src.latency_budgeter.domain.history import (
    HistoryQuery,
    HistoryWindow,
    LatencyComponent,
    LatencySample,
)
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.domain.values import Milliseconds

HISTORY_SCHEMA_NAME = "phase8_latency_history"
HISTORY_SCHEMA_VERSION = 2


class SQLiteLatencyHistoryStore:
    """Durable append-only component history safe to colocate with the event DB."""

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
        """Create a namespaced v1 schema without changing global user_version."""
        with self._lock, self._write_transaction():
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS phase8_schema_versions (
                    schema_name TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL
                )
                """
            )
            row = self._connection.execute(
                "SELECT schema_version FROM phase8_schema_versions WHERE schema_name = ?",
                (HISTORY_SCHEMA_NAME,),
            ).fetchone()
            if row is not None and int(row["schema_version"]) > HISTORY_SCHEMA_VERSION:
                raise HistoryMigrationError(
                    f"history schema {row['schema_version']} is newer than supported {HISTORY_SCHEMA_VERSION}"
                )
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS phase8_latency_samples (
                    sample_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    component TEXT NOT NULL,
                    value_ms TEXT NOT NULL,
                    component_available_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    component_definition_version TEXT NOT NULL,
                    estimator_schema_version TEXT NOT NULL,
                    valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
                    invalid_reason TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    order_id TEXT NOT NULL DEFAULT '',
                    semantic_fingerprint TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_phase8_latency_prior
                    ON phase8_latency_samples(
                        component,
                        component_definition_version,
                        estimator_schema_version,
                        unit,
                        valid,
                        component_available_at DESC,
                        sample_id DESC
                    );
                CREATE TABLE IF NOT EXISTS phase8_latency_invalidations (
                    sample_id TEXT NOT NULL,
                    integrity_event_id TEXT NOT NULL,
                    invalidated_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    PRIMARY KEY(sample_id, integrity_event_id),
                    FOREIGN KEY(sample_id) REFERENCES phase8_latency_samples(sample_id)
                );
                CREATE INDEX IF NOT EXISTS idx_phase8_latency_invalidated
                    ON phase8_latency_invalidations(sample_id, invalidated_at);
                CREATE TRIGGER IF NOT EXISTS phase8_latency_samples_no_update
                BEFORE UPDATE ON phase8_latency_samples
                BEGIN SELECT RAISE(ABORT, 'phase8_latency_samples is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS phase8_latency_samples_no_delete
                BEFORE DELETE ON phase8_latency_samples
                BEGIN SELECT RAISE(ABORT, 'phase8_latency_samples is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS phase8_latency_invalidations_no_update
                BEFORE UPDATE ON phase8_latency_invalidations
                BEGIN SELECT RAISE(ABORT, 'phase8_latency_invalidations is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS phase8_latency_invalidations_no_delete
                BEFORE DELETE ON phase8_latency_invalidations
                BEGIN SELECT RAISE(ABORT, 'phase8_latency_invalidations is append-only'); END;
                """
            )
            if row is None:
                self._connection.execute(
                    "INSERT INTO phase8_schema_versions(schema_name, schema_version) VALUES (?, ?)",
                    (HISTORY_SCHEMA_NAME, HISTORY_SCHEMA_VERSION),
                )
            elif int(row["schema_version"]) == 1:
                columns = {
                    str(column["name"])
                    for column in self._connection.execute(
                        "PRAGMA table_info(phase8_latency_samples)"
                    ).fetchall()
                }
                if "order_id" not in columns:
                    self._connection.execute(
                        "ALTER TABLE phase8_latency_samples "
                        "ADD COLUMN order_id TEXT NOT NULL DEFAULT ''"
                    )
                self._connection.execute(
                    "UPDATE phase8_schema_versions SET schema_version = ? WHERE schema_name = ?",
                    (HISTORY_SCHEMA_VERSION, HISTORY_SCHEMA_NAME),
                )

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
    def _row_to_sample(row: sqlite3.Row) -> LatencySample:
        return LatencySample(
            sample_id=row["sample_id"],
            decision_id=row["decision_id"],
            component=LatencyComponent(row["component"]),
            value_ms=Milliseconds(row["value_ms"]),
            component_available_at=normalize_timestamp(row["component_available_at"]),
            recorded_at=normalize_timestamp(row["recorded_at"]),
            component_definition_version=row["component_definition_version"],
            estimator_schema_version=row["estimator_schema_version"],
            valid=bool(row["valid"]),
            invalid_reason=row["invalid_reason"],
            source_event_id=row["source_event_id"],
            unit=row["unit"],
            order_id=row["order_id"],
        )

    def add(self, sample: LatencySample) -> bool:
        """Append atomically, retaining exact Decimal text and availability."""
        with self._lock, self._write_transaction():
            prior = self._connection.execute(
                "SELECT semantic_fingerprint FROM phase8_latency_samples WHERE sample_id = ?",
                (sample.sample_id,),
            ).fetchone()
            if prior is not None:
                if prior["semantic_fingerprint"] != sample.semantic_fingerprint:
                    raise HistoryConflictError(f"sample_id {sample.sample_id!r} has different content")
                return False
            self._connection.execute(
                """
                INSERT INTO phase8_latency_samples(
                    sample_id, decision_id, component, value_ms,
                    component_available_at, recorded_at,
                    component_definition_version, estimator_schema_version,
                    valid, invalid_reason, source_event_id, unit, order_id,
                    semantic_fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.sample_id,
                    sample.decision_id,
                    sample.component.value,
                    sample.value_ms.canonical(),
                    utc_iso(sample.component_available_at),
                    utc_iso(sample.recorded_at),
                    sample.component_definition_version,
                    sample.estimator_schema_version,
                    int(sample.valid),
                    sample.invalid_reason,
                    sample.source_event_id,
                    sample.unit,
                    sample.order_id,
                    sample.semantic_fingerprint,
                ),
            )
            return True

    def invalidate(
        self,
        sample_id: str,
        *,
        invalidated_at: datetime,
        reason: str,
        integrity_event_id: str,
    ) -> bool:
        """Append a point-in-time invalidation without rewriting the sample."""
        when = normalize_timestamp(invalidated_at)
        if not reason or not integrity_event_id:
            raise ValueError("reason and integrity_event_id are required")
        with self._lock, self._write_transaction():
            if (
                self._connection.execute(
                    "SELECT 1 FROM phase8_latency_samples WHERE sample_id = ?", (sample_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(f"unknown latency sample: {sample_id}")
            prior = self._connection.execute(
                """
                SELECT invalidated_at, reason FROM phase8_latency_invalidations
                WHERE sample_id = ? AND integrity_event_id = ?
                """,
                (sample_id, integrity_event_id),
            ).fetchone()
            if prior is not None:
                if prior["invalidated_at"] != utc_iso(when) or prior["reason"] != reason:
                    raise HistoryConflictError("integrity invalidation identity has different content")
                return False
            self._connection.execute(
                """
                INSERT INTO phase8_latency_invalidations(
                    sample_id, integrity_event_id, invalidated_at, reason
                ) VALUES (?, ?, ?, ?)
                """,
                (sample_id, integrity_event_id, utc_iso(when), reason),
            )
            return True

    def prior_window(self, query: HistoryQuery) -> HistoryWindow:
        """Execute the release-critical strict-prior SQL predicate.

        The SQL excludes current/future/equal-time/incompatible/invalidated
        rows before applying ``LIMIT``.  Ties use ``sample_id DESC``.
        """
        decision_text = utc_iso(query.decision_at)
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT s.* FROM phase8_latency_samples AS s
                WHERE s.component = ?
                  AND s.component_available_at < ?
                  AND s.recorded_at <= ?
                  AND s.decision_id <> ?
                  AND s.valid = 1
                  AND s.unit = ?
                  AND s.component_definition_version = ?
                  AND s.estimator_schema_version = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM phase8_latency_invalidations AS i
                      WHERE i.sample_id = s.sample_id
                        AND i.invalidated_at <= ?
                  )
                ORDER BY s.component_available_at DESC, s.sample_id DESC
                LIMIT ?
                """,
                (
                    query.component.value,
                    decision_text,
                    decision_text,
                    query.current_decision_id,
                    query.unit,
                    query.component_definition_version,
                    query.estimator_schema_version,
                    decision_text,
                    query.rolling_window,
                ),
            ).fetchall()
        return HistoryWindow(tuple(self._row_to_sample(row) for row in rows))

    def close(self) -> None:
        """Close the SQLite connection."""
        with self._lock:
            self._connection.close()
