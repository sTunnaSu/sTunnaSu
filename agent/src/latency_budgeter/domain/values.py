"""Precision-safe value objects for Phase 8 decision arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Any

BPS_QUANTUM = Decimal("0.000000001")
MILLISECONDS_QUANTUM = Decimal("0.001")
MAX_MILLISECONDS = Decimal("9000000000000000")


def _decimal(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


@dataclass(frozen=True, slots=True)
class BasisPoints:
    """A basis-point amount rounded once to a documented 1e-9 bps grid."""

    value: Decimal

    def __init__(self, value: Any) -> None:
        decimal = _decimal(value, label="basis points")
        object.__setattr__(self, "value", decimal.quantize(BPS_QUANTUM, rounding=ROUND_HALF_EVEN))

    def __add__(self, other: "BasisPoints") -> "BasisPoints":
        return BasisPoints(self.value + other.value)

    def __sub__(self, other: "BasisPoints") -> "BasisPoints":
        return BasisPoints(self.value - other.value)

    def __mul__(self, scalar: Any) -> "BasisPoints":
        return BasisPoints(self.value * _decimal(scalar, label="basis-point multiplier"))

    def to_float(self) -> float:
        """Return a finite JSON-compatible number after fixed-grid rounding."""
        return float(self.value)

    def canonical(self) -> str:
        """Return fixed-point text without exponent notation."""
        return format(self.value, "f")


@dataclass(frozen=True, slots=True)
class Milliseconds:
    """A non-negative duration represented at exact microsecond precision."""

    value: Decimal

    def __init__(self, value: Any) -> None:
        decimal = _decimal(value, label="milliseconds")
        if decimal < 0:
            raise ValueError("milliseconds cannot be negative")
        if decimal > MAX_MILLISECONDS:
            raise OverflowError("milliseconds exceed the supported audit range")
        object.__setattr__(
            self,
            "value",
            decimal.quantize(MILLISECONDS_QUANTUM, rounding=ROUND_HALF_EVEN),
        )

    def __add__(self, other: "Milliseconds") -> "Milliseconds":
        return Milliseconds(self.value + other.value)

    def to_float(self) -> float:
        """Return a finite JSON-compatible number."""
        return float(self.value)

    def canonical(self) -> str:
        """Return fixed-point text without exponent notation."""
        return format(self.value, "f")
