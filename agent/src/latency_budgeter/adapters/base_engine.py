"""Non-semantic Phase 8 adapter for the existing bar execution engine."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.models import FillRecord, OrderRecord
from src.latency_budgeter.application.lifecycle import ExecutionLifecycleService
from src.latency_budgeter.domain.errors import ExecutionBlockedError, LifecycleConflictError
from src.latency_budgeter.domain.lifecycle import (
    AcknowledgementAvailability,
    ExecutionAuthorization,
    FillObservation,
    HandoffState,
    SubmissionObservation,
    TerminalObservation,
    TerminalReason,
    TerminalState,
)
from src.latency_budgeter.domain.models import SignalSide

SymbolMapper = Callable[[str], str]


class BaseEngineLifecycleAdapter:
    """Gate actual BaseEngine submissions and mirror its resulting lifecycle.

    The adapter cannot choose quantity, price, order type, participation,
    expiry, fill quantity, cancellation, or terminal state.  It receives those
    facts from ``BaseEngine`` after the engine has made each transition.
    """

    def __init__(
        self,
        service: ExecutionLifecycleService,
        *,
        source_timezone: str = "UTC",
        symbol_mapper: SymbolMapper | None = None,
        acknowledgement_availability: AcknowledgementAvailability = AcknowledgementAvailability.UNSUPPORTED,
    ) -> None:
        self.service = service
        self.source_timezone = ZoneInfo(source_timezone)
        self.symbol_mapper = symbol_mapper or (lambda symbol: symbol)
        self.acknowledgement_availability = AcknowledgementAvailability(acknowledgement_availability)
        self._lock = threading.RLock()
        self._armed: dict[str, ExecutionAuthorization] = {}
        self._reserved: set[str] = set()
        self._by_order: dict[str, ExecutionAuthorization] = {}
        self._submissions: dict[str, SubmissionObservation] = {}
        self._intent_by_decision: dict[str, dict[str, Any]] = {}

    @staticmethod
    def requires_authorization(intent: Mapping[str, Any]) -> bool:
        """Budget new exposure; preserve engine-owned risk-reducing exits."""
        return str(intent.get("event_type", "")) == "entry"

    def arm(self, decision_id: str) -> ExecutionAuthorization:
        """Make one immutable ALLOW eligible for the next matching order."""
        authorization = self.service.authorize(decision_id)
        with self._lock:
            if authorization.state is HandoffState.ALREADY_SUBMITTED:
                if authorization.submitted_order_id:
                    self._by_order[authorization.submitted_order_id] = authorization
                return authorization
            prior = self._armed.get(decision_id)
            if prior is not None and prior != authorization:
                raise LifecycleConflictError("armed authorization changed without a new decision")
            self._armed[decision_id] = authorization
            return authorization

    def authorize_submission(self, intent: Mapping[str, Any]) -> ExecutionAuthorization | None:
        """Fail closed for unmatched or ambiguous engine order intents."""
        decision_id = str(intent.get("decision_id", ""))
        signal_id = str(intent.get("signal_id", ""))
        if not decision_id or not signal_id:
            return None
        symbol = self.symbol_mapper(str(intent.get("symbol", "")))
        side = SignalSide(str(intent.get("side", "")))
        with self._lock:
            authorization = self._armed.get(decision_id)
            if (
                authorization is None
                or decision_id in self._reserved
                or authorization.signal_id != signal_id
                or authorization.symbol != symbol
                or authorization.side is not side
            ):
                return None
            self._reserved.add(authorization.decision_id)
            self._intent_by_decision[authorization.decision_id] = dict(intent)
            return authorization

    def validate_authorization(
        self,
        intent: Mapping[str, Any],
        authorization: Any,
    ) -> bool:
        """Verify exact immutable identity before the engine registers an order."""
        if not isinstance(authorization, ExecutionAuthorization):
            return False
        decision_id = str(intent.get("decision_id", ""))
        signal_id = str(intent.get("signal_id", ""))
        with self._lock:
            return (
                decision_id == authorization.decision_id
                and signal_id == authorization.signal_id
                and decision_id in self._reserved
                and self._armed.get(decision_id) == authorization
                and self._intent_by_decision.get(decision_id) == dict(intent)
            )

    def on_order_submitted(self, order: OrderRecord, authorization: Any) -> None:
        """Record only the engine's post-registration submission callback."""
        if not isinstance(authorization, ExecutionAuthorization):
            raise ExecutionBlockedError("actual submission lacked an immutable ALLOW authorization")
        observation = SubmissionObservation(
            order_id=order.order_id,
            client_order_id=authorization.client_order_id,
            submitted_at=self._utc(order.created_time),
            quantity=Decimal(str(order.requested_quantity)),
            order_type=order.order_type,
            limit_price=Decimal(str(order.limit_price)) if order.limit_price is not None else None,
            participation_limit=self._intent_participation(authorization, order),
            expiry_at=self._utc(order.expires_time) if order.expires_time is not None else None,
            venue_reference=None,
            timestamp_source="base_engine_bar_timestamp",
        )
        with self._lock:
            self._by_order[order.order_id] = authorization
            self._submissions[order.order_id] = observation
            self._armed.pop(authorization.decision_id, None)
            self._reserved.discard(authorization.decision_id)
        self.service.record_submission(authorization, observation)

    def on_fill(self, order: OrderRecord, fill: FillRecord) -> None:
        """Mirror one actual fill after BaseEngine appended its FillRecord."""
        authorization = self._authorization_for(order.order_id)
        self._ensure_submission(order.order_id, authorization)
        cumulative = Decimal(str(order.requested_quantity)) - Decimal(str(fill.remaining_quantity or 0.0))
        fill_id = self._fill_id(fill, cumulative)
        observation = FillObservation(
            order_id=order.order_id,
            fill_id=fill_id,
            fill_at=self._utc(fill.timestamp),
            side=SignalSide(fill.side),
            quantity=Decimal(str(fill.quantity)),
            price=Decimal(str(fill.fill_price)),
            cumulative_filled_quantity=cumulative,
            unfilled_quantity=Decimal(str(fill.remaining_quantity or 0.0)),
            decision_price=Decimal(str(fill.decision_price)),
            fee=Decimal(str(fill.commission)),
            # BaseEngine exposes actual slippage accounting.  It does not
            # expose separate realised spread or impact, so those stay null.
            slippage_cost=Decimal(str(fill.slippage_cost)),
            timestamp_source="base_engine_fill_timestamp",
        )
        self.service.record_fill(authorization, observation)

    def on_order_terminal(self, order: OrderRecord) -> None:
        """Mirror actual engine terminal state without changing its cause."""
        authorization = self._authorization_for(order.order_id)
        self._ensure_submission(order.order_id, authorization)
        executed = Decimal(str(order.filled_quantity))
        unfilled = max(Decimal(str(order.requested_quantity)) - executed, Decimal("0"))
        terminal_state, reason = self._terminal_mapping(order, executed, unfilled)
        terminal_at = order.updated_time or order.last_fill_time or order.created_time
        observation = TerminalObservation(
            order_id=order.order_id,
            terminal_at=self._utc(terminal_at),
            terminal_state=terminal_state,
            reason_code=reason,
            executed_quantity=executed,
            unfilled_quantity=unfilled,
            engine_status=order.status,
            engine_status_reason=order.status_reason,
            cancellation_fee=None,
            acknowledgement_availability=self.acknowledgement_availability,
            timestamp_source="base_engine_terminal_timestamp",
        )
        self.service.record_terminal(authorization, observation)

    def _authorization_for(self, order_id: str) -> ExecutionAuthorization:
        with self._lock:
            authorization = self._by_order.get(order_id)
        if authorization is None:
            raise ExecutionBlockedError(f"unmatched order callback blocked: {order_id}")
        return authorization

    def _ensure_submission(self, order_id: str, authorization: ExecutionAuthorization) -> None:
        with self._lock:
            observation = self._submissions.get(order_id)
        if observation is not None:
            self.service.record_submission(authorization, observation)

    def _utc(self, value: Any) -> datetime:
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize(self.source_timezone)
        return timestamp.tz_convert("UTC").to_pydatetime()

    def _intent_participation(
        self,
        authorization: ExecutionAuthorization,
        order: OrderRecord,
    ) -> Decimal | None:
        del order
        with self._lock:
            intent = self._intent_by_decision.get(authorization.decision_id, {})
        value = intent.get("participation_limit")
        return Decimal(str(value)) if value is not None else None

    @staticmethod
    def _fill_id(fill: FillRecord, cumulative: Decimal) -> str:
        material = "\x1f".join(
            (
                fill.order_id,
                str(pd.Timestamp(fill.timestamp).value),
                str(fill.quantity),
                str(fill.fill_price),
                format(cumulative, "f"),
                str(fill.remaining_quantity),
                fill.event_type,
            )
        )
        return f"basefill_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _terminal_mapping(
        order: OrderRecord,
        executed: Decimal,
        unfilled: Decimal,
    ) -> tuple[TerminalState, TerminalReason]:
        if order.status == "filled" and unfilled == 0:
            return TerminalState.FULLY_FILLED, TerminalReason.FILLED
        if order.status == "expired":
            if executed > 0:
                return TerminalState.PARTIALLY_FILLED_EXPIRED, TerminalReason.EXPIRE_UNFILLED_REMAINDER
            return TerminalState.EXPIRED_UNFILLED, TerminalReason.EXPIRE_UNFILLED_REMAINDER
        if order.status == "rejected":
            return TerminalState.REJECTED_UNFILLED, TerminalReason.VENUE_REJECTED
        if executed > 0:
            return TerminalState.PARTIALLY_FILLED_CANCELLED, TerminalReason.CANCEL_UNFILLED_REMAINDER
        return TerminalState.CANCELLED_UNFILLED, TerminalReason.CANCEL_UNFILLED_REMAINDER
