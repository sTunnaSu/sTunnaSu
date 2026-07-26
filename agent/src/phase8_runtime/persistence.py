"""Crash-safe runtime projections plus an append-only Phase 8 audit chain."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping

from src.phase8_runtime.models import (
    IntentState,
    OrderIntent,
    RuntimeMode,
    StrategySpecification,
    canonical_hash,
)
from src.security.secret_redaction import redact_sensitive_text


class RuntimePersistenceError(RuntimeError):
    """Raised when durable runtime evidence is contradictory or corrupt."""


_REDACTED = "[REDACTED]"
_SECRET_PARTS = ("secret", "token", "password", "authorization", "api_key", "private_key")


def _utc_iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("runtime timestamps must be timezone-aware")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if any(part in key_text.lower() for part in _SECRET_PARTS):
                result[key_text] = _REDACTED
            else:
                result[key_text] = _redact(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return _utc_iso(value)
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _json(value: Any) -> str:
    return json.dumps(_redact(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


_TRANSITIONS: Mapping[IntentState, frozenset[IntentState]] = {
    IntentState.INTENT_CREATED: frozenset({IntentState.VALIDATED, IntentState.REJECTED, IntentState.EXPIRED}),
    IntentState.VALIDATED: frozenset(
        {IntentState.DRY_RUN, IntentState.SUBMITTING, IntentState.REJECTED, IntentState.EXPIRED}
    ),
    IntentState.SUBMITTING: frozenset(
        {
            IntentState.ACKNOWLEDGED,
            IntentState.PARTIALLY_FILLED,
            IntentState.FILLED,
            IntentState.REJECTED,
            IntentState.AMBIGUOUS,
            IntentState.RECONCILIATION_REQUIRED,
        }
    ),
    IntentState.ACKNOWLEDGED: frozenset(
        {
            IntentState.PARTIALLY_FILLED,
            IntentState.FILLED,
            IntentState.CANCEL_PENDING,
            IntentState.CANCELLED,
            IntentState.REJECTED,
            IntentState.EXPIRED,
            IntentState.RECONCILIATION_REQUIRED,
        }
    ),
    IntentState.PARTIALLY_FILLED: frozenset(
        {
            IntentState.FILLED,
            IntentState.CANCEL_PENDING,
            IntentState.CANCELLED,
            IntentState.EXPIRED,
            IntentState.RECONCILIATION_REQUIRED,
        }
    ),
    IntentState.CANCEL_PENDING: frozenset(
        {IntentState.CANCELLED, IntentState.PARTIALLY_FILLED, IntentState.FILLED, IntentState.AMBIGUOUS}
    ),
    IntentState.AMBIGUOUS: frozenset(
        {
            IntentState.ACKNOWLEDGED,
            IntentState.PARTIALLY_FILLED,
            IntentState.FILLED,
            IntentState.REJECTED,
            IntentState.CANCELLED,
            IntentState.RECONCILIATION_REQUIRED,
        }
    ),
    IntentState.RECONCILIATION_REQUIRED: frozenset(
        {
            IntentState.ACKNOWLEDGED,
            IntentState.PARTIALLY_FILLED,
            IntentState.FILLED,
            IntentState.REJECTED,
            IntentState.CANCELLED,
            IntentState.CLOSED,
        }
    ),
    IntentState.FILLED: frozenset({IntentState.CLOSED}),
    IntentState.DRY_RUN: frozenset({IntentState.CLOSED}),
    IntentState.CANCELLED: frozenset({IntentState.CLOSED}),
    IntentState.REJECTED: frozenset({IntentState.CLOSED}),
    IntentState.EXPIRED: frozenset({IntentState.CLOSED}),
    IntentState.CLOSED: frozenset(),
}


class Phase8RuntimeStore:
    """SQLite state store with immutable event history and mutable projections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS phase8_runtime_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO phase8_runtime_schema(component, version)
                VALUES ('runtime', 1);

                CREATE TABLE IF NOT EXISTS phase8_runtime_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    monotonic_ns INTEGER,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS phase8_runtime_events_no_update
                BEFORE UPDATE ON phase8_runtime_events
                BEGIN SELECT RAISE(ABORT, 'phase8 runtime events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS phase8_runtime_events_no_delete
                BEFORE DELETE ON phase8_runtime_events
                BEGIN SELECT RAISE(ABORT, 'phase8 runtime events are append-only'); END;

                CREATE TABLE IF NOT EXISTS phase8_runtime_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    code_revision TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    safety_halt_reason TEXT
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_strategies (
                    strategy_key TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    specification_hash TEXT NOT NULL,
                    specification_json TEXT NOT NULL,
                    parent_ids_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_strategy_controls (
                    strategy_key TEXT PRIMARY KEY,
                    operational_state TEXT NOT NULL,
                    reason TEXT,
                    evidence_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_intents (
                    intent_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT UNIQUE,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    quantity TEXT NOT NULL,
                    state TEXT NOT NULL,
                    broker_order_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_phase8_intents_run
                    ON phase8_runtime_intents(run_id, state);
                CREATE INDEX IF NOT EXISTS idx_phase8_intents_symbol
                    ON phase8_runtime_intents(symbol, state);

                CREATE TABLE IF NOT EXISTS phase8_runtime_allocations (
                    allocation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL DEFAULT '',
                    strategy_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_intent_id TEXT NOT NULL UNIQUE,
                    quantity TEXT NOT NULL,
                    remaining_quantity TEXT NOT NULL,
                    average_fill_price TEXT NOT NULL,
                    stop_price TEXT,
                    target_price TEXT,
                    highest_price TEXT,
                    opened_at TEXT NOT NULL,
                    holding_deadline TEXT NOT NULL,
                    state TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL DEFAULT '0',
                    exit_intent_id TEXT
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_intent_fills (
                    intent_id TEXT PRIMARY KEY,
                    allocation_id TEXT NOT NULL,
                    cumulative_quantity TEXT NOT NULL,
                    average_fill_price TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_shadow_trades (
                    trade_id TEXT PRIMARY KEY,
                    strategy_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    closed_at TEXT NOT NULL,
                    gross_pnl TEXT NOT NULL,
                    costs TEXT NOT NULL,
                    net_pnl TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    UNIQUE(strategy_key, signal_id)
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_shadow_positions (
                    shadow_position_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    strategy_key TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    entry_signal_id TEXT NOT NULL UNIQUE,
                    quantity TEXT NOT NULL,
                    entry_decision_price TEXT NOT NULL,
                    entry_fill_price TEXT NOT NULL,
                    entry_fee TEXT NOT NULL,
                    stop_price TEXT NOT NULL,
                    target_price TEXT NOT NULL,
                    highest_price TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    holding_deadline TEXT NOT NULL,
                    state TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS phase8_runtime_equity (
                    snapshot_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    attributable_equity TEXT NOT NULL,
                    drawdown_fraction TEXT NOT NULL,
                    gross_exposure TEXT NOT NULL
                );
                """
            )
            allocation_columns = {
                str(row["name"])
                for row in self._conn.execute("PRAGMA table_info(phase8_runtime_allocations)").fetchall()
            }
            if "session_id" not in allocation_columns:
                self._conn.execute(
                    "ALTER TABLE phase8_runtime_allocations ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
                )
            version = self._conn.execute(
                "SELECT version FROM phase8_runtime_schema WHERE component='runtime'"
            ).fetchone()
            if version is None or int(version[0]) != 1:
                raise RuntimePersistenceError("unsupported Phase 8 runtime database schema")
            self._conn.commit()

    @contextmanager
    def _write(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def append_event(
        self,
        *,
        run_id: str,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        occurred_at: datetime | None = None,
        monotonic_ns: int | None = None,
    ) -> str:
        payload_json = _json(payload)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        occurred = _utc_iso(occurred_at)
        semantic = _json(
            {
                "run_id": run_id,
                "session_id": session_id,
                "event_type": event_type,
                "occurred_at": occurred,
                "payload_hash": payload_hash,
            }
        )
        with self._write():
            existing = self._conn.execute(
                "SELECT event_id, event_type, run_id, session_id, payload_hash "
                "FROM phase8_runtime_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["event_type"] != event_type
                    or existing["run_id"] != run_id
                    or existing["session_id"] != session_id
                    or existing["payload_hash"] != payload_hash
                ):
                    raise RuntimePersistenceError(f"conflicting retry for runtime event {idempotency_key!r}")
                return str(existing["event_id"])
            previous = self._conn.execute(
                "SELECT event_hash FROM phase8_runtime_events ORDER BY sequence DESC LIMIT 1"
            ).fetchone()
            previous_hash = str(previous[0]) if previous is not None else "GENESIS"
            event_hash = hashlib.sha256((previous_hash + semantic).encode("utf-8")).hexdigest()
            event_id = f"p8evt_{uuid.uuid4().hex}"
            self._conn.execute(
                """
                INSERT INTO phase8_runtime_events(
                    event_id,idempotency_key,run_id,session_id,event_type,occurred_at,
                    monotonic_ns,payload_json,payload_hash,previous_hash,event_hash
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    idempotency_key,
                    run_id,
                    session_id,
                    event_type,
                    occurred,
                    monotonic_ns,
                    payload_json,
                    payload_hash,
                    previous_hash,
                    event_hash,
                ),
            )
            return event_id

    def start_run(
        self,
        *,
        run_id: str,
        session_id: str,
        mode: RuntimeMode,
        config_hash: str,
        code_revision: str,
        started_at: datetime,
    ) -> None:
        with self._write():
            existing = self._conn.execute(
                "SELECT session_id,mode,config_hash,code_revision FROM phase8_runtime_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            expected = (session_id, mode.value, config_hash, code_revision)
            if existing is not None:
                actual = tuple(existing)
                if actual != expected:
                    raise RuntimePersistenceError("run identity was reused with different configuration")
                return
            self._conn.execute(
                "INSERT INTO phase8_runtime_runs VALUES(?,?,?,?,?,'running',?,NULL,NULL)",
                (run_id, session_id, mode.value, config_hash, code_revision, _utc_iso(started_at)),
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        finished_at: datetime,
        safety_halt_reason: str | None = None,
    ) -> None:
        with self._write():
            cursor = self._conn.execute(
                "UPDATE phase8_runtime_runs SET status=?,finished_at=?,safety_halt_reason=? WHERE run_id=?",
                (status, _utc_iso(finished_at), safety_halt_reason, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimePersistenceError(f"unknown run {run_id}")

    def register_strategy(self, strategy: StrategySpecification, *, registered_at: datetime) -> None:
        raw = _json(strategy.model_dump(mode="json"))
        with self._write():
            existing = self._conn.execute(
                "SELECT specification_hash FROM phase8_runtime_strategies WHERE strategy_key=?",
                (strategy.key,),
            ).fetchone()
            if existing is not None:
                if existing["specification_hash"] != strategy.fingerprint:
                    raise RuntimePersistenceError(f"immutable strategy version {strategy.key} was mutated")
                self._conn.execute(
                    "INSERT OR IGNORE INTO phase8_runtime_strategy_controls VALUES(?,'active',NULL,'{}',?)",
                    (strategy.key, _utc_iso(registered_at)),
                )
                return
            self._conn.execute(
                """
                INSERT INTO phase8_runtime_strategies(
                    strategy_key,strategy_id,version,state,specification_hash,
                    specification_json,parent_ids_json,registered_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    strategy.key,
                    strategy.strategy_id,
                    strategy.version,
                    strategy.state.value,
                    strategy.fingerprint,
                    raw,
                    _json(strategy.parent_ids),
                    _utc_iso(registered_at),
                ),
            )
            self._conn.execute(
                "INSERT INTO phase8_runtime_strategy_controls VALUES(?,'active',NULL,'{}',?)",
                (strategy.key, _utc_iso(registered_at)),
            )

    def strategy(self, strategy_key: str) -> StrategySpecification | None:
        row = self._conn.execute(
            "SELECT specification_json FROM phase8_runtime_strategies WHERE strategy_key=?",
            (strategy_key,),
        ).fetchone()
        if row is None:
            return None
        return StrategySpecification.model_validate(json.loads(row["specification_json"]))

    def strategies(self, *, state: str | None = None) -> list[StrategySpecification]:
        if state is None:
            rows = self._conn.execute(
                "SELECT specification_json FROM phase8_runtime_strategies ORDER BY strategy_key"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT specification_json FROM phase8_runtime_strategies WHERE state=? ORDER BY strategy_key",
                (state,),
            ).fetchall()
        return [StrategySpecification.model_validate(json.loads(row["specification_json"])) for row in rows]

    def strategy_operational_state(self, strategy_key: str) -> str:
        row = self._conn.execute(
            "SELECT operational_state FROM phase8_runtime_strategy_controls WHERE strategy_key=?",
            (strategy_key,),
        ).fetchone()
        return str(row["operational_state"]) if row is not None else "unregistered"

    def disable_strategy(
        self,
        strategy_key: str,
        *,
        reason: str,
        evidence: Mapping[str, Any],
        at: datetime,
    ) -> None:
        with self._write():
            row = self._conn.execute(
                "SELECT operational_state FROM phase8_runtime_strategy_controls WHERE strategy_key=?",
                (strategy_key,),
            ).fetchone()
            if row is None:
                raise RuntimePersistenceError(f"unknown strategy {strategy_key}")
            if row["operational_state"] == "disabled":
                return
            self._conn.execute(
                "UPDATE phase8_runtime_strategy_controls SET operational_state='disabled',"
                "reason=?,evidence_json=?,updated_at=? WHERE strategy_key=?",
                (reason, _json(evidence), _utc_iso(at), strategy_key),
            )

    def create_intent(self, intent: OrderIntent) -> bool:
        payload = _json(intent.model_dump(mode="json"))
        with self._write():
            existing = self._conn.execute(
                "SELECT payload_json FROM phase8_runtime_intents WHERE intent_id=? OR client_order_id=?",
                (intent.intent_id, intent.client_order_id),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload:
                    raise RuntimePersistenceError("duplicate order identity has different content")
                return False
            self._conn.execute(
                """
                INSERT INTO phase8_runtime_intents(
                    intent_id,client_order_id,decision_id,run_id,session_id,strategy_key,
                    signal_id,symbol,side,kind,quantity,state,broker_order_id,payload_json,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
                """,
                (
                    intent.intent_id,
                    intent.client_order_id,
                    intent.decision_id,
                    intent.run_id,
                    intent.session_id,
                    f"{intent.strategy_id}:{intent.strategy_version}",
                    intent.signal_id,
                    intent.symbol,
                    intent.side,
                    intent.kind.value,
                    str(intent.quantity),
                    IntentState.INTENT_CREATED.value,
                    payload,
                    _utc_iso(intent.created_at),
                    _utc_iso(intent.created_at),
                ),
            )
            return True

    def transition_intent(
        self,
        intent_id: str,
        target: IntentState,
        *,
        at: datetime,
        decision_id: str | None = None,
        broker_order_id: str | None = None,
    ) -> None:
        with self._write():
            row = self._conn.execute(
                "SELECT state,decision_id,broker_order_id FROM phase8_runtime_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise RuntimePersistenceError(f"unknown intent {intent_id}")
            current = IntentState(row["state"])
            if current is target:
                if decision_id and row["decision_id"] not in (None, decision_id):
                    raise RuntimePersistenceError("intent decision identity conflict")
                if broker_order_id and row["broker_order_id"] not in (None, broker_order_id):
                    raise RuntimePersistenceError("intent broker identity conflict")
                return
            if target not in _TRANSITIONS[current]:
                raise RuntimePersistenceError(f"invalid intent transition {current.value} -> {target.value}")
            if decision_id and row["decision_id"] not in (None, decision_id):
                raise RuntimePersistenceError("intent decision identity conflict")
            if broker_order_id and row["broker_order_id"] not in (None, broker_order_id):
                raise RuntimePersistenceError("intent broker identity conflict")
            self._conn.execute(
                "UPDATE phase8_runtime_intents SET state=?,decision_id=COALESCE(decision_id,?),"
                "broker_order_id=COALESCE(broker_order_id,?),updated_at=? WHERE intent_id=?",
                (target.value, decision_id, broker_order_id, _utc_iso(at), intent_id),
            )

    def unresolved_intents(self) -> list[dict[str, Any]]:
        terminal = (
            IntentState.DRY_RUN.value,
            IntentState.CANCELLED.value,
            IntentState.REJECTED.value,
            IntentState.EXPIRED.value,
            IntentState.CLOSED.value,
        )
        rows = self._conn.execute(
            "SELECT * FROM phase8_runtime_intents WHERE state NOT IN (?,?,?,?,?) ORDER BY created_at",
            terminal,
        ).fetchall()
        return [dict(row) for row in rows]

    def intent_by_client_order_id(self, client_order_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_intents WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def intent_for_signal(
        self,
        *,
        session_id: str,
        strategy_key: str,
        signal_id: str,
        kind: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_intents WHERE session_id=? AND strategy_key=? "
            "AND signal_id=? AND kind=? ORDER BY created_at LIMIT 1",
            (session_id, strategy_key, signal_id, kind),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def intents(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            rows = self._conn.execute("SELECT * FROM phase8_runtime_intents ORDER BY created_at").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM phase8_runtime_intents WHERE session_id=? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def intent_fills(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return cumulative broker-fill projections with their intent identity.

        Fill rows deliberately retain the broker's cumulative quantity and
        average price rather than fabricating individual executions.  Joining
        the immutable intent identity here keeps reporting code out of the
        persistence implementation details.
        """
        query = (
            "SELECT f.*,i.decision_id,i.broker_order_id,i.strategy_key,i.symbol,"
            "i.side,i.kind,i.quantity AS requested_quantity "
            "FROM phase8_runtime_intent_fills AS f "
            "JOIN phase8_runtime_intents AS i ON i.intent_id=f.intent_id"
        )
        parameters: tuple[Any, ...] = ()
        if session_id is not None:
            query += " WHERE i.session_id=?"
            parameters = (session_id,)
        query += " ORDER BY f.updated_at,f.intent_id"
        return [dict(row) for row in self._conn.execute(query, parameters).fetchall()]

    def intent_counts(self, session_id: str) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT kind,state,CASE WHEN decision_id IS NULL THEN 0 ELSE 1 END AS broker_attempt,"
            "COUNT(*) AS n FROM phase8_runtime_intents "
            "WHERE session_id=? GROUP BY kind,state,broker_attempt",
            (session_id,),
        ).fetchall()
        counts = {"entry": 0, "exit": 0, "submitted": 0, "unresolved": 0}
        terminal = {
            IntentState.INTENT_CREATED.value,
            IntentState.VALIDATED.value,
            IntentState.DRY_RUN.value,
            IntentState.REJECTED.value,
            IntentState.EXPIRED.value,
            IntentState.CLOSED.value,
        }
        for row in rows:
            count = int(row["n"])
            broker_attempt = bool(row["broker_attempt"])
            if broker_attempt:
                if row["kind"] == "entry":
                    counts["entry"] += count
                else:
                    counts["exit"] += count
            if row["state"] not in {
                IntentState.INTENT_CREATED.value,
                IntentState.VALIDATED.value,
                IntentState.DRY_RUN.value,
                IntentState.REJECTED.value,
                IntentState.EXPIRED.value,
            }:
                counts["submitted"] += count
            if row["state"] not in terminal:
                counts["unresolved"] += count
        return counts

    def add_allocation(
        self,
        *,
        session_id: str = "",
        strategy_key: str,
        symbol: str,
        entry_intent_id: str,
        quantity: Decimal,
        average_fill_price: Decimal,
        stop_price: Decimal | None,
        target_price: Decimal | None,
        opened_at: datetime,
        holding_deadline: datetime,
    ) -> str:
        allocation_id = (
            "alloc_" + canonical_hash({"strategy": strategy_key, "symbol": symbol, "entry": entry_intent_id})[:24]
        )
        with self._write():
            existing = self._conn.execute(
                "SELECT allocation_id,quantity,average_fill_price FROM phase8_runtime_allocations "
                "WHERE entry_intent_id=?",
                (entry_intent_id,),
            ).fetchone()
            if existing is not None:
                if existing["quantity"] != str(quantity) or existing["average_fill_price"] != str(average_fill_price):
                    raise RuntimePersistenceError("allocation retry changed fill evidence")
                return str(existing["allocation_id"])
            self._conn.execute(
                """
                INSERT INTO phase8_runtime_allocations(
                    allocation_id,session_id,strategy_key,symbol,entry_intent_id,
                    quantity,remaining_quantity,average_fill_price,stop_price,target_price,
                    highest_price,opened_at,holding_deadline,state,realized_pnl,exit_intent_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open','0',NULL)
                """,
                (
                    allocation_id,
                    session_id,
                    strategy_key,
                    symbol,
                    entry_intent_id,
                    str(quantity),
                    str(quantity),
                    str(average_fill_price),
                    str(stop_price) if stop_price is not None else None,
                    str(target_price) if target_price is not None else None,
                    str(average_fill_price),
                    _utc_iso(opened_at),
                    _utc_iso(holding_deadline),
                ),
            )
            return allocation_id

    def open_allocations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        if session_id is None:
            query = "SELECT * FROM phase8_runtime_allocations WHERE state='open' ORDER BY opened_at"
            parameters: tuple[Any, ...] = ()
        else:
            query = "SELECT * FROM phase8_runtime_allocations WHERE state='open' AND session_id=? ORDER BY opened_at"
            parameters = (session_id,)
        return [dict(row) for row in self._conn.execute(query, parameters).fetchall()]

    def allocations(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Return all owned Phase 8 allocations for auditable reporting."""
        if session_id is None:
            query = "SELECT * FROM phase8_runtime_allocations ORDER BY opened_at,allocation_id"
            parameters: tuple[Any, ...] = ()
        else:
            query = "SELECT * FROM phase8_runtime_allocations WHERE session_id=? ORDER BY opened_at,allocation_id"
            parameters = (session_id,)
        return [dict(row) for row in self._conn.execute(query, parameters).fetchall()]

    def allocation_for_entry(self, entry_intent_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_allocations WHERE entry_intent_id=?",
            (entry_intent_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def allocation(self, allocation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_allocations WHERE allocation_id=?",
            (allocation_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def update_allocation_high(self, allocation_id: str, price: Decimal) -> None:
        with self._write():
            row = self._conn.execute(
                "SELECT highest_price,state FROM phase8_runtime_allocations WHERE allocation_id=?",
                (allocation_id,),
            ).fetchone()
            if row is None or row["state"] != "open":
                return
            if Decimal(str(row["highest_price"])) < price:
                self._conn.execute(
                    "UPDATE phase8_runtime_allocations SET highest_price=? WHERE allocation_id=?",
                    (str(price), allocation_id),
                )

    def close_allocation(
        self,
        allocation_id: str,
        *,
        exit_intent_id: str,
        quantity: Decimal,
        exit_price: Decimal,
    ) -> Decimal:
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM phase8_runtime_allocations WHERE allocation_id=?",
                (allocation_id,),
            ).fetchone()
            if row is None:
                raise RuntimePersistenceError(f"unknown allocation {allocation_id}")
            remaining = Decimal(str(row["remaining_quantity"]))
            if row["exit_intent_id"] == exit_intent_id and remaining == 0:
                return Decimal("0")
            if quantity <= 0 or quantity > remaining:
                raise RuntimePersistenceError("exit quantity exceeds owned allocation")
            pnl = (exit_price - Decimal(str(row["average_fill_price"]))) * quantity
            new_remaining = remaining - quantity
            prior_realized = Decimal(str(row["realized_pnl"]))
            state = "closed" if new_remaining == 0 else "open"
            self._conn.execute(
                "UPDATE phase8_runtime_allocations SET remaining_quantity=?,state=?,"
                "realized_pnl=?,exit_intent_id=? WHERE allocation_id=?",
                (
                    str(new_remaining),
                    state,
                    str(prior_realized + pnl),
                    exit_intent_id,
                    allocation_id,
                ),
            )
            return pnl

    def apply_cumulative_fill(
        self,
        *,
        intent: OrderIntent,
        cumulative_quantity: Decimal,
        average_fill_price: Decimal,
        observed_at: datetime,
    ) -> dict[str, Any]:
        """Apply a broker's cumulative fill exactly once and return the owned allocation.

        Alpaca order snapshots report cumulative quantity and average price.  This
        projection converts repeated snapshots into a delta before changing owned
        position quantity, preventing duplicate callbacks from double counting.
        """
        if cumulative_quantity <= 0 or average_fill_price <= 0:
            raise RuntimePersistenceError("fill quantity and price must be positive")
        with self._write():
            prior = self._conn.execute(
                "SELECT * FROM phase8_runtime_intent_fills WHERE intent_id=?",
                (intent.intent_id,),
            ).fetchone()
            prior_quantity = Decimal(str(prior["cumulative_quantity"])) if prior is not None else Decimal("0")
            prior_average = Decimal(str(prior["average_fill_price"])) if prior is not None else Decimal("0")
            if cumulative_quantity < prior_quantity:
                raise RuntimePersistenceError("cumulative fill quantity regressed")
            if cumulative_quantity == prior_quantity:
                if prior is None or average_fill_price != prior_average:
                    raise RuntimePersistenceError("duplicate fill changed average price")
                allocation = self._conn.execute(
                    "SELECT * FROM phase8_runtime_allocations WHERE allocation_id=?",
                    (str(prior["allocation_id"]),),
                ).fetchone()
                if allocation is None:
                    raise RuntimePersistenceError("fill projection references a missing allocation")
                return dict(allocation)

            delta = cumulative_quantity - prior_quantity
            if intent.kind.value == "entry":
                allocation_id = (
                    "alloc_"
                    + canonical_hash(
                        {
                            "strategy": f"{intent.strategy_id}:{intent.strategy_version}",
                            "symbol": intent.symbol,
                            "entry": intent.intent_id,
                        }
                    )[:24]
                )
                allocation = self._conn.execute(
                    "SELECT * FROM phase8_runtime_allocations WHERE entry_intent_id=?",
                    (intent.intent_id,),
                ).fetchone()
                if allocation is None:
                    if (
                        intent.proposed_stop is None
                        or intent.proposed_target is None
                        or intent.holding_deadline is None
                    ):
                        raise RuntimePersistenceError("entry intent lacks frozen exit ownership")
                    self._conn.execute(
                        """
                        INSERT INTO phase8_runtime_allocations(
                            allocation_id,session_id,strategy_key,symbol,entry_intent_id,
                            quantity,remaining_quantity,average_fill_price,stop_price,target_price,
                            highest_price,opened_at,holding_deadline,state,realized_pnl,exit_intent_id
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open','0',NULL)
                        """,
                        (
                            allocation_id,
                            intent.session_id,
                            f"{intent.strategy_id}:{intent.strategy_version}",
                            intent.symbol,
                            intent.intent_id,
                            str(cumulative_quantity),
                            str(cumulative_quantity),
                            str(average_fill_price),
                            str(intent.proposed_stop),
                            str(intent.proposed_target),
                            str(average_fill_price),
                            _utc_iso(observed_at),
                            _utc_iso(intent.holding_deadline),
                        ),
                    )
                else:
                    remaining = Decimal(str(allocation["remaining_quantity"])) + delta
                    self._conn.execute(
                        "UPDATE phase8_runtime_allocations SET quantity=?,remaining_quantity=?,"
                        "average_fill_price=? WHERE allocation_id=?",
                        (
                            str(cumulative_quantity),
                            str(remaining),
                            str(average_fill_price),
                            allocation_id,
                        ),
                    )
            else:
                if not intent.allocation_id:
                    raise RuntimePersistenceError("exit intent lacks allocation ownership")
                allocation_id = intent.allocation_id
                allocation = self._conn.execute(
                    "SELECT * FROM phase8_runtime_allocations WHERE allocation_id=?",
                    (allocation_id,),
                ).fetchone()
                if allocation is None:
                    raise RuntimePersistenceError(f"unknown allocation {allocation_id}")
                remaining = Decimal(str(allocation["remaining_quantity"]))
                if delta > remaining:
                    raise RuntimePersistenceError("exit fill exceeds owned allocation")
                incremental_notional = average_fill_price * cumulative_quantity - prior_average * prior_quantity
                incremental_price = incremental_notional / delta
                realized_delta = (incremental_price - Decimal(str(allocation["average_fill_price"]))) * delta
                new_remaining = remaining - delta
                new_state = "closed" if new_remaining == 0 else "open"
                prior_realized = Decimal(str(allocation["realized_pnl"]))
                self._conn.execute(
                    "UPDATE phase8_runtime_allocations SET remaining_quantity=?,state=?,"
                    "realized_pnl=?,exit_intent_id=? WHERE allocation_id=?",
                    (
                        str(new_remaining),
                        new_state,
                        str(prior_realized + realized_delta),
                        intent.intent_id,
                        allocation_id,
                    ),
                )

            self._conn.execute(
                """
                INSERT INTO phase8_runtime_intent_fills(
                    intent_id,allocation_id,cumulative_quantity,average_fill_price,updated_at
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(intent_id) DO UPDATE SET
                    cumulative_quantity=excluded.cumulative_quantity,
                    average_fill_price=excluded.average_fill_price,
                    updated_at=excluded.updated_at
                """,
                (
                    intent.intent_id,
                    allocation_id,
                    str(cumulative_quantity),
                    str(average_fill_price),
                    _utc_iso(observed_at),
                ),
            )
            result = self._conn.execute(
                "SELECT * FROM phase8_runtime_allocations WHERE allocation_id=?",
                (allocation_id,),
            ).fetchone()
            if result is None:
                raise RuntimePersistenceError("fill projection did not produce an allocation")
            return dict(result)

    def record_equity(
        self,
        *,
        run_id: str,
        session_id: str,
        observed_at: datetime,
        initial_capital: Decimal,
        unrealized_pnl: Decimal,
        gross_exposure: Decimal,
    ) -> dict[str, Decimal]:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)),0) AS pnl "
            "FROM phase8_runtime_allocations WHERE session_id=?",
            (session_id,),
        ).fetchone()
        realized = Decimal(str(row["pnl"] if row is not None else 0))
        equity = initial_capital + realized + unrealized_pnl
        peak_row = self._conn.execute(
            "SELECT MAX(CAST(attributable_equity AS REAL)) AS peak FROM phase8_runtime_equity WHERE session_id=?",
            (session_id,),
        ).fetchone()
        prior_peak = Decimal(str(peak_row["peak"])) if peak_row and peak_row["peak"] is not None else equity
        peak = max(prior_peak, equity)
        drawdown = (peak - equity) / peak if peak > 0 else Decimal("1")
        snapshot_id = "eq_" + canonical_hash({"run": run_id, "at": _utc_iso(observed_at), "equity": str(equity)})[:24]
        with self._write():
            self._conn.execute(
                "INSERT OR IGNORE INTO phase8_runtime_equity VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    snapshot_id,
                    run_id,
                    session_id,
                    _utc_iso(observed_at),
                    str(realized),
                    str(unrealized_pnl),
                    str(equity),
                    str(drawdown),
                    str(gross_exposure),
                ),
            )
        return {
            "realized_pnl": realized,
            "unrealized_pnl": unrealized_pnl,
            "equity": equity,
            "drawdown_fraction": drawdown,
            "gross_exposure": gross_exposure,
        }

    def latest_equity(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_equity WHERE session_id=? ORDER BY observed_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    def equity_snapshots(self, session_id: str) -> list[dict[str, Any]]:
        """Return the attributable Phase 8 equity history in causal order."""
        rows = self._conn.execute(
            "SELECT * FROM phase8_runtime_equity WHERE session_id=? ORDER BY observed_at,snapshot_id",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def completed_round_trips(
        self,
        session_id: str,
        *,
        strategy_key: str | None = None,
    ) -> int:
        if strategy_key is None:
            query = "SELECT COUNT(*) AS n FROM phase8_runtime_allocations WHERE session_id=? AND state='closed'"
            parameters: tuple[Any, ...] = (session_id,)
        else:
            query = (
                "SELECT COUNT(*) AS n FROM phase8_runtime_allocations "
                "WHERE session_id=? AND strategy_key=? AND state='closed'"
            )
            parameters = (session_id, strategy_key)
        row = self._conn.execute(query, parameters).fetchone()
        return int(row["n"] if row is not None else 0)

    def realized_pnl(
        self,
        session_id: str,
        *,
        strategy_key: str | None = None,
    ) -> Decimal:
        if strategy_key is None:
            query = (
                "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)),0) AS pnl "
                "FROM phase8_runtime_allocations WHERE session_id=?"
            )
            parameters: tuple[Any, ...] = (session_id,)
        else:
            query = (
                "SELECT COALESCE(SUM(CAST(realized_pnl AS REAL)),0) AS pnl "
                "FROM phase8_runtime_allocations WHERE session_id=? AND strategy_key=?"
            )
            parameters = (session_id, strategy_key)
        row = self._conn.execute(query, parameters).fetchone()
        return Decimal(str(row["pnl"] if row is not None else 0))

    def append_shadow_trade(
        self,
        *,
        trade_id: str,
        strategy_key: str,
        symbol: str,
        signal_id: str,
        opened_at: datetime,
        closed_at: datetime,
        gross_pnl: Decimal,
        costs: Decimal,
        regime: str,
    ) -> None:
        net = gross_pnl - costs
        with self._write():
            self._conn.execute(
                "INSERT OR IGNORE INTO phase8_runtime_shadow_trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_id,
                    strategy_key,
                    symbol,
                    signal_id,
                    _utc_iso(opened_at),
                    _utc_iso(closed_at),
                    str(gross_pnl),
                    str(costs),
                    str(net),
                    regime,
                ),
            )

    def open_shadow_position(
        self,
        *,
        session_id: str,
        strategy_key: str,
        symbol: str,
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM phase8_runtime_shadow_positions "
            "WHERE session_id=? AND strategy_key=? AND symbol=? AND state='open'",
            (session_id, strategy_key, symbol),
        ).fetchone()
        return dict(row) if row is not None else None

    def open_shadow_positions(self, session_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM phase8_runtime_shadow_positions WHERE session_id=? AND state='open' ORDER BY opened_at",
                (session_id,),
            ).fetchall()
        ]

    def add_shadow_position(
        self,
        *,
        session_id: str,
        strategy_key: str,
        symbol: str,
        entry_signal_id: str,
        quantity: Decimal,
        entry_decision_price: Decimal,
        entry_fill_price: Decimal,
        entry_fee: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        opened_at: datetime,
        holding_deadline: datetime,
    ) -> str:
        if quantity <= 0 or min(entry_decision_price, entry_fill_price, stop_price, target_price) <= 0:
            raise RuntimePersistenceError("shadow position contains nonpositive economics")
        position_id = (
            "shadowpos_" + canonical_hash({"strategy": strategy_key, "signal": entry_signal_id, "symbol": symbol})[:24]
        )
        with self._write():
            existing = self._conn.execute(
                "SELECT * FROM phase8_runtime_shadow_positions WHERE entry_signal_id=?",
                (entry_signal_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    strategy_key,
                    symbol,
                    str(quantity),
                    str(entry_fill_price),
                )
                actual = (
                    existing["strategy_key"],
                    existing["symbol"],
                    existing["quantity"],
                    existing["entry_fill_price"],
                )
                if actual != expected:
                    raise RuntimePersistenceError("shadow position retry changed economics")
                return str(existing["shadow_position_id"])
            conflict = self._conn.execute(
                "SELECT shadow_position_id FROM phase8_runtime_shadow_positions "
                "WHERE session_id=? AND strategy_key=? AND symbol=? AND state='open'",
                (session_id, strategy_key, symbol),
            ).fetchone()
            if conflict is not None:
                raise RuntimePersistenceError("duplicate open shadow strategy allocation")
            self._conn.execute(
                """
                INSERT INTO phase8_runtime_shadow_positions VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open'
                )
                """,
                (
                    position_id,
                    session_id,
                    strategy_key,
                    symbol,
                    entry_signal_id,
                    str(quantity),
                    str(entry_decision_price),
                    str(entry_fill_price),
                    str(entry_fee),
                    str(stop_price),
                    str(target_price),
                    str(entry_fill_price),
                    _utc_iso(opened_at),
                    _utc_iso(holding_deadline),
                ),
            )
            return position_id

    def update_shadow_high(self, shadow_position_id: str, price: Decimal) -> None:
        with self._write():
            row = self._conn.execute(
                "SELECT highest_price,state FROM phase8_runtime_shadow_positions WHERE shadow_position_id=?",
                (shadow_position_id,),
            ).fetchone()
            if row is not None and row["state"] == "open" and price > Decimal(str(row["highest_price"])):
                self._conn.execute(
                    "UPDATE phase8_runtime_shadow_positions SET highest_price=? WHERE shadow_position_id=?",
                    (str(price), shadow_position_id),
                )

    def close_shadow_position(
        self,
        *,
        shadow_position_id: str,
        exit_decision_price: Decimal,
        exit_fill_price: Decimal,
        exit_fee: Decimal,
        closed_at: datetime,
        regime: str,
    ) -> dict[str, Decimal]:
        with self._write():
            row = self._conn.execute(
                "SELECT * FROM phase8_runtime_shadow_positions WHERE shadow_position_id=?",
                (shadow_position_id,),
            ).fetchone()
            if row is None:
                raise RuntimePersistenceError(f"unknown shadow position {shadow_position_id}")
            if row["state"] != "open":
                raise RuntimePersistenceError("shadow position is already closed")
            quantity = Decimal(str(row["quantity"]))
            entry_decision = Decimal(str(row["entry_decision_price"]))
            entry_fill = Decimal(str(row["entry_fill_price"]))
            entry_fee = Decimal(str(row["entry_fee"]))
            gross_pnl = (exit_decision_price - entry_decision) * quantity
            slippage = ((entry_fill - entry_decision) + (exit_decision_price - exit_fill_price)) * quantity
            costs = entry_fee + exit_fee + slippage
            net_pnl = gross_pnl - costs
            trade_id = (
                "shadowtrade_" + canonical_hash({"position": shadow_position_id, "closed_at": _utc_iso(closed_at)})[:24]
            )
            self._conn.execute(
                "UPDATE phase8_runtime_shadow_positions SET state='closed' WHERE shadow_position_id=?",
                (shadow_position_id,),
            )
            self._conn.execute(
                "INSERT INTO phase8_runtime_shadow_trades VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    trade_id,
                    row["strategy_key"],
                    row["symbol"],
                    row["entry_signal_id"],
                    row["opened_at"],
                    _utc_iso(closed_at),
                    str(gross_pnl),
                    str(costs),
                    str(net_pnl),
                    regime,
                ),
            )
            return {
                "gross_pnl": gross_pnl,
                "costs": costs,
                "net_pnl": net_pnl,
            }

    def shadow_summary(
        self,
        strategy_key: str,
        *,
        reference_capital: float = 50.0,
    ) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT CAST(net_pnl AS REAL) AS pnl FROM phase8_runtime_shadow_trades "
            "WHERE strategy_key=? ORDER BY closed_at",
            (strategy_key,),
        ).fetchall()
        pnls = [float(row["pnl"]) for row in rows]
        wins = [value for value in pnls if value > 0]
        losses = [value for value in pnls if value < 0]
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        cumulative = 0.0
        peak = 0.0
        maximum_drawdown = 0.0
        for value in pnls:
            cumulative += value
            peak = max(peak, cumulative)
            maximum_drawdown = max(maximum_drawdown, peak - cumulative)
        return {
            "completed_trades": len(pnls),
            "independent_signals": len(pnls),
            "net_pnl": sum(pnls),
            "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
            "profit_factor": gross_profit / max(gross_loss, 1e-12) if wins else 0.0,
            "win_rate": len(wins) / len(pnls) if pnls else 0.0,
            "maximum_drawdown_fraction": maximum_drawdown / max(reference_capital, 1e-12),
            "profitable_time_slices_fraction": len(wins) / len(pnls) if pnls else 0.0,
            "single_trade_fraction": (max((abs(value) for value in pnls), default=0.0) / max(abs(sum(pnls)), 1e-12)),
        }

    def events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self._conn.execute("SELECT * FROM phase8_runtime_events ORDER BY sequence").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM phase8_runtime_events WHERE run_id=? ORDER BY sequence",
                (run_id,),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def verify_event_chain(self) -> bool:
        previous = "GENESIS"
        rows = self._conn.execute("SELECT * FROM phase8_runtime_events ORDER BY sequence").fetchall()
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            semantic = _json(
                {
                    "run_id": row["run_id"],
                    "session_id": row["session_id"],
                    "event_type": row["event_type"],
                    "occurred_at": row["occurred_at"],
                    "payload_hash": row["payload_hash"],
                }
            )
            expected = hashlib.sha256((previous + semantic).encode("utf-8")).hexdigest()
            if expected != row["event_hash"]:
                return False
            previous = expected
        return True

    def close(self) -> None:
        with self._lock:
            self._conn.close()
