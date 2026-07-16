"""UTC normalisation and timezone-boundary tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.latency_budgeter.domain.errors import TimestampValidationError
from src.latency_budgeter.domain.timestamps import (
    NaiveTimestampPolicy,
    normalize_timestamp,
    utc_iso,
)


def test_utc_conversion_and_deterministic_microsecond_serialization() -> None:
    converted = normalize_timestamp("2026-07-16T13:00:00.123456+01:00")

    assert converted == datetime(2026, 7, 16, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert utc_iso(converted) == "2026-07-16T12:00:00.123456Z"


def test_daylight_saving_boundary_preserves_the_instant() -> None:
    before_fallback = datetime(2025, 10, 26, 1, 30, fold=0, tzinfo=ZoneInfo("Europe/London"))
    after_fallback = datetime(2025, 10, 26, 1, 30, fold=1, tzinfo=ZoneInfo("Europe/London"))

    assert normalize_timestamp(before_fallback) != normalize_timestamp(after_fallback)
    assert normalize_timestamp(after_fallback) - normalize_timestamp(before_fallback) == timedelta(hours=1)


def test_naive_timestamp_is_rejected_by_default() -> None:
    with pytest.raises(TimestampValidationError, match="timezone-naive"):
        normalize_timestamp(datetime(2026, 1, 1, 12, 0))


def test_naive_timestamp_conversion_requires_explicit_unambiguous_timezone() -> None:
    converted = normalize_timestamp(
        datetime(2026, 7, 16, 13, 0),
        naive_policy=NaiveTimestampPolicy.REQUIRE_SOURCE_TIMEZONE,
        source_timezone="Europe/London",
    )
    assert converted == datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)

    with pytest.raises(TimestampValidationError, match="ambiguous local time"):
        normalize_timestamp(
            datetime(2025, 10, 26, 1, 30),
            naive_policy=NaiveTimestampPolicy.REQUIRE_SOURCE_TIMEZONE,
            source_timezone="Europe/London",
        )


def test_malformed_timezone_is_rejected() -> None:
    with pytest.raises(TimestampValidationError, match="unknown IANA timezone"):
        normalize_timestamp(
            datetime(2026, 1, 1, 12, 0),
            naive_policy=NaiveTimestampPolicy.REQUIRE_SOURCE_TIMEZONE,
            source_timezone="Mars/Olympus_Mons",
        )
