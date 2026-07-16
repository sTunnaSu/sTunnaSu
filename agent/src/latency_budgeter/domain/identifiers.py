"""Frozen ownership and generation rules for Phase 8 identifiers."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from src.latency_budgeter.domain.errors import IdentifierValidationError

_SIGNAL_NAMESPACE = uuid.UUID("31cb2604-05d8-4ea9-95b0-cd74972ba17e")
_DECISION_NAMESPACE = uuid.UUID("f541df0c-9228-4a0f-a0f3-c38b78a5b29d")
_ID_PATTERN = re.compile(r"^(run|sig|dec|evt)_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class IdentifierOwnership:
    """Human- and machine-readable ownership contract."""

    run_id_owner: str = "run_orchestrator"
    signal_id_owner: str = "strategy_signal_adapter"
    decision_id_owner: str = "phase8_intake_service"
    event_id_owner: str = "event_producer"
    uniqueness: str = "128-bit UUID namespace; persistence enforces uniqueness"
    collision_policy: str = "raise; never overwrite or silently regenerate persisted content"


IDENTIFIER_OWNERSHIP = IdentifierOwnership()


def validate_identifier(value: str, prefix: str) -> str:
    """Validate an identifier against its frozen prefix and UUID payload."""
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise IdentifierValidationError(f"invalid Phase 8 identifier: {value!r}")
    if not value.startswith(f"{prefix}_"):
        raise IdentifierValidationError(f"identifier {value!r} is not owned by the {prefix!r} namespace")
    return value


class Phase8IdentifierFactory:
    """Generate explicit run/event IDs and deterministic signal/decision IDs."""

    @staticmethod
    def new_run_id() -> str:
        """Return an explicitly generated run identity owned by orchestration."""
        return f"run_{uuid.uuid4().hex}"

    @staticmethod
    def signal_id(
        *,
        run_id: str,
        observation_fingerprint: str,
        strategy_version: str,
        side: str,
        signal_key: str,
    ) -> str:
        """Derive a retry-stable signal ID from strategy-owned inputs."""
        validate_identifier(run_id, "run")
        material = "\x1f".join((run_id, observation_fingerprint, strategy_version, side, signal_key))
        return f"sig_{uuid.uuid5(_SIGNAL_NAMESPACE, material).hex}"

    @staticmethod
    def decision_id(*, run_id: str, signal_id: str, config_version: str) -> str:
        """Derive the unique Phase 8 opportunity identity."""
        validate_identifier(run_id, "run")
        validate_identifier(signal_id, "sig")
        material = "\x1f".join((run_id, signal_id, config_version))
        return f"dec_{uuid.uuid5(_DECISION_NAMESPACE, material).hex}"

    @staticmethod
    def new_event_id() -> str:
        """Return an explicitly generated immutable event identity."""
        return f"evt_{uuid.uuid4().hex}"
