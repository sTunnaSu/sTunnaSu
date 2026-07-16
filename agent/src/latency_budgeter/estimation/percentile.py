"""Explicit deterministic nearest-rank percentile estimator."""

from __future__ import annotations

from math import ceil
from typing import Iterable

from src.latency_budgeter.domain.values import Milliseconds

PERCENTILE_DEFINITION = "nearest-rank-v1:sorted-ascending[ceil(p*n/100)-1]"


def nearest_rank_percentile(values: Iterable[Milliseconds], percentile: int) -> Milliseconds:
    """Return the nearest-rank percentile, including exact tie semantics.

    Values are sorted ascending by duration.  For ``n`` values, the one-based
    rank is ``ceil(percentile * n / 100)``.  Duplicate values remain duplicate
    observations.  This avoids dependency-specific interpolation defaults.
    """
    if percentile <= 0 or percentile > 100:
        raise ValueError("percentile must be in (0, 100]")
    ordered = sorted(values, key=lambda item: item.value)
    if not ordered:
        raise ValueError("at least one value is required")
    rank = max(1, ceil(percentile * len(ordered) / 100))
    return ordered[rank - 1]
