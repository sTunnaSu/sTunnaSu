"""UTC-safe timestamp parsing and deterministic serialisation.

Phase 8 persists instants at microsecond precision. Inputs must be timezone
aware unless the caller explicitly selects ``require_source_timezone`` and
supplies an unambiguous IANA timezone. No local-machine timezone is consulted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.latency_budgeter.domain.errors import TimestampValidationError

UTC = timezone.utc
TIMESTAMP_PRECISION = "microseconds"


class NaiveTimestampPolicy(str, Enum):
    """Explicit policy for timezone-naive inputs."""

    REJECT = "reject"
    REQUIRE_SOURCE_TIMEZONE = "require_source_timezone"


def validate_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA timezone or fail explicitly."""
    if not isinstance(name, str) or not name.strip():
        raise TimestampValidationError("source timezone must be a non-empty IANA name")
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError as exc:
        raise TimestampValidationError(f"unknown IANA timezone: {name!r}") from exc


def _parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        raise TimestampValidationError("timestamp must be a datetime or ISO-8601 string")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TimestampValidationError(f"invalid ISO-8601 timestamp: {value!r}") from exc


def _localize_naive(value: datetime, zone: ZoneInfo) -> datetime:
    first = value.replace(tzinfo=zone, fold=0)
    second = value.replace(tzinfo=zone, fold=1)

    def round_trips(candidate: datetime) -> bool:
        local = candidate.astimezone(UTC).astimezone(zone)
        return local.replace(tzinfo=None) == value

    first_valid = round_trips(first)
    second_valid = round_trips(second)
    if not first_valid and not second_valid:
        raise TimestampValidationError(f"non-existent local time {value.isoformat()} in {zone.key}")
    if first_valid and second_valid and first.utcoffset() != second.utcoffset():
        raise TimestampValidationError(
            f"ambiguous local time {value.isoformat()} in {zone.key}; supply an aware timestamp"
        )
    return first if first_valid else second


def normalize_timestamp(
    value: datetime | str,
    *,
    naive_policy: NaiveTimestampPolicy = NaiveTimestampPolicy.REJECT,
    source_timezone: str | None = None,
) -> datetime:
    """Normalise an instant to timezone-aware UTC without silent assumptions."""
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if naive_policy is NaiveTimestampPolicy.REJECT:
            raise TimestampValidationError("timezone-naive timestamps are rejected")
        if source_timezone is None:
            raise TimestampValidationError("a source timezone is required to convert a naive timestamp")
        parsed = _localize_naive(parsed, validate_timezone(source_timezone))
    elif source_timezone is not None:
        validate_timezone(source_timezone)
    return parsed.astimezone(UTC)


def utc_iso(value: datetime | str) -> str:
    """Return canonical UTC ISO-8601 text at microsecond precision."""
    normalised = normalize_timestamp(value)
    return normalised.isoformat(timespec=TIMESTAMP_PRECISION).replace("+00:00", "Z")


def elapsed_ms(later: datetime, earlier: datetime) -> int:
    """Return exact integer microsecond-derived milliseconds, truncated toward zero."""
    later_utc = normalize_timestamp(later)
    earlier_utc = normalize_timestamp(earlier)
    return int((later_utc - earlier_utc).total_seconds() * 1_000)


def elapsed_ms_exact(later: datetime, earlier: datetime) -> Decimal:
    """Return signed milliseconds exactly from integer microseconds."""
    delta = normalize_timestamp(later) - normalize_timestamp(earlier)
    total_microseconds = delta.days * 86_400 * 1_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    return Decimal(total_microseconds) / Decimal(1_000)
