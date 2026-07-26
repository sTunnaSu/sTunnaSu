"""Phase 8-gated Alpaca paper-order execution.

This module is the only supported bridge from a latency-budget ``ALLOW`` to an
Alpaca paper order.  It deliberately rejects every live/read-only/non-Alpaca
profile, persists the decision before submission, propagates the immutable
decision id as Alpaca's ``client_order_id``, and records only broker facts that
were actually returned by the paper endpoint.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, cast

from src.config.paths import get_runtime_root
from src.latency_budgeter.application.gate import LatencyBudgetDecisionGate
from src.latency_budgeter.application.lifecycle import ExecutionLifecycleService
from src.latency_budgeter.configuration.models import LatencyBudgetConfig
from src.latency_budgeter.domain.decisions import DecisionOutcome, LiquidityRole
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.latency_budgeter.domain.json_values import thaw_json
from src.latency_budgeter.domain.lifecycle import (
    AcknowledgementAvailability,
    AcknowledgementObservation,
    FillObservation,
    HandoffState,
    SubmissionObservation,
    TerminalObservation,
    TerminalReason,
    TerminalState,
)
from src.latency_budgeter.domain.models import MarketObservation, SignalSide, SourceMetadata
from src.latency_budgeter.domain.timestamps import normalize_timestamp, utc_iso
from src.latency_budgeter.domain.values import BasisPoints
from src.latency_budgeter.persistence.approved_memory import InMemoryApprovedOpportunityPort
from src.latency_budgeter.persistence.history_sqlite import SQLiteLatencyHistoryStore
from src.latency_budgeter.persistence.sqlite import SQLiteEventLedger
from src.latency_budgeter.ports.edge import GrossEdgeEstimate
from src.security.secret_redaction import redact_sensitive_text
from src.trading.profiles import profile_by_id
from src.trading.service import get_broker_clock, get_open_orders, get_quote, place_order

PAPER_PROFILE_ID = "alpaca-paper-trade"
DEFAULT_LEDGER_FILENAME = "alpaca-paper.sqlite3"

Clock = Callable[[], datetime]
QuoteReader = Callable[..., dict[str, Any]]
OrderSubmitter = Callable[..., dict[str, Any]]
OrdersReader = Callable[..., dict[str, Any]]
ClockReader = Callable[..., dict[str, Any]]
ProfileResolver = Callable[[str | None], Any]
BeforeSubmit = Callable[[Any], None]


def _positive_decimal(value: Any, *, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{label} must be positive and finite")
    return result


def _nonnegative_decimal(value: Any, *, label: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{label} must be non-negative and finite")
    return result


def _status_token(value: Any) -> str:
    return str(value or "").strip().lower().split(".")[-1]


def _optional_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or text.lower() == "none":
        return None
    try:
        return normalize_timestamp(text)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class Phase8PaperOrderRequest:
    """One strategy-owned paper opportunity presented to the Phase 8 gate."""

    symbol: str
    side: SignalSide | str
    quantity: Decimal | float | str
    gross_edge_bps: Decimal | float | str
    strategy_version: str
    strategy_requirements_met: bool
    submit: bool = False
    order_type: str = "market"
    limit_price: Decimal | float | str | None = None
    time_in_force: str = "gtc"
    signal_key: str = "default"
    signal_metadata: Mapping[str, Any] = field(default_factory=dict)
    poll_timeout_seconds: float = 8.0
    poll_interval_seconds: float = 0.5
    run_id: str | None = None

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper().replace("-", "/")
        if not symbol:
            raise ValueError("symbol is required")
        strategy_version = str(self.strategy_version or "").strip()
        signal_key = str(self.signal_key or "").strip()
        if not strategy_version or not signal_key:
            raise ValueError("strategy_version and signal_key are required")
        side = SignalSide(self.side)
        if side is SignalSide.FLAT:
            raise ValueError("paper orders require a buy or sell side")
        quantity = _positive_decimal(self.quantity, label="quantity")
        gross_edge = _nonnegative_decimal(self.gross_edge_bps, label="gross_edge_bps")
        order_type = str(self.order_type or "").strip().lower()
        if order_type not in {"market", "limit"}:
            raise ValueError("order_type must be market or limit")
        limit_price = None
        if self.limit_price is not None:
            limit_price = _positive_decimal(self.limit_price, label="limit_price")
        if order_type == "limit" and limit_price is None:
            raise ValueError("limit orders require limit_price")
        time_in_force = str(self.time_in_force or "").strip().lower()
        if time_in_force not in {"gtc", "ioc"}:
            raise ValueError("Alpaca crypto paper orders require gtc or ioc")
        timeout = float(self.poll_timeout_seconds)
        interval = float(self.poll_interval_seconds)
        if timeout < 0 or timeout > 30:
            raise ValueError("poll_timeout_seconds must be between 0 and 30")
        if interval <= 0 or interval > 5:
            raise ValueError("poll_interval_seconds must be between 0 and 5")
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "gross_edge_bps", gross_edge)
        object.__setattr__(self, "strategy_version", strategy_version)
        object.__setattr__(self, "signal_key", signal_key)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "limit_price", limit_price)
        object.__setattr__(self, "time_in_force", time_in_force)
        object.__setattr__(self, "signal_metadata", dict(self.signal_metadata))
        object.__setattr__(self, "poll_timeout_seconds", timeout)
        object.__setattr__(self, "poll_interval_seconds", interval)


@dataclass(slots=True)
class _DeclaredEdgeEstimator:
    gross_edge_bps: Decimal
    reference_price: Decimal
    estimator_version: str

    def estimate(self, *, observation, signal, decision_at) -> GrossEdgeEstimate:  # noqa: ANN001
        return GrossEdgeEstimate(
            gross_edge_bps=BasisPoints(self.gross_edge_bps),
            estimated_at=decision_at,
            estimator_version=self.estimator_version,
            reference_price=self.reference_price,
        )


class Phase8AlpacaPaperExecutionService:
    """Persist a Phase 8 decision and execute only immutable ``ALLOW`` results."""

    def __init__(
        self,
        *,
        config: LatencyBudgetConfig,
        ledger: Any,
        history: Any,
        profile_id: str = PAPER_PROFILE_ID,
        clock: Clock | None = None,
        quote_reader: QuoteReader = get_quote,
        clock_reader: ClockReader = get_broker_clock,
        order_submitter: OrderSubmitter = place_order,
        orders_reader: OrdersReader = get_open_orders,
        profile_resolver: ProfileResolver = profile_by_id,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not config.enabled:
            raise ValueError("Phase 8 paper execution requires enabled=True")
        self.base_config = config
        self.ledger = ledger
        self.history = history
        self.profile_id = str(profile_id)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.quote_reader = quote_reader
        self.clock_reader = clock_reader
        self.order_submitter = order_submitter
        self.orders_reader = orders_reader
        self.profile_resolver = profile_resolver
        self.sleeper = sleeper
        self.monotonic = monotonic

    def execute(
        self,
        request: Phase8PaperOrderRequest,
        *,
        before_submit: BeforeSubmit | None = None,
    ) -> dict[str, Any]:
        """Evaluate, optionally submit, then persist actual broker callbacks."""
        profile_error = self._profile_error()
        if profile_error is not None:
            return profile_error

        # ``Phase8PaperOrderRequest.__post_init__`` normalizes these union-typed
        # public inputs into their canonical domain types.
        side = cast(SignalSide, request.side)
        quantity = cast(Decimal, request.quantity)
        gross_edge_bps = cast(Decimal, request.gross_edge_bps)
        limit_price = cast(Decimal | None, request.limit_price)

        clock_error, calibrated_clock, clock_evidence = self._calibrated_clock()
        if clock_error is not None:
            return clock_error

        try:
            quote_result = self.quote_reader(request.symbol, self.profile_id)
        except Exception as exc:  # noqa: BLE001 - connector failures are result data
            return self._error("quote_unavailable", str(exc))
        quote_error, quote = self._validated_quote(quote_result)
        if quote_error is not None:
            return quote_error

        bid = _positive_decimal(quote["bid"], label="bid")
        ask = _positive_decimal(quote["ask"], label="ask")
        mid = (bid + ask) / Decimal("2")
        spread_bps = ((ask - bid) / mid) * Decimal("10000")
        config = self._config_with_observed_spread(spread_bps)
        captured_at = calibrated_clock()
        observed_at = normalize_timestamp(str(quote["time"]))
        run_id = request.run_id or Phase8IdentifierFactory.new_run_id()
        observation = MarketObservation.from_input(
            source="alpaca-paper-latest-quote",
            observed_at=observed_at,
            source_capture_at=captured_at,
            timezone="UTC",
            symbol=request.symbol,
            side=request.side,
            strategy_version=request.strategy_version,
            source_metadata=SourceMetadata(
                provider="alpaca",
                feed=str(quote_result.get("feed") or "paper-market-data"),
                venue="alpaca-paper",
                source_event_id=str(quote.get("source_event_id") or ""),
                capture_method="latest_quote_sdk",
                attributes=clock_evidence,
            ),
            raw_payload={
                "bid": str(bid),
                "ask": str(ask),
                "bid_size": quote.get("bid_size"),
                "ask_size": quote.get("ask_size"),
            },
        )
        approved = InMemoryApprovedOpportunityPort()
        gate = LatencyBudgetDecisionGate(
            config=config,
            ledger=self.ledger,
            history=self.history,
            edge_estimator=_DeclaredEdgeEstimator(
                gross_edge_bps=gross_edge_bps,
                reference_price=mid,
                estimator_version=f"{request.strategy_version}:declared-point-in-time-edge",
            ),
            approved_port=approved,
            clock=calibrated_clock,
            clock_source="alpaca_paper_clock_calibrated_utc",
        )
        gate_result = gate.evaluate(
            observation=observation,
            run_id=run_id,
            strategy_requirements_met=request.strategy_requirements_met,
            liquidity_role=(LiquidityRole.MAKER if request.order_type == "limit" else LiquidityRole.TAKER),
            signal_key=request.signal_key,
            signal_metadata={
                **dict(request.signal_metadata),
                "paper_only": True,
                "requested_quantity": str(quantity),
                "requested_order_type": request.order_type,
            },
        )
        result = self._decision_result(gate_result, config, spread_bps, mid)
        result["clock_calibration"] = clock_evidence
        if gate_result.outcome is not DecisionOutcome.ALLOW:
            result["execution_status"] = "blocked_by_phase8"
            return self._with_events(result, gate_result.event.decision_id)
        if not request.submit:
            result["execution_status"] = "allow_dry_run"
            return self._with_events(result, gate_result.event.decision_id)

        lifecycle = ExecutionLifecycleService(
            config=config,
            ledger=self.ledger,
            history=self.history,
            clock=calibrated_clock,
            clock_source="alpaca_paper_clock_calibrated_utc",
        )
        authorization = lifecycle.authorize(gate_result.event.decision_id)
        if authorization.state is HandoffState.ALREADY_SUBMITTED:
            result.update(
                execution_status="already_submitted",
                order_id=authorization.submitted_order_id,
                client_order_id=authorization.client_order_id,
            )
            return self._with_events(result, authorization.decision_id)

        # The composition root uses this fail-closed boundary to persist the
        # exact deterministic decision/client-order identity before any broker
        # mutation.  An exception aborts before ``order_submitter`` is called.
        if before_submit is not None:
            before_submit(authorization)

        try:
            response = self.order_submitter(
                request.symbol,
                self.profile_id,
                side=side.value,
                quantity=float(quantity),
                notional=None,
                order_type=request.order_type,
                limit_price=(float(limit_price) if limit_price is not None else None),
                time_in_force=request.time_in_force,
                client_order_id=authorization.client_order_id,
            )
        except Exception as exc:  # noqa: BLE001 - timeout may be an accepted order
            result.update(
                execution_status="submission_ambiguous",
                broker_error=str(exc),
                client_order_id=authorization.client_order_id,
                reconciliation_required=True,
            )
            return self._with_events(result, authorization.decision_id)
        response_received_at = calibrated_clock()
        if response.get("status") != "ok":
            result.update(
                execution_status="broker_submission_failed",
                broker_error=str(response.get("error") or "paper order submission failed"),
            )
            return self._with_events(result, authorization.decision_id)
        if response.get("environment") != "paper" or response.get("is_paper") is not True:
            result.update(
                execution_status="paper_environment_proof_missing",
                broker_error="connector response did not prove Alpaca paper execution",
            )
            return self._with_events(result, authorization.decision_id)

        order_id = str(response.get("order_id") or "").strip()
        returned_client_id = str(response.get("client_order_id") or "").strip()
        if not order_id or returned_client_id != authorization.client_order_id:
            result.update(
                execution_status="broker_correlation_failed",
                broker_error="paper response did not preserve the Phase 8 client_order_id",
                order_id=order_id or None,
            )
            return self._with_events(result, authorization.decision_id)

        submitted_at = _optional_timestamp(response.get("submitted_at")) or response_received_at
        lifecycle.record_submission(
            authorization,
            SubmissionObservation(
                order_id=order_id,
                client_order_id=authorization.client_order_id,
                submitted_at=submitted_at,
                quantity=quantity,
                order_type=request.order_type,
                limit_price=limit_price,
                venue_reference="alpaca-paper",
                timestamp_source=(
                    "alpaca_submitted_at" if response.get("submitted_at") else "alpaca_sdk_response_received"
                ),
            ),
        )
        lifecycle.record_acknowledgement(
            authorization,
            AcknowledgementObservation(
                order_id=order_id,
                acknowledged_at=response_received_at,
                acknowledgement_id=f"alpaca-submit-response:{order_id}",
                venue_reference=_status_token(response.get("order_status")),
                timestamp_source="alpaca_sdk_response_received",
            ),
        )

        result.update(
            execution_status="submitted",
            order_id=order_id,
            client_order_id=authorization.client_order_id,
            broker_order_status=_status_token(response.get("order_status")),
        )
        snapshot = self._wait_for_snapshot(request, order_id, response)
        reconciliation = self._record_snapshot(
            lifecycle=lifecycle,
            authorization=authorization,
            side=side,
            submitted_quantity=quantity,
            snapshot=snapshot,
            decision_price=mid,
        )
        result.update(reconciliation)
        return self._with_events(result, authorization.decision_id)

    def reconcile(self, decision_id: str) -> dict[str, Any]:
        """Resume one persisted submitted paper order without resubmitting it."""
        profile_error = self._profile_error()
        if profile_error is not None:
            return profile_error
        events = self.ledger.read(str(decision_id))
        if not events:
            return self._error("decision_not_found", f"unknown Phase 8 decision: {decision_id}")
        root_payload = events[0].payload
        raw_config = root_payload.get("phase8_config")
        if not isinstance(raw_config, Mapping):
            return self._error("decision_config_missing", "persisted Phase 8 config is missing")
        config = LatencyBudgetConfig.model_validate(thaw_json(raw_config))
        clock_error, calibrated_clock, clock_evidence = self._calibrated_clock()
        if clock_error is not None:
            return clock_error
        lifecycle = ExecutionLifecycleService(
            config=config,
            ledger=self.ledger,
            history=self.history,
            clock=calibrated_clock,
            clock_source="alpaca_paper_clock_calibrated_utc",
        )
        authorization = lifecycle.authorize(str(decision_id))
        projection = lifecycle.projection(str(decision_id))
        try:
            response = self.orders_reader(self.profile_id, include_executions=True)
        except Exception as exc:  # noqa: BLE001
            return self._error("order_snapshot_unavailable", str(exc))
        rows = (*response.get("open_orders", []), *response.get("executions", []))
        if authorization.state is HandoffState.READY:
            # A timeout can occur after Alpaca accepted the deterministic
            # client_order_id but before the local submission event was
            # appended.  Query by that identity exactly once; never resubmit.
            recovered = next(
                (row for row in rows if str(row.get("client_order_id") or "") == authorization.client_order_id),
                None,
            )
            if recovered is None:
                return self._error(
                    "ambiguous_submission_unresolved",
                    "no Alpaca paper order matches the deterministic client_order_id",
                )
            recovered_order_id = str(recovered.get("order_id") or "").strip()
            if not recovered_order_id:
                return self._error(
                    "ambiguous_submission_unresolved",
                    "matching Alpaca paper order has no broker order_id",
                )
            recovered_quantity = _positive_decimal(
                recovered.get("quantity"),
                label="recovered submitted quantity",
            )
            recovered_submitted_at = _optional_timestamp(recovered.get("submitted_at")) or calibrated_clock()
            lifecycle.record_submission(
                authorization,
                SubmissionObservation(
                    order_id=recovered_order_id,
                    client_order_id=authorization.client_order_id,
                    submitted_at=recovered_submitted_at,
                    quantity=recovered_quantity,
                    order_type=_status_token(recovered.get("order_type")) or "market",
                    limit_price=(
                        _positive_decimal(recovered.get("limit_price"), label="recovered limit price")
                        if recovered.get("limit_price") not in (None, "")
                        else None
                    ),
                    venue_reference="alpaca-paper-recovered-by-client-order-id",
                    timestamp_source=(
                        "alpaca_submitted_at" if recovered.get("submitted_at") else "alpaca_reconciliation_observed_at"
                    ),
                ),
            )
            lifecycle.record_acknowledgement(
                authorization,
                AcknowledgementObservation(
                    order_id=recovered_order_id,
                    acknowledged_at=calibrated_clock(),
                    acknowledgement_id=f"alpaca-recovered:{recovered_order_id}",
                    venue_reference=_status_token(recovered.get("status")),
                    timestamp_source="alpaca_reconciliation_observed_at",
                ),
            )
            authorization = lifecycle.authorize(str(decision_id))
            projection = lifecycle.projection(str(decision_id))
        if authorization.state is not HandoffState.ALREADY_SUBMITTED or not projection.order_id:
            return self._error("order_not_submitted", "decision has no persisted paper submission")
        if projection.submitted_quantity is None:
            return self._error("submitted_quantity_missing", "persisted submission quantity is missing")
        snapshot = next(
            (row for row in rows if str(row.get("order_id") or "") == projection.order_id),
            None,
        )
        if snapshot is None:
            return self._error(
                "order_snapshot_missing",
                f"Alpaca did not return order {projection.order_id} in open or closed orders",
            )
        edge_evidence = root_payload.get("gross_edge_evidence")
        if not isinstance(edge_evidence, Mapping) or not edge_evidence.get("reference_price"):
            return self._error("decision_price_missing", "persisted decision reference price is missing")
        decision_price = _positive_decimal(
            edge_evidence["reference_price"],
            label="decision reference price",
        )
        result = {
            "status": "ok",
            "paper_only": True,
            "profile_id": PAPER_PROFILE_ID,
            "decision_id": authorization.decision_id,
            "run_id": authorization.run_id,
            "signal_id": authorization.signal_id,
            "order_id": projection.order_id,
            "client_order_id": authorization.client_order_id,
            "decision_persisted": True,
            "order_submitted": True,
            "clock_calibration": clock_evidence,
        }
        result.update(
            self._record_snapshot(
                lifecycle=lifecycle,
                authorization=authorization,
                side=authorization.side,
                submitted_quantity=projection.submitted_quantity,
                snapshot=snapshot,
                decision_price=decision_price,
            )
        )
        return self._with_events(result, authorization.decision_id)

    def _profile_error(self) -> dict[str, Any] | None:
        try:
            profile = self.profile_resolver(self.profile_id)
        except Exception as exc:  # noqa: BLE001
            return self._error("profile_unavailable", str(exc))
        if (
            profile.id != PAPER_PROFILE_ID
            or profile.connector != "alpaca"
            or profile.environment != "paper"
            or profile.readonly
        ):
            return self._error(
                "paper_profile_required",
                f"Phase 8 paper execution accepts only {PAPER_PROFILE_ID}",
            )
        return None

    def _calibrated_clock(
        self,
    ) -> tuple[dict[str, Any] | None, Clock, dict[str, Any]]:
        """Calibrate local UTC to Alpaca's clock without weakening timestamp checks."""
        monotonic_before = self.monotonic()
        try:
            result = self.clock_reader(self.profile_id)
        except Exception as exc:  # noqa: BLE001
            return self._error("clock_sync_unavailable", str(exc)), self.clock, {}
        monotonic_after = self.monotonic()
        local_after = normalize_timestamp(self.clock())
        if result.get("status") != "ok" or result.get("environment") != "paper" or result.get("is_paper") is not True:
            return (
                self._error(
                    "clock_sync_unavailable",
                    str(result.get("error") or "Alpaca paper clock proof is missing"),
                ),
                self.clock,
                {},
            )
        try:
            broker_at = normalize_timestamp(str(result.get("timestamp") or ""))
        except (TypeError, ValueError) as exc:
            return self._error("clock_sync_invalid", str(exc)), self.clock, {}
        # The broker timestamp may be sampled near request receipt while the
        # response arrives one network round-trip later.  Using the midpoint
        # can therefore place our decision clock ahead of a later broker
        # ``submitted_at``.  The response-time lower bound is conservative: it
        # may overstate latency by at most the measured RTT, but it cannot turn
        # an order submitted after the decision into an apparent pre-decision
        # submission merely because of calibration uncertainty.
        offset = broker_at - local_after
        round_trip_ms = max(0.0, (monotonic_after - monotonic_before) * 1_000)
        if round_trip_ms > 5_000:
            return (
                self._error("clock_sync_too_slow", "Alpaca clock calibration exceeded 5000 ms"),
                self.clock,
                {},
            )

        def calibrated() -> datetime:
            return normalize_timestamp(self.clock()) + offset

        evidence = {
            "clock_source": "alpaca_trading_clock",
            "clock_offset_ms": round(offset.total_seconds() * 1_000, 3),
            "calibration_round_trip_ms": round(round_trip_ms, 3),
            "calibration_uncertainty_ms": round(round_trip_ms, 3),
            "calibration_policy": "conservative_response_time_lower_bound",
            "calibrated_at": utc_iso(calibrated()),
        }
        return None, calibrated, evidence

    @staticmethod
    def _validated_quote(
        result: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, Mapping[str, Any]]:
        if result.get("status") != "ok" or not isinstance(result.get("quote"), Mapping):
            return (
                Phase8AlpacaPaperExecutionService._error(
                    "quote_unavailable", str(result.get("error") or "quote response is invalid")
                ),
                {},
            )
        quote = result["quote"]
        try:
            bid = _positive_decimal(quote.get("bid"), label="bid")
            ask = _positive_decimal(quote.get("ask"), label="ask")
            observed_at = normalize_timestamp(str(quote.get("time") or ""))
        except (TypeError, ValueError) as exc:
            return Phase8AlpacaPaperExecutionService._error("quote_invalid", str(exc)), {}
        if ask < bid:
            return Phase8AlpacaPaperExecutionService._error("quote_crossed", "ask is below bid"), {}
        return None, {**dict(quote), "bid": bid, "ask": ask, "time": utc_iso(observed_at)}

    def _config_with_observed_spread(self, observed_spread_bps: Decimal) -> LatencyBudgetConfig:
        raw = self.base_config.model_dump(mode="json")
        costs = dict(raw["cost_assumptions"])
        costs["spread_bps"] = max(float(costs.get("spread_bps", 0)), float(observed_spread_bps))
        raw["cost_assumptions"] = costs
        return LatencyBudgetConfig.model_validate(raw)

    @staticmethod
    def _decision_result(gate_result, config, spread_bps, mid) -> dict[str, Any]:  # noqa: ANN001
        economics = gate_result.event.payload.get("decision_economics")
        return {
            "status": "ok",
            "paper_only": True,
            "profile_id": PAPER_PROFILE_ID,
            "run_id": gate_result.event.run_id,
            "signal_id": gate_result.event.signal_id,
            "decision_id": gate_result.event.decision_id,
            "phase8_outcome": gate_result.outcome.value,
            "phase8_reason": gate_result.reason.value,
            "decision_persisted": True,
            "order_submitted": False,
            "observed_spread_bps": float(spread_bps),
            "decision_reference_price": str(mid),
            "config_fingerprint": config.fingerprint,
            "decision_economics": thaw_json(economics) if isinstance(economics, Mapping) else None,
        }

    def _wait_for_snapshot(
        self,
        request: Phase8PaperOrderRequest,
        order_id: str,
        submission: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        initial = self._submission_snapshot(submission)
        if self._is_terminal(initial) or request.poll_timeout_seconds == 0:
            return initial
        deadline = self.monotonic() + request.poll_timeout_seconds
        latest = initial
        while self.monotonic() < deadline:
            self.sleeper(request.poll_interval_seconds)
            try:
                response = self.orders_reader(self.profile_id, include_executions=True)
            except Exception:  # noqa: BLE001 - retain last real snapshot
                continue
            for row in (*response.get("open_orders", []), *response.get("executions", [])):
                if str(row.get("order_id") or "") == order_id:
                    latest = row
                    if self._is_terminal(latest):
                        return latest
        return latest

    @staticmethod
    def _submission_snapshot(submission: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "order_id": submission.get("order_id"),
            "client_order_id": submission.get("client_order_id"),
            "status": submission.get("order_status"),
            "quantity": submission.get("quantity"),
            "filled_qty": submission.get("filled_qty"),
            "filled_avg_price": submission.get("filled_avg_price"),
            "submitted_at": submission.get("submitted_at"),
            "filled_at": submission.get("filled_at"),
            "updated_at": submission.get("updated_at"),
        }

    @staticmethod
    def _is_terminal(snapshot: Mapping[str, Any]) -> bool:
        return _status_token(snapshot.get("status")) in {
            "filled",
            "canceled",
            "cancelled",
            "expired",
            "rejected",
            "failed",
        }

    def _record_snapshot(
        self,
        *,
        lifecycle: ExecutionLifecycleService,
        authorization: Any,
        side: SignalSide,
        submitted_quantity: Decimal,
        snapshot: Mapping[str, Any],
        decision_price: Decimal,
    ) -> dict[str, Any]:
        status = _status_token(snapshot.get("status"))
        filled = _nonnegative_decimal(snapshot.get("filled_qty") or 0, label="filled_qty")
        fill_price_raw = snapshot.get("filled_avg_price")
        fill_at = _optional_timestamp(snapshot.get("filled_at"))
        fill_recorded = False
        if filled > 0 and fill_price_raw not in (None, "") and fill_at is not None:
            fill_price = _positive_decimal(fill_price_raw, label="filled_avg_price")
            unfilled = max(Decimal("0"), submitted_quantity - filled)
            fill_material = f"{authorization.decision_id}|{filled}|{fill_price}|{utc_iso(fill_at)}"
            fill_id = "alpaca-aggregate-" + hashlib.sha256(fill_material.encode()).hexdigest()[:24]
            lifecycle.record_fill(
                authorization,
                FillObservation(
                    order_id=str(snapshot.get("order_id") or authorization.submitted_order_id),
                    fill_id=fill_id,
                    fill_at=fill_at,
                    side=side,
                    quantity=filled,
                    price=fill_price,
                    cumulative_filled_quantity=filled,
                    unfilled_quantity=unfilled,
                    venue_reference="alpaca_order_aggregate_snapshot",
                    decision_price=decision_price,
                    timestamp_source="alpaca_filled_at",
                ),
            )
            fill_recorded = True

        terminal_recorded = False
        if self._is_terminal(snapshot):
            terminal_at = self._terminal_timestamp(snapshot, status)
            if terminal_at is not None and (filled == 0 or fill_recorded):
                terminal_state, reason = self._terminal_mapping(status, filled, submitted_quantity)
                lifecycle.record_terminal(
                    authorization,
                    TerminalObservation(
                        order_id=str(snapshot.get("order_id") or authorization.submitted_order_id),
                        terminal_at=terminal_at,
                        terminal_state=terminal_state,
                        reason_code=reason,
                        executed_quantity=filled,
                        unfilled_quantity=max(Decimal("0"), submitted_quantity - filled),
                        engine_status=status,
                        acknowledgement_availability=AcknowledgementAvailability.RECEIVED,
                        venue_reference="alpaca-paper",
                        timestamp_source="alpaca_order_snapshot",
                    ),
                )
                terminal_recorded = True

        if terminal_recorded:
            execution_status = "filled" if status == "filled" else status
        elif filled > 0:
            execution_status = "partially_filled_pending" if fill_recorded else "fill_evidence_incomplete"
        else:
            execution_status = "submitted_pending"
        return {
            "execution_status": execution_status,
            "broker_order_status": status,
            "filled_quantity": str(filled),
            "filled_average_price": (str(fill_price_raw) if fill_price_raw not in (None, "") else None),
            "fill_recorded": fill_recorded,
            "terminal_recorded": terminal_recorded,
            "order_submitted": True,
        }

    @staticmethod
    def _terminal_timestamp(snapshot: Mapping[str, Any], status: str) -> datetime | None:
        keys = {
            "filled": ("filled_at", "updated_at"),
            "canceled": ("canceled_at", "updated_at"),
            "cancelled": ("canceled_at", "updated_at"),
            "expired": ("expired_at", "updated_at"),
            "rejected": ("failed_at", "updated_at"),
            "failed": ("failed_at", "updated_at"),
        }.get(status, ("updated_at",))
        for key in keys:
            if value := _optional_timestamp(snapshot.get(key)):
                return value
        return None

    @staticmethod
    def _terminal_mapping(
        status: str,
        filled: Decimal,
        requested: Decimal,
    ) -> tuple[TerminalState, TerminalReason]:
        if status == "filled" and filled >= requested:
            return TerminalState.FULLY_FILLED, TerminalReason.FILLED
        if status == "expired":
            if filled > 0:
                return TerminalState.PARTIALLY_FILLED_EXPIRED, TerminalReason.EXPIRE_UNFILLED_REMAINDER
            return TerminalState.EXPIRED_UNFILLED, TerminalReason.EXPIRE_UNFILLED_REMAINDER
        if status in {"rejected", "failed"}:
            return TerminalState.REJECTED_UNFILLED, TerminalReason.VENUE_REJECTED
        if filled > 0:
            return TerminalState.PARTIALLY_FILLED_CANCELLED, TerminalReason.CANCEL_UNFILLED_REMAINDER
        return TerminalState.CANCELLED_UNFILLED, TerminalReason.CANCEL_UNFILLED_REMAINDER

    def _with_events(self, result: dict[str, Any], decision_id: str) -> dict[str, Any]:
        events = self.ledger.read(decision_id)
        result["persisted_event_count"] = len(events)
        result["persisted_event_types"] = [event.event_type.value for event in events]
        return result

    @staticmethod
    def _error(code: str, message: str) -> dict[str, Any]:
        return {
            "status": "error",
            "error_code": code,
            "error": redact_sensitive_text(message),
            "paper_only": True,
            "order_submitted": False,
        }


def default_phase8_paper_ledger_path() -> Path:
    """Return the canonical durable Phase 8 Alpaca-paper evidence database."""
    return get_runtime_root() / "phase8" / DEFAULT_LEDGER_FILENAME


def execute_phase8_alpaca_paper_order(
    request: Phase8PaperOrderRequest,
    *,
    config: LatencyBudgetConfig,
    database_path: str | Path | None = None,
    before_submit: BeforeSubmit | None = None,
) -> dict[str, Any]:
    """Run one durable paper decision and close both SQLite connections."""
    path = Path(database_path) if database_path is not None else default_phase8_paper_ledger_path()
    ledger = SQLiteEventLedger(path)
    history = SQLiteLatencyHistoryStore(path)
    try:
        result = Phase8AlpacaPaperExecutionService(
            config=config,
            ledger=ledger,
            history=history,
        ).execute(request, before_submit=before_submit)
        result["ledger_path"] = str(path)
        return result
    finally:
        history.close()
        ledger.close()


def reconcile_phase8_alpaca_paper_order(
    decision_id: str,
    *,
    database_path: str | Path | None = None,
) -> dict[str, Any]:
    """Restart-safe reconciliation for an already-submitted paper order."""
    path = Path(database_path) if database_path is not None else default_phase8_paper_ledger_path()
    ledger = SQLiteEventLedger(path)
    history = SQLiteLatencyHistoryStore(path)
    try:
        # The persisted root supplies the effective immutable configuration.
        placeholder = LatencyBudgetConfig(enabled=True)
        result = Phase8AlpacaPaperExecutionService(
            config=placeholder,
            ledger=ledger,
            history=history,
        ).reconcile(decision_id)
        result["ledger_path"] = str(path)
        return result
    finally:
        history.close()
        ledger.close()
