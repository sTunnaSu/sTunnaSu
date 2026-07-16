"""Numerically stable exponential signal-decay model."""

from __future__ import annotations

import math

from src.latency_budgeter.domain.values import Milliseconds


def exponential_decay(total_latency: Milliseconds, tau: Milliseconds) -> float:
    """Return ``exp(-latency/tau)`` with safe underflow at extreme ratios."""
    if tau.value <= 0:
        raise ValueError("tau_ms must be positive")
    ratio = total_latency.value / tau.value
    if ratio < 0:
        raise ValueError("forecast latency cannot be negative")
    # exp(-746) underflows to zero on IEEE-754 doubles; zero is the correct
    # limiting value and avoids platform-specific subnormal differences.
    result = 0.0 if ratio >= 746 else math.exp(-float(ratio))
    if not math.isfinite(result) or result < 0.0 or result > 1.0:
        raise ArithmeticError("decay factor is outside [0, 1]")
    return result
