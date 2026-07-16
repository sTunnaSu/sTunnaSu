"""Immutable, schema-versioned Phase 8 event envelope."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from src.latency_budgeter.domain.errors import (
    UnknownEventTypeError,
    UnsupportedSchemaVersionError,
)
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory, validate_identifier
from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso

EVENT_SCHEMA_VERSION = 1


class EventType(str, Enum):
    """Frozen final Phase 8 event family."""

    DECISION_CREATED = "decision_created"
    ORDER_SUBMITTED = "order_submitted"
    BROKER_ACKNOWLEDGED = "broker_acknowledged"
    FILL_RECEIVED = "fill_received"
    ORDER_TERMINAL = "order_terminal"
    LIFECYCLE_INTEGRITY_FAILURE = "lifecycle_integrity_failure"
    EXECUTION_EVALUATED = "execution_evaluated"
    OUTCOME_EVALUATED = "outcome_evaluated"


@dataclass(frozen=True, slots=True)
class LedgerEvent:
    """An immutable event; persistence assigns ``aggregate_version`` once."""

    event_id: str
    event_type: EventType
    occurred_at: datetime
    recorded_at: datetime
    decision_id: str
    run_id: str
    signal_id: str
    payload: Mapping[str, Any]
    idempotency_key: str
    schema_version: int = EVENT_SCHEMA_VERSION
    aggregate_version: int = 0
    causation_id: str | None = None
    correlation_id: str | None = None
    source_metadata: Mapping[str, Any] = field(default_factory=dict)
    integrity_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported event schema version: {self.schema_version}")
        validate_identifier(self.event_id, "evt")
        validate_identifier(self.decision_id, "dec")
        validate_identifier(self.run_id, "run")
        validate_identifier(self.signal_id, "sig")
        try:
            event_type = EventType(self.event_type)
        except ValueError as exc:
            raise UnknownEventTypeError(f"unknown Phase 8 event type: {self.event_type!r}") from exc
        object.__setattr__(self, "event_type", event_type)
        occurred_at = normalize_timestamp(self.occurred_at)
        recorded_at = normalize_timestamp(self.recorded_at)
        if recorded_at < occurred_at:
            raise ValueError("recorded_at cannot precede occurred_at")
        object.__setattr__(self, "occurred_at", occurred_at)
        object.__setattr__(self, "recorded_at", recorded_at)
        if self.aggregate_version < 0:
            raise ValueError("aggregate_version cannot be negative")
        key = str(self.idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key is required")
        object.__setattr__(self, "idempotency_key", key)
        correlation_id = str(self.correlation_id or self.decision_id)
        validate_identifier(correlation_id, "dec")
        object.__setattr__(self, "correlation_id", correlation_id)
        if self.causation_id:
            causation_id = str(self.causation_id)
            validate_identifier(causation_id, "evt")
            object.__setattr__(self, "causation_id", causation_id)
        else:
            object.__setattr__(self, "causation_id", None)
        object.__setattr__(self, "payload", freeze_json(self.payload))
        object.__setattr__(self, "source_metadata", freeze_json(self.source_metadata))
        object.__setattr__(self, "integrity_metadata", freeze_json(self.integrity_metadata))

    @classmethod
    def create(
        cls,
        *,
        event_type: EventType,
        occurred_at: datetime,
        recorded_at: datetime,
        decision_id: str,
        run_id: str,
        signal_id: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        source_metadata: Mapping[str, Any] | None = None,
        integrity_metadata: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> "LedgerEvent":
        """Create an unsequenced event ready for an append-only ledger."""
        return cls(
            event_id=event_id or Phase8IdentifierFactory.new_event_id(),
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            decision_id=decision_id,
            run_id=run_id,
            signal_id=signal_id,
            payload=payload,
            idempotency_key=idempotency_key,
            causation_id=causation_id,
            correlation_id=correlation_id,
            source_metadata=source_metadata or {},
            integrity_metadata=integrity_metadata or {},
        )

    @property
    def semantic_fingerprint(self) -> str:
        """Hash immutable business content, excluding retry-local envelope data."""
        material = {
            "event_type": self.event_type.value,
            "occurred_at": utc_iso(self.occurred_at),
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "signal_id": self.signal_id,
            "schema_version": self.schema_version,
            "payload": thaw_json(self.payload),
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "source_metadata": thaw_json(self.source_metadata),
            "integrity_metadata": thaw_json(self.integrity_metadata),
        }
        return hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Serialise the complete event envelope deterministically."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "occurred_at": utc_iso(self.occurred_at),
            "recorded_at": utc_iso(self.recorded_at),
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "signal_id": self.signal_id,
            "schema_version": self.schema_version,
            "aggregate_version": self.aggregate_version,
            "payload": thaw_json(self.payload),
            "idempotency_key": self.idempotency_key,
            "causation_id": self.causation_id,
            "correlation_id": self.correlation_id,
            "source_metadata": thaw_json(self.source_metadata),
            "integrity_metadata": thaw_json(self.integrity_metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LedgerEvent":
        """Load current or legacy-v1 envelopes; reject unknown future semantics."""
        version = int(value.get("schema_version", EVENT_SCHEMA_VERSION))
        if version != EVENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported event schema version: {version}")
        raw_type = value.get("event_type")
        try:
            event_type = EventType(str(raw_type))
        except ValueError as exc:
            raise UnknownEventTypeError(f"unknown Phase 8 event type: {raw_type!r}") from exc
        return cls(
            event_id=str(value["event_id"]),
            event_type=event_type,
            occurred_at=normalize_timestamp(str(value["occurred_at"])),
            recorded_at=normalize_timestamp(str(value["recorded_at"])),
            decision_id=str(value["decision_id"]),
            run_id=str(value["run_id"]),
            signal_id=str(value["signal_id"]),
            schema_version=version,
            aggregate_version=int(value.get("aggregate_version", 0)),
            payload=value.get("payload", {}),
            idempotency_key=str(value["idempotency_key"]),
            causation_id=value.get("causation_id"),
            correlation_id=value.get("correlation_id"),
            source_metadata=value.get("source_metadata", {}),
            integrity_metadata=value.get("integrity_metadata", {}),
        )
