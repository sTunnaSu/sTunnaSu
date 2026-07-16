"""Opportunity identity ownership and collision-prevention tests."""

from __future__ import annotations

import pytest

from src.latency_budgeter.domain.errors import IdentifierValidationError
from src.latency_budgeter.domain.identifiers import (
    IDENTIFIER_OWNERSHIP,
    Phase8IdentifierFactory,
    validate_identifier,
)


def test_signal_and_decision_identifiers_are_retry_stable() -> None:
    factory = Phase8IdentifierFactory()
    run_id = factory.new_run_id()
    kwargs = {
        "run_id": run_id,
        "observation_fingerprint": "f" * 64,
        "strategy_version": "v1",
        "side": "buy",
        "signal_key": "alpha-1",
    }

    first_signal = factory.signal_id(**kwargs)
    second_signal = factory.signal_id(**kwargs)
    first_decision = factory.decision_id(run_id=run_id, signal_id=first_signal, config_version="phase8-step1-v1")
    second_decision = factory.decision_id(run_id=run_id, signal_id=second_signal, config_version="phase8-step1-v1")

    assert first_signal == second_signal
    assert first_decision == second_decision
    assert factory.new_run_id() != run_id
    assert IDENTIFIER_OWNERSHIP.decision_id_owner == "phase8_intake_service"


def test_signal_key_prevents_accidental_coalescing_of_distinct_signals() -> None:
    factory = Phase8IdentifierFactory()
    run_id = factory.new_run_id()
    common = {
        "run_id": run_id,
        "observation_fingerprint": "f" * 64,
        "strategy_version": "v1",
        "side": "buy",
    }

    assert factory.signal_id(**common, signal_key="first") != factory.signal_id(**common, signal_key="second")


def test_identifier_namespace_mismatch_is_rejected() -> None:
    run_id = Phase8IdentifierFactory.new_run_id()
    with pytest.raises(IdentifierValidationError, match="not owned"):
        validate_identifier(run_id, "sig")
