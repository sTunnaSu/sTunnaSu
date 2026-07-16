"""Safe JSON/YAML loader for frozen Phase 8 configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from src.latency_budgeter.configuration.models import LatencyBudgetConfig


def load_latency_budget_config(
    source: Mapping[str, Any] | str | Path,
) -> LatencyBudgetConfig:
    """Load and validate a frozen Phase 8 configuration.

    Args:
        source: A mapping or a path ending in ``.json``, ``.yaml``, or ``.yml``.

    Raises:
        ValueError: If a file has an unsupported format or non-object root.
        pydantic.ValidationError: If fields violate the frozen contract.
    """
    if isinstance(source, Mapping):
        raw: Any = dict(source)
    else:
        path = Path(source)
        text = path.read_text(encoding="utf-8")
        suffix = path.suffix.lower()
        if suffix == ".json":
            raw = json.loads(text)
        elif suffix in {".yaml", ".yml"}:
            raw = yaml.safe_load(text)
        else:
            raise ValueError("Phase 8 config files must be JSON or YAML")
    if not isinstance(raw, Mapping):
        raise ValueError("Phase 8 configuration root must be an object")
    return LatencyBudgetConfig.model_validate(raw)
