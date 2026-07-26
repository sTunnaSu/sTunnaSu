"""Frozen contracts shared by the Phase 8 runtime.

These models keep research, strategy, risk, latency and execution state
explicit.  They contain no broker methods and no credentials.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


def canonical_hash(value: Any) -> str:
    """Hash a JSON-compatible value with deterministic encoding."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FrozenModel(BaseModel):
    """Strict immutable Pydantic base."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class RuntimeMode(str, Enum):
    RESEARCH_ONLY = "research_only"
    DRY_RUN = "dry_run"
    PAPER_EXECUTE = "paper_execute"


class ReleaseDecision(str, Enum):
    NO_GO = "NO_GO"
    GO_FOR_RESEARCH_ONLY = "GO_FOR_RESEARCH_ONLY"
    GO_FOR_DRY_RUN = "GO_FOR_DRY_RUN"
    GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST = "GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST"
    GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST = "GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST"
    GO_FOR_BOUNDED_PAPER_TEST = "GO_FOR_BOUNDED_PAPER_TEST"
    GO_WITH_EXPLICIT_LIMITATIONS = "GO_WITH_EXPLICIT_LIMITATIONS"


class ModuleState(str, Enum):
    VALID = "VALID"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    ERROR = "ERROR"


class ModuleRequirement(str, Enum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NOT_APPLICABLE = "not_applicable"


class StrategyState(str, Enum):
    ACCEPTED_PAPER = "ACCEPTED_PAPER"
    SHADOW = "SHADOW"
    EXPERIMENTAL_PAPER = "EXPERIMENTAL_PAPER"
    DEMOTED = "DEMOTED"
    REJECTED = "REJECTED"


class ExistingPositionPolicy(str, Enum):
    MANAGE = "manage"
    RISK_ONLY = "risk_only"
    FLATTEN_BEFORE_START = "flatten_before_start"
    REJECT_START = "reject_start"


class SignalDirection(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


class IntentKind(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    PROTECTIVE_EXIT = "protective_exit"


class IntentState(str, Enum):
    INTENT_CREATED = "INTENT_CREATED"
    VALIDATED = "VALIDATED"
    DRY_RUN = "DRY_RUN"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    AMBIGUOUS = "AMBIGUOUS"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CLOSED = "CLOSED"


class CheckStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class Phase8ValidationProfile(FrozenModel):
    """Locked defaults for the named bounded validation profile."""

    name: str = "phase8-bounded-validation-v1"
    internal_capital_usd: Decimal = Decimal("1000")
    maximum_position_value_usd: Decimal = Decimal("150")
    maximum_gross_exposure_usd: Decimal = Decimal("300")
    maximum_concurrent_positions: int = 2
    maximum_completed_round_trips: int = 20
    maximum_entry_orders: int = 20
    maximum_exit_orders: int = 20
    maximum_total_submitted_orders: int = 50
    session_loss_stop_usd: Decimal = Decimal("20")
    hard_drawdown_fraction: Decimal = Decimal("0.05")
    minimum_unallocated_capital_usd: Decimal = Decimal("700")
    existing_position_policy: ExistingPositionPolicy = ExistingPositionPolicy.RISK_ONLY

    @model_validator(mode="after")
    def _validate_limits(self) -> "Phase8ValidationProfile":
        positive_decimals = (
            self.internal_capital_usd,
            self.maximum_position_value_usd,
            self.maximum_gross_exposure_usd,
            self.session_loss_stop_usd,
            self.minimum_unallocated_capital_usd,
        )
        if any(value <= 0 or not value.is_finite() for value in positive_decimals):
            raise ValueError("validation-profile monetary limits must be positive and finite")
        if not Decimal("0") < self.hard_drawdown_fraction < Decimal("1"):
            raise ValueError("hard_drawdown_fraction must be between zero and one")
        if (
            min(
                self.maximum_concurrent_positions,
                self.maximum_completed_round_trips,
                self.maximum_entry_orders,
                self.maximum_exit_orders,
                self.maximum_total_submitted_orders,
            )
            <= 0
        ):
            raise ValueError("validation-profile count limits must be positive")
        if self.maximum_gross_exposure_usd > (self.internal_capital_usd - self.minimum_unallocated_capital_usd):
            raise ValueError("gross exposure would violate the mandatory cash reserve")
        if self.maximum_position_value_usd * self.maximum_concurrent_positions > self.maximum_gross_exposure_usd:
            raise ValueError("concurrent maximum positions exceed gross exposure")
        if self.name == "phase8-bounded-validation-v1":
            locked = {
                "internal_capital_usd": Decimal("1000"),
                "maximum_position_value_usd": Decimal("150"),
                "maximum_gross_exposure_usd": Decimal("300"),
                "maximum_concurrent_positions": 2,
                "maximum_completed_round_trips": 20,
                "maximum_entry_orders": 20,
                "maximum_exit_orders": 20,
                "maximum_total_submitted_orders": 50,
                "session_loss_stop_usd": Decimal("20"),
                "hard_drawdown_fraction": Decimal("0.05"),
                "minimum_unallocated_capital_usd": Decimal("700"),
                "existing_position_policy": ExistingPositionPolicy.RISK_ONLY,
            }
            changed = [name for name, expected in locked.items() if getattr(self, name) != expected]
            if changed:
                raise ValueError("named Phase 8 validation profile is locked: " + ",".join(changed))
        return self


class ExperimentalRiskProfile(FrozenModel):
    maximum_simultaneous_positions: int = 1
    maximum_position_value_usd: Decimal = Decimal("50")
    maximum_aggregate_exposure_usd: Decimal = Decimal("50")
    maximum_completed_round_trips: int = 5
    maximum_realized_loss_usd: Decimal = Decimal("5")
    maximum_unresolved_orders: int = 0
    maximum_critical_rule_violations: int = 0

    @model_validator(mode="after")
    def _bounded_experimental_limits(self) -> "ExperimentalRiskProfile":
        if not 0 < self.maximum_simultaneous_positions <= 1:
            raise ValueError("experimental positions must be capped at one")
        for value, cap, label in (
            (self.maximum_position_value_usd, Decimal("50"), "position value"),
            (self.maximum_aggregate_exposure_usd, Decimal("50"), "aggregate exposure"),
            (self.maximum_realized_loss_usd, Decimal("5"), "realized loss"),
        ):
            if value <= 0 or not value.is_finite() or value > cap:
                raise ValueError(f"experimental {label} exceeds its locked cap")
        if not 0 < self.maximum_completed_round_trips <= 5:
            raise ValueError("experimental round trips must be between one and five")
        if self.maximum_unresolved_orders != 0 or self.maximum_critical_rule_violations != 0:
            raise ValueError("experimental unresolved orders and critical violations must remain zero")
        return self


class LatencyRequirements(FrozenModel):
    maximum_quote_age_ms: int = 5_000
    maximum_bar_age_ms: int = 120_000
    maximum_feature_age_ms: int = 5_000
    maximum_signal_age_ms: int = 5_000
    maximum_decision_to_submit_ms: int = 2_000
    acknowledgement_expectation_ms: int = 2_000
    expected_holding_period_minutes: int = 60
    sensitivity_class: Literal["low", "medium", "high"] = "medium"
    permitted_execution_style: Literal["market", "limit", "either"] = "either"

    @model_validator(mode="after")
    def _positive(self) -> "LatencyRequirements":
        values = (
            self.maximum_quote_age_ms,
            self.maximum_bar_age_ms,
            self.maximum_feature_age_ms,
            self.maximum_signal_age_ms,
            self.maximum_decision_to_submit_ms,
            self.acknowledgement_expectation_ms,
            self.expected_holding_period_minutes,
        )
        if min(values) <= 0:
            raise ValueError("latency requirements must be positive")
        return self


class StrategyRiskRules(FrozenModel):
    maximum_position_value_usd: Decimal = Decimal("150")
    maximum_loss_per_trade_usd: Decimal = Decimal("2")
    maximum_open_positions: int = 1
    maximum_completed_round_trips: int = 20
    permitted_regimes: tuple[str, ...] = ("bull", "bear", "sideways", "high_vol", "low_vol")

    @model_validator(mode="after")
    def _valid_strategy_risk(self) -> "StrategyRiskRules":
        if any(
            value <= 0 or not value.is_finite()
            for value in (self.maximum_position_value_usd, self.maximum_loss_per_trade_usd)
        ):
            raise ValueError("strategy monetary risk limits must be positive and finite")
        if min(self.maximum_open_positions, self.maximum_completed_round_trips) <= 0:
            raise ValueError("strategy count limits must be positive")
        if not self.permitted_regimes:
            raise ValueError("strategy must declare at least one permitted regime")
        return self


class ExecutableRules(FrozenModel):
    """Small auditable rule vocabulary used by the built-in evaluator."""

    family: Literal["trend", "momentum", "mean_reversion"]
    fast_window: int = 5
    slow_window: int = 20
    entry_threshold_bps: Decimal = Decimal("10")
    exit_threshold_bps: Decimal = Decimal("0")
    stop_loss_bps: Decimal = Decimal("100")
    take_profit_bps: Decimal = Decimal("200")
    trailing_stop_bps: Decimal | None = None
    maximum_holding_cycles: int = 60
    long_only: bool = True

    @model_validator(mode="after")
    def _valid_rules(self) -> "ExecutableRules":
        if self.fast_window < 2 or self.slow_window <= self.fast_window:
            raise ValueError("slow_window must be greater than fast_window >= 2")
        if self.entry_threshold_bps < 0 or self.stop_loss_bps <= 0 or self.take_profit_bps <= 0:
            raise ValueError("strategy thresholds and protective exits are invalid")
        if self.trailing_stop_bps is not None and self.trailing_stop_bps <= 0:
            raise ValueError("trailing_stop_bps must be positive when supplied")
        if self.maximum_holding_cycles <= 0:
            raise ValueError("maximum_holding_cycles must be positive")
        return self


class StrategySpecification(FrozenModel):
    """Immutable machine-readable accepted or generated strategy version."""

    strategy_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    parent_ids: tuple[str, ...] = ()
    created_at: datetime
    creator_component: str
    code_revision: str
    configuration_hash: str
    hypothesis: str
    edge_rationale: str
    eligible_asset_classes: tuple[str, ...]
    universe: tuple[str, ...]
    market_regime_assumptions: tuple[str, ...]
    required_features: tuple[str, ...]
    required_data_sources: tuple[str, ...]
    rules: ExecutableRules
    risk: StrategyRiskRules
    latency: LatencyRequirements
    module_policy: Mapping[str, ModuleRequirement]
    liquidity_minimum_notional_usd: Decimal = Decimal("10")
    maximum_spread_bps: Decimal = Decimal("50")
    expected_gross_edge_bps: Decimal = Decimal("0")
    edge_estimator_version: str = "unvalidated"
    confidence_calibration_version: str = "uncalibrated"
    invalidation_conditions: tuple[str, ...]
    known_weaknesses: tuple[str, ...]
    permitted_runtime_modes: tuple[RuntimeMode, ...]
    state: StrategyState
    execution_permissions: tuple[str, ...] = ()

    @field_validator("created_at")
    @classmethod
    def _aware_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _complete(self) -> "StrategySpecification":
        if not self.universe or not self.required_data_sources:
            raise ValueError("strategy universe and data sources are required")
        if not self.invalidation_conditions or not self.known_weaknesses:
            raise ValueError("invalidation conditions and known weaknesses are required")
        if not self.configuration_hash or not self.code_revision:
            raise ValueError("strategy revision and configuration hash are required")
        if self.maximum_spread_bps <= 0 or self.liquidity_minimum_notional_usd <= 0:
            raise ValueError("liquidity and spread requirements must be positive")
        if self.state in {StrategyState.ACCEPTED_PAPER, StrategyState.EXPERIMENTAL_PAPER}:
            if "paper_order" not in self.execution_permissions:
                raise ValueError("paper-executable strategies require paper_order permission")
            if self.confidence_calibration_version == "uncalibrated":
                raise ValueError("uncalibrated strategy versions cannot execute")
            if self.edge_estimator_version == "unvalidated" or self.expected_gross_edge_bps <= 0:
                raise ValueError("paper strategies require a validated positive edge estimate")
            if RuntimeMode.PAPER_EXECUTE not in self.permitted_runtime_modes:
                raise ValueError("paper strategy does not permit paper_execute mode")
        return self

    @property
    def key(self) -> str:
        return f"{self.strategy_id}:{self.version}"

    @property
    def fingerprint(self) -> str:
        # Creation time is provenance, not executable semantics.  Excluding it
        # lets a deterministic generator reproduce the same immutable version
        # after restart while every rule/configuration change still changes the
        # fingerprint and is rejected by the registry.
        material = self.model_dump(mode="json")
        material.pop("created_at", None)
        return canonical_hash(material)


class PromotionEvidence(FrozenModel):
    strategy_key: str
    evidence_sha256: str
    registered_at: datetime
    independent_signals: int
    completed_trades: int
    net_pnl_after_costs_usd: Decimal
    net_expectancy_usd: Decimal
    profit_factor: Decimal
    average_winner_usd: Decimal
    average_loser_usd: Decimal
    win_rate_fraction: Decimal
    downside_deviation_usd: Decimal
    turnover_usd: Decimal
    maximum_exposure_usd: Decimal
    maximum_drawdown_fraction: Decimal
    profitable_time_slices_fraction: Decimal
    maximum_single_trade_pnl_fraction: Decimal
    maximum_single_symbol_pnl_fraction: Decimal
    profitable_regimes_fraction: Decimal
    slippage_stress_expectancy_usd: Decimal
    latency_stress_expectancy_usd: Decimal
    missing_data_behavior_passed: bool
    module_failure_behavior_passed: bool
    duplicate_similarity_fraction: Decimal
    selection_registry_sha256: str
    transaction_cost_model_sha256: str
    untouched_out_of_sample: bool
    untouched_out_of_sample_artifact_sha256: str
    latency_feasible: bool
    validated_gross_edge_bps: Decimal
    edge_estimator_version: str
    confidence_calibration_version: str

    @model_validator(mode="after")
    def _valid_evidence(self) -> "PromotionEvidence":
        for digest in (
            self.evidence_sha256,
            self.untouched_out_of_sample_artifact_sha256,
            self.selection_registry_sha256,
            self.transaction_cost_model_sha256,
        ):
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
                raise ValueError("promotion evidence requires SHA-256 artifact identities")
        if self.registered_at.tzinfo is None:
            raise ValueError("promotion evidence timestamp must be timezone-aware")
        if min(self.independent_signals, self.completed_trades) < 0:
            raise ValueError("promotion evidence counts cannot be negative")
        finite_values = (
            self.net_pnl_after_costs_usd,
            self.net_expectancy_usd,
            self.profit_factor,
            self.average_winner_usd,
            self.average_loser_usd,
            self.win_rate_fraction,
            self.downside_deviation_usd,
            self.turnover_usd,
            self.maximum_exposure_usd,
            self.maximum_drawdown_fraction,
            self.profitable_time_slices_fraction,
            self.maximum_single_trade_pnl_fraction,
            self.maximum_single_symbol_pnl_fraction,
            self.profitable_regimes_fraction,
            self.slippage_stress_expectancy_usd,
            self.latency_stress_expectancy_usd,
            self.duplicate_similarity_fraction,
            self.validated_gross_edge_bps,
        )
        if any(not value.is_finite() for value in finite_values):
            raise ValueError("promotion evidence must contain only finite values")
        if min(self.downside_deviation_usd, self.turnover_usd, self.maximum_exposure_usd) < 0:
            raise ValueError("promotion risk/turnover evidence cannot be negative")
        if self.average_winner_usd <= 0 or self.average_loser_usd >= 0:
            raise ValueError("promotion winner/loser evidence has invalid signs")
        if not Decimal("0") <= self.maximum_drawdown_fraction <= Decimal("1"):
            raise ValueError("promotion drawdown is invalid")
        fractions = (
            self.win_rate_fraction,
            self.profitable_time_slices_fraction,
            self.maximum_single_trade_pnl_fraction,
            self.maximum_single_symbol_pnl_fraction,
            self.profitable_regimes_fraction,
            self.duplicate_similarity_fraction,
        )
        if any(not Decimal("0") <= value <= Decimal("1") for value in fractions):
            raise ValueError("promotion fraction evidence is invalid")
        if self.validated_gross_edge_bps <= 0:
            raise ValueError("promotion requires a positive independently validated edge")
        return self


class Bar(FrozenModel):
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @field_validator("timestamp")
    @classmethod
    def _aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("bar timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _valid_ohlcv(self) -> "Bar":
        prices = (self.open, self.high, self.low, self.close)
        if any(value <= 0 or not value.is_finite() for value in prices):
            raise ValueError("bar prices must be positive and finite")
        if self.volume < 0 or not self.volume.is_finite():
            raise ValueError("bar volume must be finite and nonnegative")
        if self.high < max(self.open, self.close, self.low) or self.low > min(self.open, self.close, self.high):
            raise ValueError("bar OHLC values are inconsistent")
        return self


class MarketSnapshot(FrozenModel):
    snapshot_id: str
    symbol: str
    asset_class: str
    provider: str
    quote_observed_at: datetime
    quote_received_at: datetime
    snapshot_created_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    bars: tuple[Bar, ...]
    data_quality_flags: tuple[str, ...] = ()
    source_metadata: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _coherent(self) -> "MarketSnapshot":
        times = (self.quote_observed_at, self.quote_received_at, self.snapshot_created_at)
        if any(value.tzinfo is None for value in times):
            raise ValueError("snapshot timestamps must be timezone-aware")
        if not self.quote_observed_at <= self.quote_received_at <= self.snapshot_created_at:
            raise ValueError("quote receipt/creation timestamps are causally incoherent")
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError("snapshot quote is invalid or crossed")
        if not self.bars:
            raise ValueError("snapshot requires historical bars")
        ordered = tuple(sorted(self.bars, key=lambda bar: bar.timestamp))
        if ordered != self.bars or len({bar.timestamp for bar in ordered}) != len(ordered):
            raise ValueError("bars must be unique and time-ordered")
        if ordered[-1].timestamp > self.snapshot_created_at:
            raise ValueError("future bar detected")
        if ordered[-1].timestamp > self.quote_observed_at:
            raise ValueError("bar/quote snapshot timestamps are incoherent")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_bps(self) -> Decimal:
        return ((self.ask - self.bid) / self.mid) * Decimal("10000")

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self)


class ModuleResult(FrozenModel):
    module: str
    state: ModuleState
    evaluated_at: datetime
    source_timestamp: datetime | None = None
    version: str
    values: Mapping[str, Any] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _causal_timestamps(self) -> "ModuleResult":
        if self.evaluated_at.tzinfo is None:
            raise ValueError("module evaluation timestamp must be timezone-aware")
        if self.source_timestamp is not None:
            if self.source_timestamp.tzinfo is None:
                raise ValueError("module source timestamp must be timezone-aware")
            if self.source_timestamp > self.evaluated_at:
                raise ValueError("module result uses future source data")
        return self


class TradingSignal(FrozenModel):
    signal_id: str
    run_id: str
    timestamp: datetime
    symbol: str
    asset_class: str
    strategy_id: str
    strategy_version: str
    direction: SignalDirection
    signal_type: str
    raw_score: Decimal
    calibrated_confidence: Decimal | None
    confidence_calibration_version: str
    feature_values: Mapping[str, Any]
    source_data_timestamps: Mapping[str, str]
    coherent_snapshot_id: str
    market_regime: str
    module_states: Mapping[str, ModuleState]
    proposed_entry_reference: Decimal
    proposed_stop: Decimal | None
    proposed_target: Decimal | None
    holding_period_expectation_minutes: int
    maximum_signal_age_ms: int
    gross_edge_bps: Decimal | None
    rejection_reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    data_quality_flags: tuple[str, ...] = ()
    latency_result: str = "NOT_EVALUATED"

    @model_validator(mode="after")
    def _valid_signal_contract(self) -> "TradingSignal":
        if self.timestamp.tzinfo is None:
            raise ValueError("signal timestamp must be timezone-aware")
        if self.proposed_entry_reference <= 0 or not self.proposed_entry_reference.is_finite():
            raise ValueError("signal entry reference must be positive and finite")
        if self.maximum_signal_age_ms <= 0 or self.holding_period_expectation_minutes <= 0:
            raise ValueError("signal timing limits must be positive")
        if self.calibrated_confidence is not None and not Decimal("0") <= self.calibrated_confidence <= Decimal("1"):
            raise ValueError("calibrated confidence must be between zero and one")
        if self.gross_edge_bps is not None and not self.gross_edge_bps.is_finite():
            raise ValueError("gross edge must be finite")
        if self.direction is SignalDirection.BUY:
            if self.proposed_stop is not None and self.proposed_stop >= self.proposed_entry_reference:
                raise ValueError("long-entry stop must be below the entry reference")
            if self.proposed_target is not None and self.proposed_target <= self.proposed_entry_reference:
                raise ValueError("long-entry target must be above the entry reference")
        for label, raw in self.source_data_timestamps.items():
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"signal source timestamp is invalid: {label}") from exc
            if parsed.tzinfo is None or parsed.astimezone(timezone.utc) > self.timestamp.astimezone(timezone.utc):
                raise ValueError(f"signal source timestamp is non-causal: {label}")
        return self

    @property
    def executable(self) -> bool:
        return (
            self.direction is not SignalDirection.HOLD
            and self.calibrated_confidence is not None
            and self.confidence_calibration_version != "uncalibrated"
            and self.gross_edge_bps is not None
            and self.gross_edge_bps > 0
            and not self.rejection_reasons
        )


class RiskDecision(FrozenModel):
    approved: bool
    reason_codes: tuple[str, ...]
    quantity: Decimal = Decimal("0")
    estimated_notional_usd: Decimal = Decimal("0")
    estimated_loss_at_stop_usd: Decimal = Decimal("0")
    gross_exposure_after_usd: Decimal = Decimal("0")
    available_internal_capital_usd: Decimal = Decimal("0")


class OrderIntent(FrozenModel):
    intent_id: str
    decision_id: str | None = None
    run_id: str
    session_id: str
    strategy_id: str
    strategy_version: str
    signal_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: Decimal
    order_type: Literal["market", "limit"]
    time_in_force: Literal["gtc", "ioc"]
    kind: IntentKind
    risk_approved: bool
    latency_approved: bool
    reconciliation_approved: bool
    created_at: datetime
    expires_at: datetime
    client_order_id: str
    allocation_id: str | None = None
    proposed_stop: Decimal | None = None
    proposed_target: Decimal | None = None
    holding_deadline: datetime | None = None

    @model_validator(mode="after")
    def _valid_intent(self) -> "OrderIntent":
        if self.quantity <= 0 or not self.quantity.is_finite():
            raise ValueError("order intent quantity must be positive and finite")
        if self.created_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("order intent timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("order intent must expire after creation")
        if not self.client_order_id:
            raise ValueError("order intent requires a client order ID")
        if self.holding_deadline is not None:
            if self.holding_deadline.tzinfo is None or self.holding_deadline <= self.created_at:
                raise ValueError("holding deadline must be aware and after intent creation")
        return self


class PreflightItem(FrozenModel):
    name: str
    status: CheckStatus
    evidence: str
    source: str
    severity: Literal["info", "warning", "critical"]
    action_required: str = ""


class PreflightReport(FrozenModel):
    run_id: str
    mode: RuntimeMode
    created_at: datetime
    items: tuple[PreflightItem, ...]

    @property
    def passed(self) -> bool:
        return not any(item.status is CheckStatus.FAIL for item in self.items)


class CycleReport(FrozenModel):
    run_id: str
    session_id: str
    mode: RuntimeMode
    started_at: datetime
    finished_at: datetime
    preflight_passed: bool
    snapshots: int
    signals_generated: int
    signals_rejected: int
    shadow_signals: int
    intents_created: int
    orders_submitted: int
    safety_halts: tuple[str, ...]
    report_path: str | None = None
