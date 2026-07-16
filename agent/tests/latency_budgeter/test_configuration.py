"""Frozen config loading, validation, and version tests."""

from __future__ import annotations

from pydantic import ValidationError
import pytest

from src.latency_budgeter.configuration.loader import load_latency_budget_config
from src.latency_budgeter.configuration.models import LatencyBudgetConfig


def test_configuration_is_frozen_and_has_stable_fingerprint() -> None:
    config = LatencyBudgetConfig()
    reloaded = LatencyBudgetConfig.model_validate_json(config.canonical_json())

    assert reloaded == config
    assert reloaded.fingerprint == config.fingerprint
    with pytest.raises(ValidationError):
        config.tau_ms = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        config.cost_assumptions.spread_bps = 20.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"tau_ms": 0},
        {"latency_percentile": 50},
        {"rolling_history_window": 5, "minimum_prior_samples": 6},
        {"cold_start_policy": "fallback_p90", "fallback_p90_latency_ms": 0},
        {"expiry_policy": {"mode": "fixed_ms"}},
        {"unknown_setting": True},
    ],
)
def test_invalid_configuration_is_rejected(overrides) -> None:
    with pytest.raises((ValidationError, ValueError)):
        LatencyBudgetConfig(**overrides)


def test_unsupported_config_version_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unsupported Phase 8 config version"):
        LatencyBudgetConfig(config_version="phase8-step9-v1")


def test_json_and_yaml_config_loading(tmp_path) -> None:
    json_path = tmp_path / "phase8.json"
    yaml_path = tmp_path / "phase8.yaml"
    json_path.write_text('{"tau_ms": 45000, "latency_percentile": 75}', encoding="utf-8")
    yaml_path.write_text("tau_ms: 45000\nlatency_percentile: 75\n", encoding="utf-8")

    assert load_latency_budget_config(json_path) == load_latency_budget_config(yaml_path)


def test_config_loader_rejects_non_object_root(tmp_path) -> None:
    path = tmp_path / "phase8.yaml"
    path.write_text("- not\n- an\n- object\n", encoding="utf-8")

    with pytest.raises(ValueError, match="root must be an object"):
        load_latency_budget_config(path)
