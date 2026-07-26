"""Configuration loading and paper-only validation for Phase 8 runtime."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

import yaml  # type: ignore[import-untyped]
from pydantic import Field, model_validator

from src.config.paths import get_runtime_root
from src.phase8_runtime.models import (
    ExperimentalRiskProfile,
    FrozenModel,
    Phase8ValidationProfile,
    ReleaseDecision,
    RuntimeMode,
    StrategySpecification,
    canonical_hash,
)


PAPER_PROFILE_ID = "alpaca-paper-trade"
PAPER_ENDPOINT = "https://paper-api.alpaca.markets"


class PromotionPolicy(FrozenModel):
    minimum_independent_signals: int = 30
    minimum_completed_trades: int = 20
    minimum_net_expectancy_usd: float = 0.0
    minimum_profit_factor: float = 1.1
    maximum_drawdown_fraction: float = 0.10
    minimum_profitable_time_slices_fraction: float = 0.60
    maximum_single_trade_pnl_fraction: float = 0.35
    maximum_single_symbol_pnl_fraction: float = 0.50
    minimum_profitable_regimes_fraction: float = 0.50
    maximum_duplicate_similarity_fraction: float = 0.80

    @model_validator(mode="after")
    def _valid_promotion_policy(self) -> "PromotionPolicy":
        if min(self.minimum_independent_signals, self.minimum_completed_trades) <= 0:
            raise ValueError("promotion sample requirements must be positive")
        if self.minimum_net_expectancy_usd < 0 or self.minimum_profit_factor < 1:
            raise ValueError("promotion economic thresholds are invalid")
        if not 0 <= self.maximum_drawdown_fraction < 1:
            raise ValueError("promotion drawdown threshold is invalid")
        if not 0 < self.minimum_profitable_time_slices_fraction <= 1:
            raise ValueError("promotion time-slice threshold is invalid")
        if not 0 < self.maximum_single_trade_pnl_fraction <= 1:
            raise ValueError("promotion concentration threshold is invalid")
        if not 0 < self.maximum_single_symbol_pnl_fraction <= 1:
            raise ValueError("promotion symbol-concentration threshold is invalid")
        if not 0 < self.minimum_profitable_regimes_fraction <= 1:
            raise ValueError("promotion regime-stability threshold is invalid")
        if not 0 <= self.maximum_duplicate_similarity_fraction < 1:
            raise ValueError("promotion duplicate-similarity threshold is invalid")
        return self


class ShadowSimulationConfig(FrozenModel):
    notional_usd: float = 50.0
    taker_fee_bps: float = 25.0
    slippage_bps_each_side: float = 5.0

    @model_validator(mode="after")
    def _valid_shadow_costs(self) -> "ShadowSimulationConfig":
        if self.notional_usd <= 0:
            raise ValueError("shadow notional must be positive")
        if self.taker_fee_bps < 0 or self.slippage_bps_each_side < 0:
            raise ValueError("shadow costs cannot be negative")
        return self


class AdaptiveResearchConfig(FrozenModel):
    enabled: bool = True
    maximum_new_hypotheses_per_cycle: int = 2
    promotion: PromotionPolicy = Field(default_factory=PromotionPolicy)
    experimental_risk: ExperimentalRiskProfile = Field(default_factory=ExperimentalRiskProfile)
    shadow: ShadowSimulationConfig = Field(default_factory=ShadowSimulationConfig)

    @model_validator(mode="after")
    def _valid_research_limits(self) -> "AdaptiveResearchConfig":
        if self.maximum_new_hypotheses_per_cycle <= 0:
            raise ValueError("maximum_new_hypotheses_per_cycle must be positive")
        return self


class Phase8RuntimeConfig(FrozenModel):
    """Complete runtime configuration with paper execution disabled by default."""

    schema_version: int = 1
    runtime_name: str = "agent-larry-phase8"
    session_id: str = "phase8-bounded-validation"
    broker_profile_id: str = PAPER_PROFILE_ID
    expected_paper_endpoint: str = PAPER_ENDPOINT
    mode: RuntimeMode = RuntimeMode.RESEARCH_ONLY
    paper_execution_authorized: bool = False
    release_decision: ReleaseDecision = ReleaseDecision.GO_FOR_RESEARCH_ONLY
    release_limitations: tuple[str, ...] = ()
    validation_profile: Phase8ValidationProfile = Field(default_factory=Phase8ValidationProfile)
    universe: tuple[str, ...] = ("BTC/USD",)
    accepted_strategies: tuple[StrategySpecification, ...] = ()
    experimental_strategies: tuple[StrategySpecification, ...] = ()
    adaptive_research: AdaptiveResearchConfig = Field(default_factory=AdaptiveResearchConfig)
    cycle_interval_seconds: float = 60.0
    maximum_cycles: int = 1
    pending_order_cancel_after_seconds: float = 30.0
    history_period: str = "1m"
    history_limit: int = 120
    database_path: str = ""
    phase8_ledger_path: str = ""
    report_directory: str = ""
    code_revision: str = "unresolved"
    final_audit_path: str = "agent/PHASE8_INDEPENDENT_AUDIT.md"
    release_audit_sha256: str = ""

    @model_validator(mode="after")
    def _validate_runtime(self) -> "Phase8RuntimeConfig":
        if self.schema_version != 1:
            raise ValueError("unsupported Phase 8 runtime schema_version")
        if self.broker_profile_id != PAPER_PROFILE_ID:
            raise ValueError(f"broker_profile_id must be {PAPER_PROFILE_ID}")
        if self.expected_paper_endpoint.rstrip("/") != PAPER_ENDPOINT:
            raise ValueError("the Alpaca paper endpoint is immutable")
        if not self.universe or len(set(self.universe)) != len(self.universe):
            raise ValueError("universe must contain unique symbols")
        if not self.session_id:
            raise ValueError("session_id is required for restart recovery")
        if self.cycle_interval_seconds <= 0 or self.maximum_cycles <= 0 or self.pending_order_cancel_after_seconds <= 0:
            raise ValueError("cycle controls must be positive")
        if self.history_limit < 25:
            raise ValueError("history_limit must be at least 25")
        all_strategies = (*self.accepted_strategies, *self.experimental_strategies)
        keys = [strategy.key for strategy in all_strategies]
        if len(keys) != len(set(keys)):
            raise ValueError("strategy IDs and versions must be unique")
        if any(strategy.state.value != "ACCEPTED_PAPER" for strategy in self.accepted_strategies):
            raise ValueError("accepted_strategies may contain only ACCEPTED_PAPER versions")
        if any(strategy.state.value != "EXPERIMENTAL_PAPER" for strategy in self.experimental_strategies):
            raise ValueError("experimental_strategies may contain only EXPERIMENTAL_PAPER versions")
        if any(
            not strategy.parent_ids or strategy.creator_component != "phase8-controlled-promotion-v1"
            for strategy in self.experimental_strategies
        ):
            raise ValueError("experimental strategies require controlled-promotion lineage")
        experimental = self.adaptive_research.experimental_risk
        if any(
            strategy.risk.maximum_position_value_usd > experimental.maximum_position_value_usd
            for strategy in self.experimental_strategies
        ):
            raise ValueError("experimental strategy exceeds the experimental position limit")
        if self.release_audit_sha256 and (
            len(self.release_audit_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.release_audit_sha256.lower())
        ):
            raise ValueError("release_audit_sha256 must be a SHA-256 hex digest")
        if self.mode is RuntimeMode.PAPER_EXECUTE and not self.paper_execution_authorized:
            raise ValueError("paper_execute requires explicit paper_execution_authorized=true")
        if (
            self.mode in {RuntimeMode.DRY_RUN, RuntimeMode.PAPER_EXECUTE}
            and self.validation_profile.name != "phase8-bounded-validation-v1"
        ):
            raise ValueError("dry/paper modes require the locked Phase 8 validation profile")
        return self

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self)

    def resolved_database_path(self) -> Path:
        return (
            Path(self.database_path).expanduser()
            if self.database_path
            else get_runtime_root() / "phase8" / "runtime.sqlite3"
        )

    def resolved_phase8_ledger_path(self) -> Path:
        return (
            Path(self.phase8_ledger_path).expanduser()
            if self.phase8_ledger_path
            else get_runtime_root() / "phase8" / "alpaca-paper.sqlite3"
        )

    def resolved_report_directory(self) -> Path:
        return (
            Path(self.report_directory).expanduser()
            if self.report_directory
            else get_runtime_root() / "phase8" / "reports"
        )


_SECRET_TOKENS = ("api_key", "secret", "access_token", "refresh_token", "private_key", "password")


def _reject_embedded_secrets(value: Any, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            if any(token in key_text for token in _SECRET_TOKENS):
                raise ValueError(f"credentials must not be embedded in {path}.{key}")
            _reject_embedded_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_embedded_secrets(child, f"{path}[{index}]")


def load_runtime_config(
    path: str | Path,
    *,
    mode: RuntimeMode | None = None,
    authorize_paper_execution: bool = False,
) -> Phase8RuntimeConfig:
    """Load JSON/YAML and require independent config/CLI paper authority.

    The command-line authorization flag is deliberately *not* written into the
    configuration.  Paper execution is a two-key control: the reviewed config
    must already opt in and the operator must supply the separate CLI flag for
    this invocation.
    """
    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8")
    if config_path.suffix.lower() == ".json":
        raw = json.loads(text)
    else:
        raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError("Phase 8 config root must be an object")
    _reject_embedded_secrets(raw)
    if mode is not None:
        raw["mode"] = mode.value
    config = Phase8RuntimeConfig.model_validate(raw)
    if config.mode is RuntimeMode.PAPER_EXECUTE and not authorize_paper_execution:
        raise ValueError("paper_execute requires the separate CLI authorization flag")
    return config
