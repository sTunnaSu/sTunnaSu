"""Observation, raw-signal, and shared-cohort domain records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from src.latency_budgeter.domain.errors import UnsupportedSchemaVersionError
from src.latency_budgeter.domain.identifiers import validate_identifier
from src.latency_budgeter.domain.json_values import canonical_json, freeze_json, thaw_json
from src.latency_budgeter.domain.timestamps import (
    NaiveTimestampPolicy,
    normalize_timestamp,
    utc_iso,
    validate_timezone,
)

OBSERVATION_SCHEMA_VERSION = 1
SIGNAL_SCHEMA_VERSION = 1
CLASSIFICATION_SCHEMA_VERSION = 1


class SignalSide(str, Enum):
    """Raw strategy direction."""

    BUY = "buy"
    SELL = "sell"
    FLAT = "flat"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Immutable provenance supplied by the market-data adapter."""

    provider: str
    feed: str = ""
    venue: str = ""
    source_event_id: str = ""
    capture_method: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", str(self.provider).strip())
        object.__setattr__(self, "feed", str(self.feed).strip())
        object.__setattr__(self, "venue", str(self.venue).strip())
        object.__setattr__(self, "source_event_id", str(self.source_event_id).strip())
        object.__setattr__(self, "capture_method", str(self.capture_method).strip())
        object.__setattr__(self, "attributes", freeze_json(self.attributes))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached serialisable representation."""
        return {
            "provider": self.provider,
            "feed": self.feed,
            "venue": self.venue,
            "source_event_id": self.source_event_id,
            "capture_method": self.capture_method,
            "attributes": thaw_json(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class MarketObservation:
    """A structurally valid market observation before cohort enforcement."""

    source: str
    observed_at: datetime
    source_capture_at: datetime | None
    timezone: str
    symbol: str
    side: SignalSide
    strategy_version: str
    source_metadata: SourceMetadata
    raw_payload: Mapping[str, Any] | None = None
    payload_reference: str | None = None
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported observation schema version: {self.schema_version}")
        validate_timezone(self.timezone)
        object.__setattr__(self, "source", str(self.source).strip())
        object.__setattr__(self, "symbol", str(self.symbol).strip())
        object.__setattr__(self, "strategy_version", str(self.strategy_version).strip())
        if not self.symbol or not self.strategy_version:
            raise ValueError("symbol and strategy_version are required")
        object.__setattr__(self, "side", SignalSide(self.side))
        object.__setattr__(
            self,
            "observed_at",
            normalize_timestamp(self.observed_at, source_timezone=self.timezone),
        )
        if self.source_capture_at is not None:
            object.__setattr__(
                self,
                "source_capture_at",
                normalize_timestamp(self.source_capture_at, source_timezone=self.timezone),
            )
        if (self.raw_payload is None) == (self.payload_reference is None):
            raise ValueError("provide exactly one of raw_payload or payload_reference")
        if self.raw_payload is not None:
            object.__setattr__(self, "raw_payload", freeze_json(self.raw_payload))
        if self.payload_reference is not None:
            reference = str(self.payload_reference).strip()
            if not reference:
                raise ValueError("payload_reference cannot be empty")
            object.__setattr__(self, "payload_reference", reference)

    @classmethod
    def from_input(
        cls,
        *,
        source: str,
        observed_at: datetime | str,
        source_capture_at: datetime | str | None,
        timezone: str,
        symbol: str,
        side: SignalSide | str,
        strategy_version: str,
        source_metadata: SourceMetadata,
        raw_payload: Mapping[str, Any] | None = None,
        payload_reference: str | None = None,
        naive_policy: NaiveTimestampPolicy = NaiveTimestampPolicy.REJECT,
        schema_version: int = OBSERVATION_SCHEMA_VERSION,
    ) -> "MarketObservation":
        """Parse external values under an explicit naive-timestamp policy."""
        return cls(
            source=source,
            observed_at=normalize_timestamp(
                observed_at,
                naive_policy=naive_policy,
                source_timezone=timezone,
            ),
            source_capture_at=(
                normalize_timestamp(
                    source_capture_at,
                    naive_policy=naive_policy,
                    source_timezone=timezone,
                )
                if source_capture_at is not None
                else None
            ),
            timezone=timezone,
            symbol=symbol,
            side=SignalSide(side),
            strategy_version=strategy_version,
            source_metadata=source_metadata,
            raw_payload=raw_payload,
            payload_reference=payload_reference,
            schema_version=schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the immutable evidence as serialisable data."""
        return {
            "source": self.source,
            "observed_at": utc_iso(self.observed_at),
            "source_capture_at": utc_iso(self.source_capture_at) if self.source_capture_at is not None else None,
            "timezone": self.timezone,
            "raw_payload": thaw_json(self.raw_payload) if self.raw_payload is not None else None,
            "payload_reference": self.payload_reference,
            "symbol": self.symbol,
            "side": self.side.value,
            "strategy_version": self.strategy_version,
            "source_metadata": self.source_metadata.to_dict(),
            "schema_version": self.schema_version,
        }

    @property
    def fingerprint(self) -> str:
        """Return a deterministic content identity for signal correlation."""
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawStrategySignal:
    """Immutable raw signal correlated to one observation and run."""

    signal_id: str
    run_id: str
    observation_fingerprint: str
    generated_at: datetime
    symbol: str
    side: SignalSide
    strategy_version: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SIGNAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SIGNAL_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported signal schema version: {self.schema_version}")
        validate_identifier(self.signal_id, "sig")
        validate_identifier(self.run_id, "run")
        if len(self.observation_fingerprint) != 64:
            raise ValueError("observation_fingerprint must be a SHA-256 hex digest")
        int(self.observation_fingerprint, 16)
        object.__setattr__(self, "generated_at", normalize_timestamp(self.generated_at))
        object.__setattr__(self, "symbol", str(self.symbol).strip())
        object.__setattr__(self, "strategy_version", str(self.strategy_version).strip())
        object.__setattr__(self, "side", SignalSide(self.side))
        object.__setattr__(self, "metadata", freeze_json(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached serialisable representation."""
        return {
            "signal_id": self.signal_id,
            "run_id": self.run_id,
            "observation_fingerprint": self.observation_fingerprint,
            "generated_at": utc_iso(self.generated_at),
            "symbol": self.symbol,
            "side": self.side.value,
            "strategy_version": self.strategy_version,
            "metadata": thaw_json(self.metadata),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class CohortClassification:
    """Versioned, non-blocking flags shared by baseline and later Phase 8 arms."""

    provenance_valid_for_phase8: bool
    data_fresh_for_phase8: bool
    strategy_requirements_met: bool
    common_phase8_eligible: bool
    classification_version: str
    evaluated_at: datetime
    data_age_ms: float
    ingestion_delay_ms: float | None
    processing_delay_ms: float | None
    reasons: tuple[str, ...] = ()
    schema_version: int = CLASSIFICATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CLASSIFICATION_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(f"unsupported classification schema version: {self.schema_version}")
        expected = self.provenance_valid_for_phase8 and self.data_fresh_for_phase8 and self.strategy_requirements_met
        if self.common_phase8_eligible is not expected:
            raise ValueError("common_phase8_eligible must equal the conjunction of shared flags")
        object.__setattr__(self, "evaluated_at", normalize_timestamp(self.evaluated_at))
        object.__setattr__(self, "reasons", tuple(str(reason) for reason in self.reasons))

    def to_dict(self) -> dict[str, Any]:
        """Return a detached serialisable representation."""
        return {
            "provenance_valid_for_phase8": self.provenance_valid_for_phase8,
            "data_fresh_for_phase8": self.data_fresh_for_phase8,
            "strategy_requirements_met": self.strategy_requirements_met,
            "common_phase8_eligible": self.common_phase8_eligible,
            "classification_version": self.classification_version,
            "evaluated_at": utc_iso(self.evaluated_at),
            "data_age_ms": self.data_age_ms,
            "ingestion_delay_ms": self.ingestion_delay_ms,
            "processing_delay_ms": self.processing_delay_ms,
            "reasons": list(self.reasons),
            "schema_version": self.schema_version,
        }
