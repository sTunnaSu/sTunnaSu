"""Deterministic analysis, strategy, promotion, and risk services for Phase 8."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from statistics import mean, pstdev
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence

from src.phase8_runtime.configuration import AdaptiveResearchConfig
from src.phase8_runtime.models import (
    Bar,
    ExecutableRules,
    LatencyRequirements,
    MarketSnapshot,
    ModuleRequirement,
    ModuleResult,
    ModuleState,
    PromotionEvidence,
    RiskDecision,
    RuntimeMode,
    SignalDirection,
    StrategyRiskRules,
    StrategySpecification,
    StrategyState,
    TradingSignal,
    canonical_hash,
)


Clock = Callable[[], datetime]


def _decimal(value: Any, *, label: str, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError(f"{label} is invalid")
    return result


def _timestamp(value: Any, *, label: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} has an invalid timestamp") from exc
    if result.tzinfo is None:
        raise ValueError(f"{label} timestamp must be timezone-aware")
    return result.astimezone(timezone.utc)


class MarketDataReader(Protocol):
    def __call__(self, symbol: str, profile_id: str, **kwargs: Any) -> Mapping[str, Any]: ...


class BrokerMarketDataService:
    """Build a coherent quote/bar snapshot from Alpaca paper reads."""

    def __init__(
        self,
        *,
        profile_id: str,
        quote_reader: MarketDataReader,
        history_reader: MarketDataReader,
        clock: Clock,
    ) -> None:
        self.profile_id = profile_id
        self.quote_reader = quote_reader
        self.history_reader = history_reader
        self.clock = clock

    def snapshot(self, symbol: str, *, period: str, limit: int) -> MarketSnapshot:
        quote_response = self.quote_reader(symbol, self.profile_id)
        quote_received_at = self.clock().astimezone(timezone.utc)
        history_response = self.history_reader(
            symbol,
            self.profile_id,
            period=period,
            limit=limit,
        )
        created_at = self.clock().astimezone(timezone.utc)
        if quote_response.get("status") != "ok":
            raise ValueError(str(quote_response.get("error") or "quote unavailable"))
        if history_response.get("status") != "ok":
            raise ValueError(str(history_response.get("error") or "history unavailable"))
        raw_quote = quote_response.get("quote")
        if not isinstance(raw_quote, Mapping):
            raise ValueError("quote payload is missing")
        raw_bars = history_response.get("bars")
        if not isinstance(raw_bars, list) or not raw_bars:
            raise ValueError("bar payload is missing")
        bars: list[Bar] = []
        flags: list[str] = []
        for index, row in enumerate(raw_bars):
            if not isinstance(row, Mapping):
                raise ValueError(f"bar {index} is malformed")
            bar = Bar(
                timestamp=_timestamp(row.get("time"), label=f"bar {index}"),
                open=_decimal(row.get("open"), label=f"bar {index} open", positive=True),
                high=_decimal(row.get("high"), label=f"bar {index} high", positive=True),
                low=_decimal(row.get("low"), label=f"bar {index} low", positive=True),
                close=_decimal(row.get("close"), label=f"bar {index} close", positive=True),
                volume=_decimal(row.get("volume") or 0, label=f"bar {index} volume"),
            )
            if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
                flags.append("ohlc_inconsistent")
            bars.append(bar)
        bars.sort(key=lambda item: item.timestamp)
        if len({bar.timestamp for bar in bars}) != len(bars):
            raise ValueError("duplicate bar timestamps")
        quote_at = _timestamp(raw_quote.get("time"), label="quote")
        source = {
            "quote_profile": quote_response.get("profile_id"),
            "quote_environment": quote_response.get("environment"),
            "history_profile": history_response.get("profile_id"),
            "history_environment": history_response.get("environment"),
            "period": period,
            "bar_count": len(bars),
        }
        material = {
            "symbol": symbol,
            "quote_at": quote_at.isoformat(),
            "bid": str(raw_quote.get("bid")),
            "ask": str(raw_quote.get("ask")),
            "last_bar": bars[-1].timestamp.isoformat(),
            "period": period,
        }
        return MarketSnapshot(
            snapshot_id="snap_" + canonical_hash(material)[:32],
            symbol=str(history_response.get("symbol") or symbol).upper(),
            asset_class=str(history_response.get("asset_class") or "unknown"),
            provider="alpaca-paper",
            quote_observed_at=quote_at,
            quote_received_at=quote_received_at,
            snapshot_created_at=created_at,
            bid=_decimal(raw_quote.get("bid"), label="bid", positive=True),
            ask=_decimal(raw_quote.get("ask"), label="ask", positive=True),
            bid_size=(
                _decimal(raw_quote.get("bid_size"), label="bid_size")
                if raw_quote.get("bid_size") not in (None, "")
                else None
            ),
            ask_size=(
                _decimal(raw_quote.get("ask_size"), label="ask_size")
                if raw_quote.get("ask_size") not in (None, "")
                else None
            ),
            bars=tuple(bars),
            data_quality_flags=tuple(sorted(set(flags))),
            source_metadata=source,
        )


class AnalyticalModule(Protocol):
    name: str

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        *,
        now: datetime,
    ) -> ModuleResult: ...


class TechnicalAnalysisService:
    name = "technical"
    version = "phase8-technical-v1"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        *,
        now: datetime,
    ) -> ModuleResult:
        required = strategy.rules.slow_window + 1
        if len(snapshot.bars) < required:
            return ModuleResult(
                module=self.name,
                state=ModuleState.UNAVAILABLE,
                evaluated_at=now,
                source_timestamp=snapshot.bars[-1].timestamp,
                version=self.version,
                reasons=(f"requires {required} bars",),
            )
        closes = [float(bar.close) for bar in snapshot.bars]
        fast = mean(closes[-strategy.rules.fast_window :])
        slow = mean(closes[-strategy.rules.slow_window :])
        previous = closes[-strategy.rules.slow_window - 1]
        momentum_bps = ((closes[-1] / previous) - 1.0) * 10_000
        returns = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]
        volatility_bps = pstdev(returns[-strategy.rules.slow_window :]) * 10_000
        trend_bps = ((fast / slow) - 1.0) * 10_000
        return ModuleResult(
            module=self.name,
            state=ModuleState.VALID,
            evaluated_at=now,
            source_timestamp=snapshot.bars[-1].timestamp,
            version=self.version,
            values={
                "fast_average": fast,
                "slow_average": slow,
                "trend_bps": trend_bps,
                "momentum_bps": momentum_bps,
                "volatility_bps": volatility_bps,
                "last_close": closes[-1],
            },
        )


class RegimeAnalysisService:
    name = "regime"
    version = "phase8-regime-v1"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        *,
        now: datetime,
    ) -> ModuleResult:
        technical = TechnicalAnalysisService().evaluate(snapshot, strategy, now=now)
        if technical.state is not ModuleState.VALID:
            return ModuleResult(
                module=self.name,
                state=technical.state,
                evaluated_at=now,
                source_timestamp=technical.source_timestamp,
                version=self.version,
                reasons=technical.reasons,
            )
        trend = float(technical.values["trend_bps"])
        volatility = float(technical.values["volatility_bps"])
        if volatility >= 150:
            regime = "high_vol"
        elif volatility <= 25:
            regime = "low_vol"
        elif trend >= 20:
            regime = "bull"
        elif trend <= -20:
            regime = "bear"
        else:
            regime = "sideways"
        return ModuleResult(
            module=self.name,
            state=ModuleState.VALID,
            evaluated_at=now,
            source_timestamp=technical.source_timestamp,
            version=self.version,
            values={"regime": regime, "trend_bps": trend, "volatility_bps": volatility},
        )


class UnavailableAnalysisService:
    """Explicit fail-closed state for modules with no configured adapter."""

    def __init__(self, name: str, *, reason: str = "adapter not configured") -> None:
        self.name = name
        self.reason = reason
        self.version = f"{name}-unavailable-v1"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        *,
        now: datetime,
    ) -> ModuleResult:
        requirement = strategy.module_policy.get(self.name, ModuleRequirement.NOT_APPLICABLE)
        state = (
            ModuleState.NOT_APPLICABLE if requirement is ModuleRequirement.NOT_APPLICABLE else ModuleState.UNAVAILABLE
        )
        return ModuleResult(
            module=self.name,
            state=state,
            evaluated_at=now,
            version=self.version,
            reasons=(() if state is ModuleState.NOT_APPLICABLE else (self.reason,)),
        )


class SignalEvaluationService:
    """Evaluate the built-in rule vocabulary without broker authority."""

    def __init__(self, modules: Mapping[str, AnalyticalModule]) -> None:
        self.modules = dict(modules)

    def evaluate(
        self,
        *,
        run_id: str,
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        now: datetime,
    ) -> tuple[TradingSignal, Mapping[str, ModuleResult]]:
        results = {name: module.evaluate(snapshot, strategy, now=now) for name, module in self.modules.items()}
        rejection_reasons: list[str] = []
        for module_name, requirement in strategy.module_policy.items():
            result = results.get(module_name)
            if result is None:
                if requirement is ModuleRequirement.REQUIRED:
                    rejection_reasons.append(f"required_module_missing:{module_name}")
                continue
            if requirement is ModuleRequirement.REQUIRED and result.state is not ModuleState.VALID:
                rejection_reasons.append(f"required_module_{result.state.value.lower()}:{module_name}")
        technical = results.get("technical")
        regime_result = results.get("regime")
        regime = str(regime_result.values.get("regime", "unknown")) if regime_result else "unknown"
        if regime not in strategy.risk.permitted_regimes:
            rejection_reasons.append(f"regime_not_permitted:{regime}")
        raw_score = Decimal("0")
        if technical and technical.state is ModuleState.VALID:
            if strategy.rules.family == "trend":
                raw_score = Decimal(str(technical.values["trend_bps"]))
            elif strategy.rules.family == "momentum":
                raw_score = Decimal(str(technical.values["momentum_bps"]))
            else:
                raw_score = -Decimal(str(technical.values["momentum_bps"]))
        threshold = strategy.rules.entry_threshold_bps
        direction = SignalDirection.BUY if raw_score > threshold else SignalDirection.HOLD
        if strategy.rules.long_only and raw_score < -threshold:
            direction = SignalDirection.HOLD
        if direction is SignalDirection.HOLD:
            rejection_reasons.append("entry_rule_not_satisfied")
        executable_state = strategy.state in {
            StrategyState.ACCEPTED_PAPER,
            StrategyState.EXPERIMENTAL_PAPER,
        }
        confidence: Decimal | None = None
        gross_edge: Decimal | None = None
        if executable_state and strategy.confidence_calibration_version != "uncalibrated":
            denominator = max(threshold * Decimal("4"), Decimal("1"))
            confidence = min(Decimal("0.99"), abs(raw_score) / denominator)
            gross_edge = strategy.expected_gross_edge_bps
        entry = snapshot.ask
        stop = entry * (Decimal("1") - strategy.rules.stop_loss_bps / Decimal("10000"))
        target = entry * (Decimal("1") + strategy.rules.take_profit_bps / Decimal("10000"))
        signal_material = {
            "run_id": run_id,
            "snapshot": snapshot.snapshot_id,
            "strategy": strategy.key,
            "direction": direction.value,
            "score": str(raw_score),
        }
        signal_id = "sig_" + hashlib.sha256(json_bytes(signal_material)).hexdigest()[:32]
        signal = TradingSignal(
            signal_id=signal_id,
            run_id=run_id,
            timestamp=now,
            symbol=snapshot.symbol,
            asset_class=snapshot.asset_class,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            direction=direction,
            signal_type=f"{strategy.rules.family}_entry",
            raw_score=raw_score,
            calibrated_confidence=confidence,
            confidence_calibration_version=strategy.confidence_calibration_version,
            feature_values=(technical.values if technical else {}),
            source_data_timestamps={
                "quote": snapshot.quote_observed_at.isoformat(),
                "bar": snapshot.bars[-1].timestamp.isoformat(),
                **{
                    name: result.source_timestamp.isoformat()
                    for name, result in results.items()
                    if result.source_timestamp is not None
                },
            },
            coherent_snapshot_id=snapshot.snapshot_id,
            market_regime=regime,
            module_states={name: result.state for name, result in results.items()},
            proposed_entry_reference=entry,
            proposed_stop=stop,
            proposed_target=target,
            holding_period_expectation_minutes=strategy.latency.expected_holding_period_minutes,
            maximum_signal_age_ms=strategy.latency.maximum_signal_age_ms,
            gross_edge_bps=gross_edge,
            rejection_reasons=tuple(rejection_reasons),
            warnings=tuple(snapshot.data_quality_flags),
            data_quality_flags=snapshot.data_quality_flags,
        )
        return signal, results


def json_bytes(value: Any) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class AdaptiveStrategyGenerator:
    """Generate complete shadow-only hypotheses from the current snapshot."""

    def __init__(self, config: AdaptiveResearchConfig, *, code_revision: str) -> None:
        self.config = config
        self.code_revision = code_revision

    def generate(self, snapshot: MarketSnapshot, *, now: datetime) -> tuple[StrategySpecification, ...]:
        if not self.config.enabled:
            return ()
        available_families: tuple[Literal["trend", "mean_reversion"], ...] = (
            "trend",
            "mean_reversion",
        )
        families = available_families[: self.config.maximum_new_hypotheses_per_cycle]
        generated: list[StrategySpecification] = []
        for family in families:
            rules = ExecutableRules(
                family=family,
                fast_window=5,
                slow_window=20,
                entry_threshold_bps=Decimal("15"),
                exit_threshold_bps=Decimal("0"),
                stop_loss_bps=Decimal("100"),
                take_profit_bps=Decimal("200"),
                maximum_holding_cycles=60,
            )
            identity = canonical_hash(
                {
                    "family": family,
                    "symbol": snapshot.symbol,
                    "rules": rules.model_dump(mode="json"),
                    "code_revision": self.code_revision,
                }
            )[:12]
            generated.append(
                StrategySpecification(
                    strategy_id=f"adaptive-{family}-{snapshot.symbol.replace('/', '-').lower()}",
                    version=f"v1-{identity}",
                    created_at=now,
                    creator_component="phase8-adaptive-generator-v1",
                    code_revision=self.code_revision,
                    configuration_hash=canonical_hash(rules),
                    hypothesis=(
                        f"A {family} rule on {snapshot.symbol} may retain positive expectancy "
                        "after costs in its declared regimes."
                    ),
                    edge_rationale=(
                        "Behavioral underreaction and risk transfer"
                        if family == "trend"
                        else "Short-horizon liquidity provision after temporary displacement"
                    ),
                    eligible_asset_classes=(snapshot.asset_class,),
                    universe=(snapshot.symbol,),
                    market_regime_assumptions=("bull", "bear", "sideways"),
                    required_features=("returns", "moving_averages", "volatility"),
                    required_data_sources=("alpaca-paper-quote", "alpaca-paper-bars"),
                    rules=rules,
                    risk=StrategyRiskRules(maximum_position_value_usd=Decimal("50")),
                    latency=LatencyRequirements(),
                    module_policy={
                        "technical": ModuleRequirement.REQUIRED,
                        "regime": ModuleRequirement.REQUIRED,
                        "ai": ModuleRequirement.NOT_APPLICABLE,
                        "sentiment": ModuleRequirement.NOT_APPLICABLE,
                        "news": ModuleRequirement.NOT_APPLICABLE,
                    },
                    liquidity_minimum_notional_usd=Decimal("10"),
                    maximum_spread_bps=Decimal("50"),
                    expected_gross_edge_bps=Decimal("0"),
                    edge_estimator_version="unvalidated",
                    confidence_calibration_version="uncalibrated",
                    invalidation_conditions=(
                        "negative net expectancy after modeled costs",
                        "regime instability",
                        "latency infeasibility",
                    ),
                    known_weaknesses=(
                        "generated from one current snapshot",
                        "no prospective out-of-sample evidence",
                    ),
                    permitted_runtime_modes=(RuntimeMode.RESEARCH_ONLY, RuntimeMode.DRY_RUN),
                    state=StrategyState.SHADOW,
                    execution_permissions=(),
                )
            )
        return tuple(generated)


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    approved: bool
    reason_codes: tuple[str, ...]


class PromotionController:
    """Fail-closed promotion gate; generated strategies cannot self-promote."""

    def __init__(self, config: AdaptiveResearchConfig) -> None:
        self.policy = config.promotion
        self.experimental_risk = config.experimental_risk

    def evaluate(self, summary: Mapping[str, Any]) -> PromotionDecision:
        reasons: list[str] = []
        if int(summary.get("independent_signals", 0)) < self.policy.minimum_independent_signals:
            reasons.append("insufficient_independent_signals")
        if int(summary.get("completed_trades", 0)) < self.policy.minimum_completed_trades:
            reasons.append("insufficient_completed_trades")
        if float(summary.get("expectancy", 0)) <= self.policy.minimum_net_expectancy_usd:
            reasons.append("nonpositive_net_expectancy")
        if float(summary.get("net_pnl_after_costs", 0)) <= 0:
            reasons.append("nonpositive_net_pnl_after_costs")
        if float(summary.get("maximum_exposure", float("inf"))) > float(
            self.experimental_risk.maximum_aggregate_exposure_usd
        ):
            reasons.append("experimental_exposure_limit_exceeded")
        if float(summary.get("profit_factor", 0)) < self.policy.minimum_profit_factor:
            reasons.append("profit_factor_below_threshold")
        if float(summary.get("maximum_drawdown_fraction", 1)) > self.policy.maximum_drawdown_fraction:
            reasons.append("drawdown_above_threshold")
        if float(summary.get("profitable_time_slices_fraction", 0)) < (
            self.policy.minimum_profitable_time_slices_fraction
        ):
            reasons.append("time_slice_instability")
        if float(summary.get("single_trade_fraction", 1)) > self.policy.maximum_single_trade_pnl_fraction:
            reasons.append("single_trade_dominance")
        if float(summary.get("single_symbol_fraction", 1)) > self.policy.maximum_single_symbol_pnl_fraction:
            reasons.append("single_symbol_dominance")
        if float(summary.get("profitable_regimes_fraction", 0)) < self.policy.minimum_profitable_regimes_fraction:
            reasons.append("regime_instability")
        if float(summary.get("slippage_stress_expectancy", 0)) <= 0:
            reasons.append("slippage_sensitivity_failed")
        if float(summary.get("latency_stress_expectancy", 0)) <= 0:
            reasons.append("latency_sensitivity_failed")
        if not bool(summary.get("missing_data_behavior_passed", False)):
            reasons.append("missing_data_behavior_failed")
        if not bool(summary.get("module_failure_behavior_passed", False)):
            reasons.append("module_failure_behavior_failed")
        if float(summary.get("duplicate_similarity_fraction", 1)) > self.policy.maximum_duplicate_similarity_fraction:
            reasons.append("duplicate_similarity_too_high")
        if not bool(summary.get("untouched_out_of_sample", False)):
            reasons.append("untouched_out_of_sample_missing")
        if not bool(summary.get("latency_feasible", False)):
            reasons.append("latency_feasibility_missing")
        return PromotionDecision(approved=not reasons, reason_codes=tuple(reasons))

    def promote(
        self,
        strategy: StrategySpecification,
        evidence: PromotionEvidence,
        *,
        code_revision: str,
    ) -> StrategySpecification:
        if strategy.state is not StrategyState.SHADOW:
            raise ValueError("only a shadow strategy version can be promoted")
        if evidence.strategy_key != strategy.key:
            raise ValueError("promotion evidence belongs to another strategy version")
        decision = self.evaluate(
            {
                "independent_signals": evidence.independent_signals,
                "completed_trades": evidence.completed_trades,
                "net_pnl_after_costs": evidence.net_pnl_after_costs_usd,
                "maximum_exposure": evidence.maximum_exposure_usd,
                "expectancy": evidence.net_expectancy_usd,
                "profit_factor": evidence.profit_factor,
                "maximum_drawdown_fraction": evidence.maximum_drawdown_fraction,
                "profitable_time_slices_fraction": (evidence.profitable_time_slices_fraction),
                "single_trade_fraction": evidence.maximum_single_trade_pnl_fraction,
                "single_symbol_fraction": evidence.maximum_single_symbol_pnl_fraction,
                "profitable_regimes_fraction": evidence.profitable_regimes_fraction,
                "slippage_stress_expectancy": evidence.slippage_stress_expectancy_usd,
                "latency_stress_expectancy": evidence.latency_stress_expectancy_usd,
                "missing_data_behavior_passed": evidence.missing_data_behavior_passed,
                "module_failure_behavior_passed": evidence.module_failure_behavior_passed,
                "duplicate_similarity_fraction": evidence.duplicate_similarity_fraction,
                "untouched_out_of_sample": evidence.untouched_out_of_sample,
                "latency_feasible": evidence.latency_feasible,
            }
        )
        if not decision.approved:
            raise ValueError("promotion gate rejected: " + ",".join(decision.reason_codes))
        risk = StrategyRiskRules.model_validate(
            {
                **strategy.risk.model_dump(mode="python"),
                "maximum_position_value_usd": Decimal("50"),
            }
        )
        promoted = strategy.model_dump(mode="python")
        promoted.update(
            {
                "version": f"{strategy.version}-experimental-{evidence.evidence_sha256[:8]}",
                "parent_ids": (*strategy.parent_ids, strategy.key),
                "creator_component": "phase8-controlled-promotion-v1",
                "code_revision": code_revision,
                "configuration_hash": evidence.evidence_sha256,
                "risk": risk,
                "expected_gross_edge_bps": evidence.validated_gross_edge_bps,
                "edge_estimator_version": evidence.edge_estimator_version,
                "confidence_calibration_version": evidence.confidence_calibration_version,
                "permitted_runtime_modes": (
                    RuntimeMode.RESEARCH_ONLY,
                    RuntimeMode.DRY_RUN,
                    RuntimeMode.PAPER_EXECUTE,
                ),
                "state": StrategyState.EXPERIMENTAL_PAPER,
                "execution_permissions": ("paper_order",),
            }
        )
        return StrategySpecification.model_validate(promoted)

    def demotion_reasons(self, summary: Mapping[str, Any]) -> tuple[str, ...]:
        reasons: list[str] = []
        if int(summary.get("critical_rule_violations", 0)) > 0:
            reasons.append("critical_rule_violation")
        if int(summary.get("unresolved_orders", 0)) > 0:
            reasons.append("unresolved_order")
        if float(summary.get("realized_pnl", 0)) <= -5.0:
            reasons.append("experimental_loss_limit")
        if int(summary.get("completed_round_trips", 0)) >= 5:
            reasons.append("experimental_round_trip_limit")
        if not bool(summary.get("latency_feasible", True)):
            reasons.append("latency_infeasible")
        return tuple(reasons)


class PortfolioRiskManager:
    """Account/portfolio/strategy/order risk sizing for the locked profile."""

    def __init__(self, validation_profile, experimental_profile) -> None:  # noqa: ANN001
        self.profile = validation_profile
        self.experimental_profile = experimental_profile

    def size_entry(
        self,
        *,
        signal: TradingSignal,
        strategy: StrategySpecification,
        account: Mapping[str, Any],
        broker_positions: Sequence[Mapping[str, Any]],
        open_allocations: Sequence[Mapping[str, Any]],
        intent_counts: Mapping[str, int],
        completed_round_trips: int,
        strategy_completed_round_trips: int,
        strategy_realized_pnl: Decimal,
        asset: Mapping[str, Any],
    ) -> RiskDecision:
        reasons: list[str] = []
        if not signal.executable:
            reasons.append("signal_not_execution_eligible")
        if signal.direction is not SignalDirection.BUY:
            reasons.append("entry_direction_not_supported")
        if signal.proposed_stop is None or signal.proposed_stop >= signal.proposed_entry_reference:
            reasons.append("invalid_protective_stop")
        if signal.symbol not in strategy.universe:
            reasons.append("symbol_outside_strategy_universe")
        if signal.market_regime not in strategy.risk.permitted_regimes:
            reasons.append("strategy_regime_ineligible")
        if int(intent_counts.get("entry", 0)) >= self.profile.maximum_entry_orders:
            reasons.append("entry_order_limit")
        if int(intent_counts.get("submitted", 0)) >= self.profile.maximum_total_submitted_orders:
            reasons.append("total_order_limit")
        if int(intent_counts.get("unresolved", 0)) > 0:
            reasons.append("unresolved_order_exists")
        if len(open_allocations) >= self.profile.maximum_concurrent_positions:
            reasons.append("concurrent_position_limit")
        strategy_key = f"{strategy.strategy_id}:{strategy.version}"
        strategy_allocations = [row for row in open_allocations if str(row.get("strategy_key") or "") == strategy_key]
        if len(strategy_allocations) >= strategy.risk.maximum_open_positions:
            reasons.append("strategy_open_position_limit")
        if completed_round_trips >= self.profile.maximum_completed_round_trips:
            reasons.append("session_round_trip_limit")
        if strategy_completed_round_trips >= strategy.risk.maximum_completed_round_trips:
            reasons.append("strategy_round_trip_limit")
        experimental = strategy.state is StrategyState.EXPERIMENTAL_PAPER
        if experimental:
            if len(strategy_allocations) >= self.experimental_profile.maximum_simultaneous_positions:
                reasons.append("experimental_position_limit")
            if strategy_completed_round_trips >= self.experimental_profile.maximum_completed_round_trips:
                reasons.append("experimental_round_trip_limit")
            if strategy_realized_pnl <= -self.experimental_profile.maximum_realized_loss_usd:
                reasons.append("experimental_loss_limit")
        if any(str(position.get("symbol") or "").upper() == signal.symbol for position in broker_positions):
            reasons.append("symbol_already_held_or_external")

        external_exposure = sum(abs(float(position.get("market_value") or 0)) for position in broker_positions)
        owned_exposure = sum(
            abs(float(row.get("remaining_quantity") or 0)) * abs(float(row.get("average_fill_price") or 0))
            for row in open_allocations
        )
        current_exposure = Decimal(str(max(external_exposure, owned_exposure)))
        remaining_exposure = self.profile.maximum_gross_exposure_usd - current_exposure
        cash_raw = account.get("cash") or account.get("buying_power") or 0
        cash = _decimal(cash_raw, label="account cash")
        internal_remaining = min(
            self.profile.internal_capital_usd - current_exposure,
            cash,
        )
        maximum_notional = min(
            self.profile.maximum_position_value_usd,
            strategy.risk.maximum_position_value_usd,
            remaining_exposure,
            internal_remaining,
        )
        if experimental:
            experimental_exposure = sum(
                abs(Decimal(str(row.get("remaining_quantity") or 0)))
                * abs(Decimal(str(row.get("average_fill_price") or 0)))
                for row in strategy_allocations
            )
            maximum_notional = min(
                maximum_notional,
                self.experimental_profile.maximum_position_value_usd,
                self.experimental_profile.maximum_aggregate_exposure_usd - experimental_exposure,
            )
        stop_fraction = (
            (signal.proposed_entry_reference - signal.proposed_stop) / signal.proposed_entry_reference
            if signal.proposed_stop is not None
            else Decimal("0")
        )
        if stop_fraction <= 0:
            maximum_notional = Decimal("0")
        else:
            maximum_notional = min(
                maximum_notional,
                strategy.risk.maximum_loss_per_trade_usd / stop_fraction,
            )
        price = signal.proposed_entry_reference
        increment = _decimal(asset.get("min_trade_increment") or "0.00000001", label="trade increment")
        minimum_quantity = _decimal(asset.get("min_order_size") or increment, label="minimum order size")
        if increment <= 0 or minimum_quantity <= 0:
            reasons.append("invalid_asset_precision")
            quantity = Decimal("0")
        else:
            raw_quantity = maximum_notional / price if price > 0 else Decimal("0")
            quantity = (raw_quantity / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        notional = quantity * price
        minimum_notional = strategy.liquidity_minimum_notional_usd
        if quantity < minimum_quantity:
            reasons.append("below_minimum_order_size")
        if notional < minimum_notional:
            reasons.append("below_minimum_notional")
        if notional <= 0 or notional > maximum_notional:
            reasons.append("unsafe_sized_notional")
        exposure_after = current_exposure + notional
        if exposure_after > self.profile.maximum_gross_exposure_usd:
            reasons.append("gross_exposure_limit")
        reserve_after = self.profile.internal_capital_usd - exposure_after
        if reserve_after < self.profile.minimum_unallocated_capital_usd:
            reasons.append("cash_reserve_limit")
        loss_at_stop = notional * stop_fraction
        if loss_at_stop > strategy.risk.maximum_loss_per_trade_usd:
            reasons.append("strategy_loss_per_trade_limit")
        return RiskDecision(
            approved=not reasons,
            reason_codes=tuple(sorted(set(reasons))),
            quantity=max(quantity, Decimal("0")),
            estimated_notional_usd=max(notional, Decimal("0")),
            estimated_loss_at_stop_usd=max(loss_at_stop, Decimal("0")),
            gross_exposure_after_usd=max(exposure_after, Decimal("0")),
            available_internal_capital_usd=max(internal_remaining, Decimal("0")),
        )
