"""Canonical, deeply immutable JSON values used by the event ledger."""

from __future__ import annotations

import json
import math
from types import MappingProxyType
from typing import Any, Mapping


def freeze_json(value: Any) -> Any:
    """Return a recursively immutable JSON-compatible value.

    Raises:
        TypeError: If *value* is not JSON-compatible.
        ValueError: If a floating-point value is not finite.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats are not valid ledger values")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("ledger mapping keys must be strings")
            frozen[key] = freeze_json(child)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(child) for child in value)
    raise TypeError(f"unsupported ledger value type: {type(value).__name__}")


def thaw_json(value: Any) -> Any:
    """Return a plain JSON-serialisable copy of an immutable value."""
    if isinstance(value, Mapping):
        return {str(key): thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(child) for child in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialise a value deterministically for hashing and persistence."""
    return json.dumps(
        thaw_json(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
