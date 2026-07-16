"""Precision-safe Phase 8 Step 3 execution-lifecycle records.

These records describe observations reported by an execution adapter.  They
never size, price, submit, cancel, or amend an order themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from src.latency_budgeter.domain.models import SignalSide
from src.latency_budgeter.domain.timestamps import normalize_timestamp

STEP3_LIFECYCLE_VERSION = "phase8-step3-lifecycle-v1"
STEP3_AUDIT_VERSION = "phase8-step3-terminal-audit-v1"
STEP3_EXECUTION_EVALUATION_VERSION = "phase8-step3-execution-evaluation-v1"


def exact_decimal(value: Any, *, label: str, allow_zero: bool = True) -> Decimal:
    """Parse one finite exact decimal without an intermediate float."""
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    if parsed < 0 or (not allow_zero and parsed == 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {qualifier}")
    return parsed


def signed_decimal(value: Any, *, label: str) -> Decimal:
    """Parse a finite signed amount (price improvement may be negative)."""
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} must be finite")
    return parsed


def decimal_text(value: Decimal | None) -> str | None:
    """Serialise exact decimal evidence without exponent notation."""
    return format(value, "f") if value is not None else None


class HandoffState(str, Enum):
    READY = "READY"
    ALREADY_SUBMITTED = "ALREADY_SUBMITTED"


class AcknowledgementAvailability(str, Enum):
    RECEIVED = "RECEIVED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_RECEIVED = "NOT_RECEIVED"
    INTEGRATION_ERROR = "INTEGRATION_ERROR"


class ComponentValidationStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"
    PENDING_CONTEXT = "PENDING_CONTEXT"


class TerminalState(str, Enum):
    FULLY_FILLED = "FULLY_FILLED"
    PARTIALLY_FILLED_EXPIRED = "PARTIALLY_FILLED_EXPIRED"
    EXPIRED_UNFILLED = "EXPIRED_UNFILLED"
    # The existing engine has terminal paths beyond expiry.  They are retained
    # explicitly rather than relabelled as expiry by Phase 8.
    PARTIALLY_FILLED_CANCELLED = "PARTIALLY_FILLED_CANCELLED"
    CANCELLED_UNFILLED = "CANCELLED_UNFILLED"
    REJECTED_UNFILLED = "REJECTED_UNFILLED"


class ExecutionOutcome(str, Enum):
    FULLY_FILLED = "FULLY_FILLED"
    PARTIALLY_FILLED_EXPIRED = "PARTIALLY_FILLED_EXPIRED"
    NO_FILL = "NO_FILL"


class TerminalReason(str, Enum):
    FILLED = "FILLED"
    EXPIRE_UNFILLED_REMAINDER = "EXPIRE_UNFILLED_REMAINDER"
    CANCEL_UNFILLED_REMAINDER = "CANCEL_UNFILLED_REMAINDER"
    VENUE_REJECTED = "VENUE_REJECTED"


@dataclass(frozen=True, slots=True)
class ExecutionAuthorization:
    """Immutable proof that one Step 2 ALLOW may enter execution."""

    decision_id: str
    run_id: str
    signal_id: str
    decision_event_id: str
    decision_at: datetime
    observed_at: datetime
    symbol: str
    side: SignalSide
    frozen_decision_fingerprint: str
    client_order_id: str
    state: HandoffState = HandoffState.READY
    submitted_order_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "decision_at", normalize_timestamp(self.decision_at))
        object.__setattr__(self, "observed_at", normalize_timestamp(self.observed_at))
        object.__setattr__(self, "side", SignalSide(self.side))
        object.__setattr__(self, "state", HandoffState(self.state))
        if self.side is SignalSide.FLAT:
            raise ValueError("flat signals cannot be authorised for submission")
        if not self.symbol or not self.client_order_id or not self.frozen_decision_fingerprint:
            raise ValueError("symbol, client_order_id and frozen decision fingerprint are required")


@dataclass(frozen=True, slots=True)
class SubmissionObservation:
    """Actual successful submission callback from the unchanged engine."""

    order_id: str
    client_order_id: str
    submitted_at: datetime
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None = None
    participation_limit: Decimal | None = None
    expiry_at: datetime | None = None
    venue_reference: str | None = None
    timestamp_source: str = "execution_adapter"

    def __post_init__(self) -> None:
        object.__setattr__(self, "submitted_at", normalize_timestamp(self.submitted_at))
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, label="submitted quantity", allow_zero=False))
        order_type = str(self.order_type).lower().strip()
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        object.__setattr__(self, "order_type", order_type)
        if self.limit_price is not None:
            object.__setattr__(self, "limit_price", exact_decimal(self.limit_price, label="limit price", allow_zero=False))
        if order_type == "limit" and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.participation_limit is not None:
            participation = exact_decimal(self.participation_limit, label="participation limit")
            if participation > 1:
                raise ValueError("participation_limit cannot exceed 1")
            object.__setattr__(self, "participation_limit", participation)
        if self.expiry_at is not None:
            object.__setattr__(self, "expiry_at", normalize_timestamp(self.expiry_at))
        if not self.order_id or not self.client_order_id or not self.timestamp_source:
            raise ValueError("order_id, client_order_id and timestamp_source are required")


@dataclass(frozen=True, slots=True)
class AcknowledgementObservation:
    """One real broker/venue acknowledgement; absence is not this record."""

    order_id: str
    acknowledged_at: datetime
    acknowledgement_id: str
    venue_reference: str | None = None
    timestamp_source: str = "broker"

    def __post_init__(self) -> None:
        object.__setattr__(self, "acknowledged_at", normalize_timestamp(self.acknowledged_at))
        if not self.order_id or not self.acknowledgement_id or not self.timestamp_source:
            raise ValueError("order_id, acknowledgement_id and timestamp_source are required")


@dataclass(frozen=True, slots=True)
class FillObservation:
    """One actual fill message with exact quantities and optional cost facts."""

    order_id: str
    fill_id: str
    fill_at: datetime
    side: SignalSide
    quantity: Decimal
    price: Decimal
    cumulative_filled_quantity: Decimal
    unfilled_quantity: Decimal
    venue_reference: str | None = None
    decision_price: Decimal | None = None
    fee: Decimal | None = None
    spread_cost: Decimal | None = None
    slippage_cost: Decimal | None = None
    impact_cost: Decimal | None = None
    implementation_shortfall: Decimal | None = None
    timestamp_source: str = "execution_adapter"

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_at", normalize_timestamp(self.fill_at))
        object.__setattr__(self, "side", SignalSide(self.side))
        if self.side is SignalSide.FLAT:
            raise ValueError("fill side cannot be flat")
        object.__setattr__(self, "quantity", exact_decimal(self.quantity, label="fill quantity", allow_zero=False))
        object.__setattr__(self, "price", exact_decimal(self.price, label="fill price", allow_zero=False))
        object.__setattr__(
            self,
            "cumulative_filled_quantity",
            exact_decimal(self.cumulative_filled_quantity, label="cumulative filled quantity"),
        )
        object.__setattr__(self, "unfilled_quantity", exact_decimal(self.unfilled_quantity, label="unfilled quantity"))
        if self.cumulative_filled_quantity < self.quantity:
            raise ValueError("cumulative filled quantity cannot be smaller than this fill")
        for field_name in ("decision_price", "fee"):
            value = getattr(self, field_name)
            if value is not None:
                allow_zero = field_name != "decision_price"
                object.__setattr__(self, field_name, exact_decimal(value, label=field_name, allow_zero=allow_zero))
        for field_name in ("spread_cost", "slippage_cost", "impact_cost", "implementation_shortfall"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, signed_decimal(value, label=field_name))
        if not self.order_id or not self.fill_id or not self.timestamp_source:
            raise ValueError("order_id, fill_id and timestamp_source are required")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is SignalSide.BUY else -self.quantity


@dataclass(frozen=True, slots=True)
class TerminalObservation:
    """Actual terminal callback after the engine has ended the order."""

    order_id: str
    terminal_at: datetime
    terminal_state: TerminalState
    reason_code: TerminalReason
    executed_quantity: Decimal
    unfilled_quantity: Decimal
    engine_status: str
    engine_status_reason: str = ""
    cancellation_fee: Decimal | None = None
    acknowledgement_availability: AcknowledgementAvailability = AcknowledgementAvailability.NOT_RECEIVED
    venue_reference: str | None = None
    timestamp_source: str = "execution_adapter"

    def __post_init__(self) -> None:
        object.__setattr__(self, "terminal_at", normalize_timestamp(self.terminal_at))
        object.__setattr__(self, "terminal_state", TerminalState(self.terminal_state))
        object.__setattr__(self, "reason_code", TerminalReason(self.reason_code))
        object.__setattr__(
            self,
            "executed_quantity",
            exact_decimal(self.executed_quantity, label="terminal executed quantity"),
        )
        object.__setattr__(
            self,
            "unfilled_quantity",
            exact_decimal(self.unfilled_quantity, label="terminal unfilled quantity"),
        )
        object.__setattr__(
            self,
            "acknowledgement_availability",
            AcknowledgementAvailability(self.acknowledgement_availability),
        )
        if self.cancellation_fee is not None:
            object.__setattr__(
                self,
                "cancellation_fee",
                exact_decimal(self.cancellation_fee, label="cancellation fee"),
            )
        if not self.order_id or not self.engine_status or not self.timestamp_source:
            raise ValueError("order_id, engine_status and timestamp_source are required")


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """Result of recording one immutable callback and rebuilding projection."""

    event_id: str
    appended: bool
    aggregate_version: int
    released_sample_ids: tuple[str, ...] = ()
