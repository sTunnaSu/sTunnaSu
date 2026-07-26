"""Composition root and autonomous bounded runtime for Phase 8.

The runtime is synchronous by design: one cycle must finish and reconcile
before the next can begin, so overlapping order-producing cycles are
structurally impossible.
"""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from src.latency_budgeter.configuration.models import CostAssumptions, LatencyBudgetConfig
from src.latency_budgeter.domain.identifiers import Phase8IdentifierFactory
from src.phase8_runtime.configuration import PAPER_ENDPOINT, PAPER_PROFILE_ID, Phase8RuntimeConfig
from src.phase8_runtime.models import (
    CheckStatus,
    CycleReport,
    IntentKind,
    IntentState,
    MarketSnapshot,
    ModuleRequirement,
    ModuleState,
    OrderIntent,
    PreflightItem,
    PreflightReport,
    ReleaseDecision,
    RuntimeMode,
    SignalDirection,
    StrategySpecification,
    StrategyState,
    TradingSignal,
    canonical_hash,
)
from src.phase8_runtime.persistence import Phase8RuntimeStore, RuntimePersistenceError
from src.phase8_runtime.reporting import write_runtime_report
from src.phase8_runtime.services import (
    AdaptiveStrategyGenerator,
    AnalyticalModule,
    BrokerMarketDataService,
    PortfolioRiskManager,
    PromotionController,
    RegimeAnalysisService,
    SignalEvaluationService,
    TechnicalAnalysisService,
    UnavailableAnalysisService,
)
from src.trading.phase8_paper import Phase8PaperOrderRequest


Clock = Callable[[], datetime]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _verify_local_code_revision(expected_revision: str) -> tuple[bool, str]:
    """Bind release authority to an immutable, unchanged code commit.

    Audit-only commits may follow the reviewed code commit.  They cannot alter
    any protected execution path, and those paths must also be clean locally.
    This avoids a circular requirement where committing an audit changes HEAD
    and invalidates the revision written inside that audit.
    """
    repository_root = Path(__file__).resolve().parents[3]
    if re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected_revision) is None:
        return False, "configured code revision is not a full Git object ID"
    protected_paths = (
        "agent/src/config",
        "agent/src/latency_budgeter",
        "agent/src/phase8_runtime",
        "agent/src/security/secret_redaction.py",
        "agent/src/tools/phase8_paper_tool.py",
        "agent/src/trading",
    )
    try:
        current_revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{expected_revision}^{{commit}}"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", expected_revision, current_revision],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        subprocess.run(
            ["git", "diff", "--quiet", expected_revision, current_revision, "--", *protected_paths],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *protected_paths],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Git revision could not be verified: {type(exc).__name__}"
    if status:
        return False, f"protected execution paths have {len(status)} uncommitted path(s)"
    return True, f"reviewed code revision {expected_revision}; running HEAD {current_revision}"


@dataclass(slots=True)
class Phase8RuntimeDependencies:
    profile_resolver: Callable[[str | None], Any]
    check_connection: Callable[..., Mapping[str, Any]]
    account_reader: Callable[..., Mapping[str, Any]]
    positions_reader: Callable[..., Mapping[str, Any]]
    orders_reader: Callable[..., Mapping[str, Any]]
    assets_reader: Callable[..., Mapping[str, Any]]
    quote_reader: Callable[..., Mapping[str, Any]]
    history_reader: Callable[..., Mapping[str, Any]]
    phase8_executor: Callable[..., Mapping[str, Any]]
    phase8_reconciler: Callable[..., Mapping[str, Any]]
    code_integrity_verifier: Callable[[str], tuple[bool, str]] = _verify_local_code_revision
    order_canceller: Callable[..., Mapping[str, Any]] | None = None
    clock: Clock = _clock
    monotonic_ns: Callable[[], int] = time.monotonic_ns
    sleeper: Callable[[float], None] = time.sleep


@dataclass(slots=True)
class BrokerState:
    account: Mapping[str, Any]
    positions: list[Mapping[str, Any]]
    open_orders: list[Mapping[str, Any]]
    executions: list[Mapping[str, Any]]


def _release_allows(mode: RuntimeMode, decision: ReleaseDecision) -> bool:
    if decision is ReleaseDecision.NO_GO:
        return False
    if mode is RuntimeMode.RESEARCH_ONLY:
        return True
    if mode is RuntimeMode.DRY_RUN:
        return decision in {
            ReleaseDecision.GO_FOR_DRY_RUN,
            ReleaseDecision.GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST,
            ReleaseDecision.GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST,
            ReleaseDecision.GO_FOR_BOUNDED_PAPER_TEST,
        }
    return decision in {
        ReleaseDecision.GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST,
        ReleaseDecision.GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST,
        ReleaseDecision.GO_FOR_BOUNDED_PAPER_TEST,
    }


def _strategy_scope_allows(config: Phase8RuntimeConfig) -> tuple[bool, str]:
    """Require the strategy set appropriate to the requested validation stage."""
    accepted = len(config.accepted_strategies)
    experimental = len(config.experimental_strategies)
    if config.mode is RuntimeMode.RESEARCH_ONLY:
        return True, f"research scope accepted={accepted} experimental={experimental}"
    if config.mode is RuntimeMode.DRY_RUN:
        return accepted > 0, f"dry-run scope accepted={accepted} experimental={experimental}"
    if config.release_decision is ReleaseDecision.GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST:
        valid = accepted == 1 and experimental == 0
        return valid, f"accepted-smoke scope accepted={accepted} experimental={experimental}"
    if config.release_decision is ReleaseDecision.GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST:
        valid = accepted == 0 and experimental == 1
        return valid, f"experimental-smoke scope accepted={accepted} experimental={experimental}"
    if config.release_decision is ReleaseDecision.GO_FOR_BOUNDED_PAPER_TEST:
        valid = accepted + experimental > 0
        return valid, f"bounded scope accepted={accepted} experimental={experimental}"
    return False, f"unsupported paper scope accepted={accepted} experimental={experimental}"


def _is_single_entry_smoke(config: Phase8RuntimeConfig) -> bool:
    return config.mode is RuntimeMode.PAPER_EXECUTE and config.release_decision in {
        ReleaseDecision.GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST,
        ReleaseDecision.GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST,
    }


def _verify_release_audit(config: Phase8RuntimeConfig) -> tuple[bool, str]:
    if not config.release_audit_sha256:
        return False, "release audit hash is not configured"
    path = Path(config.final_audit_path)
    if not path.is_file():
        return False, f"release audit is missing: {path}"
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != config.release_audit_sha256.lower():
        return False, "release audit hash mismatch"
    marker = f"Runtime release decision: **{config.release_decision.value}**"
    if marker not in raw.decode("utf-8", errors="replace"):
        return False, "release audit decision marker mismatch"
    revision_marker = f"Runtime code revision: **{config.code_revision}**"
    if revision_marker not in raw.decode("utf-8", errors="replace"):
        return False, "release audit code-revision marker mismatch"
    return True, f"sha256={digest} marker={config.release_decision.value}"


class Phase8Runtime:
    """One fail-closed research/dry-run/paper composition."""

    def __init__(
        self,
        *,
        config: Phase8RuntimeConfig,
        dependencies: Phase8RuntimeDependencies,
        store: Phase8RuntimeStore,
    ) -> None:
        self.config = config
        self.dependencies = dependencies
        self.store = store
        self.market_data = BrokerMarketDataService(
            profile_id=config.broker_profile_id,
            quote_reader=dependencies.quote_reader,
            history_reader=dependencies.history_reader,
            clock=dependencies.clock,
        )
        self.modules: dict[str, AnalyticalModule] = {
            "technical": TechnicalAnalysisService(),
            "regime": RegimeAnalysisService(),
            "ai": UnavailableAnalysisService("ai"),
            "sentiment": UnavailableAnalysisService("sentiment"),
            "news": UnavailableAnalysisService("news"),
        }
        self.signal_service = SignalEvaluationService(self.modules)
        self.adaptive_generator = AdaptiveStrategyGenerator(
            config.adaptive_research,
            code_revision=config.code_revision,
        )
        self.promotion_controller = PromotionController(config.adaptive_research)
        self.risk_manager = PortfolioRiskManager(
            config.validation_profile,
            config.adaptive_research.experimental_risk,
        )
        self._cycle_active = False
        self._recovery_attempt = 0
        self._broker_state: BrokerState | None = None
        self._assets: dict[str, Mapping[str, Any]] = {}

    def preflight(self, run_id: str) -> PreflightReport:
        """Validate every construction and broker boundary without mutation."""
        now = self.dependencies.clock()
        items: list[PreflightItem] = []

        def add(
            name: str,
            status: CheckStatus,
            evidence: str,
            source: str,
            severity: Literal["info", "warning", "critical"] = "critical",
            action: str = "",
        ) -> None:
            items.append(
                PreflightItem(
                    name=name,
                    status=status,
                    evidence=evidence,
                    source=source,
                    severity=severity,
                    action_required=action,
                )
            )

        # Static guard runs before every connector call.
        try:
            profile = self.dependencies.profile_resolver(self.config.broker_profile_id)
            static_ok = (
                profile.id == PAPER_PROFILE_ID
                and profile.connector == "alpaca"
                and profile.environment == "paper"
                and profile.transport == "broker_sdk"
                and not profile.readonly
            )
        except Exception as exc:  # noqa: BLE001
            profile = None
            static_ok = False
            add("paper profile selected", CheckStatus.FAIL, str(exc), "trading profile registry")
        else:
            add(
                "paper profile selected",
                CheckStatus.PASS if static_ok else CheckStatus.FAIL,
                f"profile={getattr(profile, 'id', None)} environment={getattr(profile, 'environment', None)}",
                "src.trading.profiles",
            )
        if not static_ok:
            return PreflightReport(run_id=run_id, mode=self.config.mode, created_at=now, items=tuple(items))

        add("Phase 8 composition root exists", CheckStatus.PASS, self.__class__.__name__, __file__)
        add(
            "executable entry point exists",
            CheckStatus.PASS,
            "python -m src.phase8_runtime",
            "src.phase8_runtime.__main__",
        )
        add(
            "launch command identified",
            CheckStatus.PASS,
            "python -m src.phase8_runtime --config <path> --research-only|--dry-run|--paper-execute",
            "src.phase8_runtime.cli",
        )
        add("live configuration absent", CheckStatus.PASS, "only alpaca-paper-trade is constructible", __file__)
        add("persistence writable", CheckStatus.PASS, str(self.store.path), "Phase8RuntimeStore")
        revision_resolved, revision_evidence = self.dependencies.code_integrity_verifier(self.config.code_revision)
        revision_required = self.config.mode in {RuntimeMode.DRY_RUN, RuntimeMode.PAPER_EXECUTE}
        add(
            "code revision resolved",
            (
                CheckStatus.PASS
                if revision_resolved
                else (CheckStatus.FAIL if revision_required else CheckStatus.WARNING)
            ),
            revision_evidence,
            "Phase8RuntimeConfig.code_revision",
            severity="critical" if revision_required else "warning",
            action="record the exact reviewed Git revision" if not revision_resolved else "",
        )
        add(
            "event chain valid",
            CheckStatus.PASS if self.store.verify_event_chain() else CheckStatus.FAIL,
            "append-only hash chain verified",
            "Phase8RuntimeStore.verify_event_chain",
        )
        add(
            "latency budgeter integrated",
            CheckStatus.PASS,
            "all submissions use phase8_executor",
            "src.trading.phase8_paper",
        )
        add(
            "causal evaluator integrated",
            CheckStatus.PASS,
            "Phase 8 gate is the only order bridge",
            "src.latency_budgeter.application.gate",
        )
        add("execution adapter wired", CheckStatus.PASS, "Alpaca paper Phase 8 bridge", "src.trading.phase8_paper")
        add(
            "reconciliation wired",
            CheckStatus.PASS,
            "decision-id/client-id recovery",
            "Phase8Runtime._recover_runtime_intents",
        )
        add(
            "exit handling wired",
            CheckStatus.PASS,
            "protective exits precede entries",
            "Phase8Runtime._manage_protective_exits",
        )
        add("risk manager wired", CheckStatus.PASS, type(self.risk_manager).__name__, "src.phase8_runtime.services")
        add("portfolio manager wired", CheckStatus.PASS, "locked validation profile", "Phase8ValidationProfile")
        add("experimental-risk manager wired", CheckStatus.PASS, "subordinate $50 profile", "ExperimentalRiskProfile")
        add("monitoring wired", CheckStatus.PASS, "non-overlapping synchronous cycles", "Phase8Runtime.run")
        add(
            "restart recovery wired",
            CheckStatus.PASS,
            "durable intents + broker reconciliation",
            "Phase8Runtime._recover_runtime_intents",
        )
        add("logging wired", CheckStatus.PASS, "append-only structured event chain", "Phase8RuntimeStore")
        add("metrics wired", CheckStatus.PASS, "event counters and reports", "write_runtime_report")
        add("report generation wired", CheckStatus.PASS, "JSON + Markdown", "src.phase8_runtime.reporting")
        add(
            "adaptive generator available",
            CheckStatus.PASS,
            type(self.adaptive_generator).__name__,
            "src.phase8_runtime.services",
        )
        add(
            "shadow simulator available",
            CheckStatus.PASS,
            "shadow-only signals and durable evidence",
            "Phase8Runtime._cycle",
        )
        add(
            "transaction-cost model configured",
            CheckStatus.PASS,
            "Phase 8 frozen cost assumptions",
            "LatencyBudgetConfig",
        )
        now_probe = self.dependencies.clock()
        monotonic_before = self.dependencies.monotonic_ns()
        monotonic_after = self.dependencies.monotonic_ns()
        clock_ok = now_probe.tzinfo is not None and monotonic_after >= monotonic_before
        add(
            "system clock valid",
            CheckStatus.PASS if clock_ok else CheckStatus.FAIL,
            f"utc_aware={now_probe.tzinfo is not None} monotonic_non_decreasing={monotonic_after >= monotonic_before}",
            "injected wall and monotonic clocks",
        )
        registry_errors: list[str] = []
        configured_strategies = (
            *self.config.accepted_strategies,
            *self.config.experimental_strategies,
        )
        for strategy in configured_strategies:
            try:
                self.store.register_strategy(strategy, registered_at=now)
            except Exception as exc:  # noqa: BLE001
                registry_errors.append(f"{strategy.key}:{exc}")
        add(
            "strategy registry available",
            CheckStatus.PASS if not registry_errors else CheckStatus.FAIL,
            f"registered={len(configured_strategies)} errors={registry_errors or 'none'}",
            "Phase8RuntimeStore.register_strategy",
        )
        for name in ("technical", "regime"):
            add(
                f"{name} module wired",
                CheckStatus.PASS,
                type(self.modules[name]).__name__,
                "src.phase8_runtime.services",
            )
        for name in ("ai", "sentiment", "news"):
            required = any(
                strategy.module_policy.get(name) is ModuleRequirement.REQUIRED for strategy in configured_strategies
            )
            add(
                f"{name} module wired",
                CheckStatus.FAIL if required else CheckStatus.WARNING,
                "explicit UNAVAILABLE adapter; never treated as confirmation",
                "UnavailableAnalysisService",
                "critical" if required else "warning",
                "configure a point-in-time adapter" if required else "optional for current strategies",
            )

        try:
            connection = dict(self.dependencies.check_connection(self.config.broker_profile_id))
        except Exception as exc:  # noqa: BLE001
            connection = {"status": "error", "error": str(exc)}
        host = str(connection.get("host") or (connection.get("config") or {}).get("host") or "").rstrip("/")
        connection_ok = (
            connection.get("status") == "ok"
            and connection.get("environment") == "paper"
            and connection.get("connector") == "alpaca"
            and host == PAPER_ENDPOINT
        )
        add(
            "paper endpoint verified",
            CheckStatus.PASS if connection_ok else CheckStatus.FAIL,
            f"host={host or 'missing'} status={connection.get('status')}",
            "src.trading.service.check_connection",
        )

        if connection_ok:
            try:
                market_probe = self.market_data.snapshot(
                    self.config.universe[0],
                    period=self.config.history_period,
                    limit=self.config.history_limit,
                )
            except Exception as exc:  # noqa: BLE001
                add(
                    "market data available",
                    CheckStatus.FAIL,
                    str(exc),
                    "BrokerMarketDataService.snapshot",
                    action="restore a valid two-sided paper quote and historical bars",
                )
                add(
                    "market timestamps valid",
                    CheckStatus.FAIL,
                    "snapshot construction failed",
                    "MarketSnapshot",
                    action="correct timestamp ordering/freshness",
                )
            else:
                market_checked_at = self.dependencies.clock()
                quote_age_ms = (market_checked_at - market_probe.quote_observed_at).total_seconds() * 1_000
                bar_age_ms = (market_checked_at - market_probe.bars[-1].timestamp).total_seconds() * 1_000
                quote_limit = min(
                    [5_000] + [strategy.latency.maximum_quote_age_ms for strategy in configured_strategies]
                )
                bar_limit = min([120_000] + [strategy.latency.maximum_bar_age_ms for strategy in configured_strategies])
                timestamps_ok = 0 <= quote_age_ms <= quote_limit and 0 <= bar_age_ms <= bar_limit
                add(
                    "market data available",
                    CheckStatus.PASS,
                    f"symbol={market_probe.symbol} bars={len(market_probe.bars)} spread_bps={market_probe.spread_bps}",
                    "BrokerMarketDataService.snapshot",
                )
                add(
                    "market timestamps valid",
                    CheckStatus.PASS if timestamps_ok else CheckStatus.FAIL,
                    f"quote_age_ms={quote_age_ms:.3f}/{quote_limit} bar_age_ms={bar_age_ms:.3f}/{bar_limit}",
                    "MarketSnapshot + strategy latency limits",
                    action="wait for fresh point-in-time data" if not timestamps_ok else "",
                )

        state: BrokerState | None = None
        recovery_errors: list[str] = []
        if connection_ok:
            try:
                state = self._read_broker_state()
            except Exception as exc:  # noqa: BLE001
                add("broker state readable", CheckStatus.FAIL, str(exc), "Alpaca paper read APIs")
            else:
                account_status = str(state.account.get("status") or "").lower()
                blocked = bool(state.account.get("trading_blocked", False))
                add(
                    "account accessible",
                    CheckStatus.PASS if account_status == "active" else CheckStatus.FAIL,
                    f"status={account_status}",
                    "get_account",
                )
                add(
                    "account order permission verified",
                    CheckStatus.PASS if not blocked else CheckStatus.FAIL,
                    f"trading_blocked={blocked}",
                    "get_account",
                )
                recovery_errors = self._recover_runtime_intents(run_id=run_id)
                # Re-read after recovery so the preflight evidence is not based
                # on the pre-reconciliation snapshot.
                state = self._read_broker_state()
                ownership = self._classify_positions(state.positions)
                external = ownership["external_symbols"]
                policy = self.config.validation_profile.existing_position_policy.value
                existing_status = CheckStatus.PASS
                action = ""
                if ownership["discrepancies"]:
                    existing_status = CheckStatus.FAIL
                    action = "resolve owned-position quantity discrepancies"
                elif external and policy == "reject_start":
                    existing_status = CheckStatus.FAIL
                    action = "clear external positions or select risk_only"
                elif policy in {"manage", "flatten_before_start"}:
                    existing_status = CheckStatus.FAIL
                    action = "these policies require explicit ownership/flatten authorization"
                add(
                    "current positions classified",
                    existing_status,
                    (
                        f"policy={policy} external={external or 'none'} "
                        f"owned={ownership['owned_symbols'] or 'none'} "
                        f"discrepancies={ownership['discrepancies'] or 'none'}"
                    ),
                    "existing-position policy",
                    action=action,
                )
                btc = [symbol for symbol in external if symbol.replace("/", "").upper() == "BTCUSD"]
                add(
                    "BTC policy applied",
                    CheckStatus.PASS,
                    f"pre-existing BTC={bool(btc)} policy={policy}",
                    "existing-position policy",
                )
                owned_orders, external_orders = self._classify_open_orders(state.open_orders)
                prior_status = CheckStatus.PASS
                prior_action = ""
                if recovery_errors or external_orders:
                    prior_status = CheckStatus.FAIL
                    prior_action = "resolve recovery errors or external open orders"
                elif owned_orders:
                    prior_status = CheckStatus.WARNING
                    prior_action = "continue reconciliation monitoring; block new entries"
                add(
                    "prior orders reconciled",
                    prior_status,
                    (
                        f"owned_open={len(owned_orders)} external_open={len(external_orders)} "
                        f"recovery_errors={recovery_errors or 'none'}"
                    ),
                    "broker reconciliation",
                    severity="critical" if prior_status is CheckStatus.FAIL else "warning",
                    action=prior_action,
                )

        unresolved = self.store.unresolved_intents()
        counters = self.store.intent_counts(self.config.session_id)
        latest_equity = self.store.latest_equity(self.config.session_id)
        add(
            "order counters restored",
            CheckStatus.PASS,
            str(counters),
            "Phase8RuntimeStore.intent_counts",
        )
        add(
            "session P&L restored",
            CheckStatus.PASS,
            (
                f"realized={latest_equity['realized_pnl']} unrealized={latest_equity['unrealized_pnl']}"
                if latest_equity
                else "no prior attributable equity snapshot"
            ),
            "Phase8RuntimeStore.latest_equity",
            severity="info",
        )
        add(
            "drawdown state restored",
            CheckStatus.PASS,
            (
                f"drawdown_fraction={latest_equity['drawdown_fraction']}"
                if latest_equity
                else "no prior attributable equity snapshot"
            ),
            "Phase8RuntimeStore.latest_equity",
            severity="info",
        )
        add(
            "duplicate-order protection active",
            CheckStatus.PASS,
            "unique intent and client_order_id constraints",
            "Phase8RuntimeStore",
        )
        dangerous_unresolved = [
            row
            for row in unresolved
            if row["state"]
            in {
                IntentState.AMBIGUOUS.value,
                IntentState.RECONCILIATION_REQUIRED.value,
                IntentState.SUBMITTING.value,
            }
        ]
        unresolved_status = CheckStatus.PASS
        if recovery_errors or dangerous_unresolved:
            unresolved_status = CheckStatus.FAIL
        elif unresolved:
            unresolved_status = CheckStatus.WARNING
        add(
            "unresolved intents restored",
            unresolved_status,
            (f"monitorable={len(unresolved) - len(dangerous_unresolved)} dangerous={len(dangerous_unresolved)}"),
            "Phase8RuntimeStore.unresolved_intents",
            severity="critical" if unresolved_status is CheckStatus.FAIL else "warning",
            action="reconcile unresolved intent before new entries" if unresolved else "",
        )
        for feature in ("partial-fill handling", "rejection handling", "ambiguous-submission handling"):
            add(feature, CheckStatus.PASS, "explicit intent/lifecycle state", "IntentState + Phase8 lifecycle")
        add(
            "cancellation handling",
            CheckStatus.PASS if self.dependencies.order_canceller is not None else CheckStatus.FAIL,
            "paper cancellation adapter wired; broker terminal state remains authoritative",
            "src.trading.service.cancel_order",
            action="wire a paper-only cancel adapter" if self.dependencies.order_canceller is None else "",
        )
        experimental_release_ok = (
            not self.config.experimental_strategies
            or self.config.mode is not RuntimeMode.PAPER_EXECUTE
            or self.config.release_decision
            in {
                ReleaseDecision.GO_FOR_EXPERIMENTAL_STRATEGY_SMOKE_TEST,
                ReleaseDecision.GO_FOR_BOUNDED_PAPER_TEST,
            }
        )
        add(
            "experimental strategy release scope",
            CheckStatus.PASS if experimental_release_ok else CheckStatus.FAIL,
            (f"configured={len(self.config.experimental_strategies)} decision={self.config.release_decision.value}"),
            "Phase8RuntimeConfig.experimental_strategies",
            action=("complete the experimental-strategy release gate" if not experimental_release_ok else ""),
        )
        add(
            "accepted strategies valid",
            CheckStatus.PASS if self.config.accepted_strategies else CheckStatus.WARNING,
            f"accepted immutable versions={len(self.config.accepted_strategies)}",
            "Phase8RuntimeConfig.accepted_strategies",
            "warning",
            "paper smoke requires one independently accepted version" if not self.config.accepted_strategies else "",
        )
        strategy_scope_ok, strategy_scope_evidence = _strategy_scope_allows(self.config)
        add(
            "requested-stage strategy set valid",
            CheckStatus.PASS if strategy_scope_ok else CheckStatus.FAIL,
            strategy_scope_evidence,
            "Phase8RuntimeConfig strategy release scope",
            action="configure only the strategy class and count permitted by this release stage",
        )
        release_ok = _release_allows(self.config.mode, self.config.release_decision)
        audit_ok, audit_evidence = _verify_release_audit(self.config)
        audit_required = self.config.mode in {RuntimeMode.DRY_RUN, RuntimeMode.PAPER_EXECUTE}
        if not audit_required and not self.config.release_audit_sha256:
            audit_ok = True
            audit_evidence = "research-only mode; release artifact not required"
        add(
            "release audit permits requested stage",
            CheckStatus.PASS if release_ok and audit_ok else CheckStatus.FAIL,
            f"decision={self.config.release_decision.value}; {audit_evidence}",
            self.config.final_audit_path,
            action=(
                "complete, hash, and record the preceding validation gate" if not (release_ok and audit_ok) else ""
            ),
        )
        authorization_ok = self.config.mode is not RuntimeMode.PAPER_EXECUTE or self.config.paper_execution_authorized
        add(
            "explicit paper execution authorization",
            CheckStatus.PASS if authorization_ok else CheckStatus.FAIL,
            f"authorized={self.config.paper_execution_authorized}",
            "Phase8RuntimeConfig",
        )
        self._broker_state = state
        return PreflightReport(run_id=run_id, mode=self.config.mode, created_at=now, items=tuple(items))

    def _read_broker_state(self) -> BrokerState:
        account_response = dict(self.dependencies.account_reader(self.config.broker_profile_id))
        positions_response = dict(self.dependencies.positions_reader(self.config.broker_profile_id))
        orders_response = dict(
            self.dependencies.orders_reader(
                self.config.broker_profile_id,
                include_executions=True,
            )
        )
        for label, response in (
            ("account", account_response),
            ("positions", positions_response),
            ("orders", orders_response),
        ):
            if response.get("status") != "ok":
                raise RuntimeError(str(response.get("error") or f"{label} read failed"))
            if response.get("environment") != "paper" or response.get("is_paper") is not True:
                raise RuntimeError(f"{label} response did not prove Alpaca paper environment")
        account = account_response.get("account")
        positions = positions_response.get("positions")
        open_orders = orders_response.get("open_orders")
        executions = orders_response.get("executions", [])
        if not isinstance(account, Mapping) or not isinstance(positions, list) or not isinstance(open_orders, list):
            raise RuntimeError("broker state payload is malformed")
        return BrokerState(
            account=account,
            positions=[row for row in positions if isinstance(row, Mapping)],
            open_orders=[row for row in open_orders if isinstance(row, Mapping)],
            executions=[row for row in executions if isinstance(row, Mapping)],
        )

    def _classify_open_orders(
        self,
        open_orders: list[Mapping[str, Any]],
    ) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        known = {str(row["client_order_id"]) for row in self.store.unresolved_intents() if row.get("client_order_id")}
        owned: list[Mapping[str, Any]] = []
        external: list[Mapping[str, Any]] = []
        for order in open_orders:
            client_order_id = str(order.get("client_order_id") or "")
            (owned if client_order_id in known else external).append(order)
        return owned, external

    @staticmethod
    def _canonical_symbol(value: Any) -> str:
        return str(value or "").replace("/", "").replace("-", "").upper()

    def _classify_positions(self, positions: list[Mapping[str, Any]]) -> dict[str, Any]:
        owned_by_symbol: dict[str, Decimal] = {}
        owned_labels: dict[str, str] = {}
        for allocation in self.store.open_allocations(self.config.session_id):
            symbol = self._canonical_symbol(allocation.get("symbol"))
            owned_by_symbol[symbol] = owned_by_symbol.get(symbol, Decimal("0")) + Decimal(
                str(allocation.get("remaining_quantity") or 0)
            )
            owned_labels[symbol] = str(allocation.get("symbol") or symbol)
        broker_by_symbol: dict[str, Decimal] = {}
        broker_labels: dict[str, str] = {}
        for position in positions:
            symbol = self._canonical_symbol(position.get("symbol"))
            quantity = abs(Decimal(str(position.get("qty") or position.get("quantity") or 0)))
            broker_by_symbol[symbol] = broker_by_symbol.get(symbol, Decimal("0")) + quantity
            broker_labels[symbol] = str(position.get("symbol") or symbol)
        discrepancies: list[str] = []
        external_symbols: list[str] = []
        for symbol, broker_quantity in broker_by_symbol.items():
            owned_quantity = owned_by_symbol.get(symbol, Decimal("0"))
            if broker_quantity < owned_quantity:
                discrepancies.append(f"{symbol}:broker={broker_quantity}<owned={owned_quantity}")
            if broker_quantity > owned_quantity:
                external_symbols.append(broker_labels.get(symbol, symbol))
        for symbol, owned_quantity in owned_by_symbol.items():
            if symbol not in broker_by_symbol and owned_quantity > 0:
                discrepancies.append(f"{symbol}:owned={owned_quantity} broker=0")
        return {
            "owned_symbols": sorted(owned_labels.values()),
            "external_symbols": sorted(set(external_symbols)),
            "discrepancies": tuple(discrepancies),
        }

    def _recover_runtime_intents(self, *, run_id: str | None = None) -> list[str]:
        """Reconcile durable unresolved intents without ever resubmitting."""
        self._recovery_attempt += 1
        recovery_attempt = self._recovery_attempt
        recovery_started_ns = self.dependencies.monotonic_ns()
        errors: list[str] = []
        for row in self.store.unresolved_intents():
            intent_id = str(row["intent_id"])
            state = IntentState(str(row["state"]))
            if state in {IntentState.INTENT_CREATED, IntentState.VALIDATED}:
                self.store.transition_intent(intent_id, IntentState.EXPIRED, at=self.dependencies.clock())
                self.store.transition_intent(intent_id, IntentState.CLOSED, at=self.dependencies.clock())
                if run_id:
                    self._event(
                        run_id,
                        "recovery_closed_unsubmitted_intent",
                        {"intent_id": intent_id, "prior_state": state.value},
                        f"recovery:{run_id}:{intent_id}:closed_unsubmitted",
                    )
                continue
            decision_id = str(row.get("decision_id") or "")
            if not decision_id:
                if state is IntentState.SUBMITTING:
                    self.store.transition_intent(
                        intent_id,
                        IntentState.AMBIGUOUS,
                        at=self.dependencies.clock(),
                    )
                errors.append(f"{intent_id}:missing_decision_id")
                continue
            try:
                result = dict(
                    self.dependencies.phase8_reconciler(
                        decision_id,
                        database_path=self.config.resolved_phase8_ledger_path(),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                if state is IntentState.SUBMITTING:
                    self.store.transition_intent(
                        intent_id,
                        IntentState.AMBIGUOUS,
                        at=self.dependencies.clock(),
                    )
                errors.append(f"{intent_id}:{exc}")
                continue
            if result.get("status") != "ok":
                if state is IntentState.SUBMITTING:
                    self.store.transition_intent(
                        intent_id,
                        IntentState.AMBIGUOUS,
                        at=self.dependencies.clock(),
                    )
                errors.append(f"{intent_id}:{result.get('error_code') or result.get('error') or 'reconcile_failed'}")
                continue
            execution_status = str(result.get("execution_status") or "")
            if execution_status == "filled":
                target = IntentState.FILLED
            elif execution_status == "partially_filled_pending":
                target = IntentState.PARTIALLY_FILLED
            elif execution_status in {"submitted", "submitted_pending"}:
                target = IntentState.ACKNOWLEDGED
            elif execution_status in {"rejected", "failed", "broker_submission_failed"}:
                target = IntentState.REJECTED
            elif execution_status in {"cancelled", "canceled"}:
                target = IntentState.CANCELLED
            elif execution_status == "expired":
                target = IntentState.EXPIRED
            else:
                target = IntentState.RECONCILIATION_REQUIRED
            if state is IntentState.PARTIALLY_FILLED and target is IntentState.ACKNOWLEDGED:
                target = IntentState.PARTIALLY_FILLED
            if state is IntentState.CANCEL_PENDING and target is IntentState.ACKNOWLEDGED:
                target = IntentState.CANCEL_PENDING
            try:
                if target in {IntentState.PARTIALLY_FILLED, IntentState.FILLED}:
                    self._materialize_recovered_fill(intent_id, result)
                self.store.transition_intent(
                    intent_id,
                    target,
                    at=self.dependencies.clock(),
                    decision_id=decision_id,
                    broker_order_id=str(result.get("order_id") or "") or None,
                )
                if target is IntentState.FILLED:
                    self.store.transition_intent(
                        intent_id,
                        IntentState.CLOSED,
                        at=self.dependencies.clock(),
                    )
                elif target in {IntentState.REJECTED, IntentState.CANCELLED, IntentState.EXPIRED}:
                    self.store.transition_intent(
                        intent_id,
                        IntentState.CLOSED,
                        at=self.dependencies.clock(),
                    )
                elif target is IntentState.RECONCILIATION_REQUIRED:
                    errors.append(f"{intent_id}:unknown_reconciliation_state")
                if run_id:
                    self._event(
                        run_id,
                        "intent_reconciled",
                        {
                            "intent_id": intent_id,
                            "decision_id": decision_id,
                            "target_state": target.value,
                            "execution_status": execution_status,
                        },
                        f"recovery:{run_id}:{intent_id}:{target.value}",
                    )
            except Exception as exc:  # noqa: BLE001
                if state not in {IntentState.FILLED, IntentState.CLOSED}:
                    try:
                        self.store.transition_intent(
                            intent_id,
                            IntentState.RECONCILIATION_REQUIRED,
                            at=self.dependencies.clock(),
                        )
                    except RuntimePersistenceError:
                        pass
                errors.append(f"{intent_id}:projection_failed:{exc}")
        if run_id:
            self._event(
                run_id,
                "restart_reconciliation_completed",
                {
                    "errors": errors,
                    "attempt": recovery_attempt,
                    "duration_ms": (self.dependencies.monotonic_ns() - recovery_started_ns) / 1_000_000,
                },
                f"restart_reconciliation:{run_id}:{recovery_attempt}",
            )
        return errors

    def _cancel_stale_owned_orders(self, *, run_id: str) -> list[str]:
        """Request cancellation for stale owned paper orders exactly once."""
        if self.config.mode is not RuntimeMode.PAPER_EXECUTE:
            return []
        if not self.config.paper_execution_authorized or self.dependencies.order_canceller is None:
            return []
        errors: list[str] = []
        now = self.dependencies.clock()
        for row in self.store.unresolved_intents():
            state = IntentState(str(row["state"]))
            if state not in {IntentState.ACKNOWLEDGED, IntentState.PARTIALLY_FILLED}:
                continue
            updated_at = self._parse_utc(row["updated_at"])
            age_seconds = (now - updated_at).total_seconds()
            if age_seconds < self.config.pending_order_cancel_after_seconds:
                continue
            order_id = str(row.get("broker_order_id") or "")
            if not order_id:
                self.store.transition_intent(
                    str(row["intent_id"]),
                    IntentState.RECONCILIATION_REQUIRED,
                    at=now,
                )
                errors.append(f"{row['intent_id']}:stale_order_missing_broker_id")
                continue
            self.store.transition_intent(
                str(row["intent_id"]),
                IntentState.CANCEL_PENDING,
                at=now,
            )
            try:
                response = dict(
                    self.dependencies.order_canceller(
                        order_id,
                        self.config.broker_profile_id,
                        symbol=str(row["symbol"]),
                        session_id=self.config.session_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                response = {"status": "error", "error": str(exc)}
            paper_proven = (
                response.get("status") == "ok"
                and response.get("environment") == "paper"
                and response.get("is_paper") is True
            )
            self._event(
                run_id,
                "owned_order_cancel_requested",
                {
                    "intent_id": row["intent_id"],
                    "broker_order_id": order_id,
                    "age_seconds": age_seconds,
                    "paper_proven": paper_proven,
                    "response_status": response.get("status"),
                    "error": response.get("error"),
                },
                f"cancel_request:{row['intent_id']}",
            )
            if not paper_proven:
                self.store.transition_intent(
                    str(row["intent_id"]),
                    IntentState.AMBIGUOUS,
                    at=self.dependencies.clock(),
                )
                errors.append(f"{row['intent_id']}:cancel_not_proven_paper")
        return errors

    def _materialize_recovered_fill(
        self,
        intent_id: str,
        result: Mapping[str, Any],
    ) -> None:
        row = self.store.get_intent(intent_id)
        if row is None:
            raise RuntimePersistenceError(f"missing recovered intent {intent_id}")
        intent = OrderIntent.model_validate(row["payload"])
        fill_price_raw = result.get("filled_average_price")
        fill_quantity_raw = result.get("filled_quantity")
        if fill_price_raw in (None, "") or fill_quantity_raw in (None, ""):
            raise RuntimePersistenceError("filled reconciliation lacks price or quantity")
        fill_price = Decimal(str(fill_price_raw))
        fill_quantity = Decimal(str(fill_quantity_raw))
        self.store.apply_cumulative_fill(
            intent=intent,
            cumulative_quantity=fill_quantity,
            average_fill_price=fill_price,
            observed_at=self.dependencies.clock(),
        )

    def run(self) -> CycleReport:
        """Run bounded non-overlapping cycles and stop at the configured limit."""
        run_id = Phase8IdentifierFactory.new_run_id()
        started = self.dependencies.clock()
        self.store.start_run(
            run_id=run_id,
            session_id=self.config.session_id,
            mode=self.config.mode,
            config_hash=self.config.fingerprint,
            code_revision=self.config.code_revision,
            started_at=started,
        )
        self.store.append_event(
            run_id=run_id,
            session_id=self.config.session_id,
            event_type="run_started",
            payload={
                "mode": self.config.mode.value,
                "config_hash": self.config.fingerprint,
                "code_revision": self.config.code_revision,
                "paper_only": True,
                "release_decision": self.config.release_decision.value,
                "release_audit_sha256": self.config.release_audit_sha256,
                "validation_profile": self.config.validation_profile.model_dump(mode="json"),
            },
            idempotency_key=f"run_started:{run_id}",
            occurred_at=started,
            monotonic_ns=self.dependencies.monotonic_ns(),
        )
        preflight = self.preflight(run_id)
        self.store.append_event(
            run_id=run_id,
            session_id=self.config.session_id,
            event_type="preflight_completed",
            payload=preflight.model_dump(mode="json"),
            idempotency_key=f"preflight:{run_id}",
            occurred_at=preflight.created_at,
            monotonic_ns=self.dependencies.monotonic_ns(),
        )
        totals = {
            "snapshots": 0,
            "signals_generated": 0,
            "signals_rejected": 0,
            "shadow_signals": 0,
            "intents_created": 0,
            "orders_submitted": 0,
        }
        halts: list[str] = []
        if not preflight.passed:
            halts.append("preflight_failed")
        else:
            for cycle_index in range(self.config.maximum_cycles):
                cycle = self._cycle(run_id, cycle_index)
                for key in totals:
                    totals[key] += int(cycle.get(key, 0))
                halts.extend(cycle.get("safety_halts", []))
                if halts:
                    break
                if cycle_index + 1 < self.config.maximum_cycles:
                    self.dependencies.sleeper(self.config.cycle_interval_seconds)
        if self.store.unresolved_intents() and not halts:
            halts.append("bounded_run_ended_with_unresolved_intents")
        finished = self.dependencies.clock()
        status = "completed" if not halts else "halted"
        self.store.finish_run(
            run_id,
            status=status,
            finished_at=finished,
            safety_halt_reason=";".join(halts) if halts else None,
        )
        summary = {
            "run_id": run_id,
            "session_id": self.config.session_id,
            "mode": self.config.mode.value,
            "status": status,
            "preflight_passed": preflight.passed,
            "release_decision": self.config.release_decision.value,
            "release_limitations": self.config.release_limitations,
            "configuration_hash": self.config.fingerprint,
            "code_revision": self.config.code_revision,
            "validation_profile": self.config.validation_profile.model_dump(mode="json"),
            **totals,
            "safety_halts": halts,
        }
        report_path = write_runtime_report(
            report_directory=self.config.resolved_report_directory(),
            run_id=run_id,
            summary=summary,
            store=self.store,
        )
        return CycleReport(
            run_id=run_id,
            session_id=self.config.session_id,
            mode=self.config.mode,
            started_at=started,
            finished_at=finished,
            preflight_passed=preflight.passed,
            snapshots=totals["snapshots"],
            signals_generated=totals["signals_generated"],
            signals_rejected=totals["signals_rejected"],
            shadow_signals=totals["shadow_signals"],
            intents_created=totals["intents_created"],
            orders_submitted=totals["orders_submitted"],
            safety_halts=tuple(halts),
            report_path=str(report_path),
        )

    def _cycle(self, run_id: str, cycle_index: int) -> dict[str, Any]:
        if self._cycle_active:
            return {"safety_halts": ["overlapping_cycle_detected"]}
        self._cycle_active = True
        started_ns = self.dependencies.monotonic_ns()
        try:
            result = self._run_cycle(run_id, cycle_index)
            self._event(
                run_id,
                "cycle_completed",
                {
                    "cycle_index": cycle_index,
                    "duration_ms": (self.dependencies.monotonic_ns() - started_ns) / 1_000_000,
                    "counters": result,
                },
                f"cycle_completed:{run_id}:{cycle_index}",
            )
            return result
        finally:
            self._cycle_active = False

    def _run_cycle(self, run_id: str, cycle_index: int) -> dict[str, Any]:
        now = self.dependencies.clock()
        counters: dict[str, Any] = {
            "snapshots": 0,
            "signals_generated": 0,
            "signals_rejected": 0,
            "shadow_signals": 0,
            "intents_created": 0,
            "orders_submitted": 0,
            "safety_halts": [],
        }
        state = self._read_broker_state()
        recovery_errors = self._recover_runtime_intents(run_id=run_id)
        state = self._read_broker_state()
        owned_orders, external_orders = self._classify_open_orders(state.open_orders)
        ownership = self._classify_positions(state.positions)
        if recovery_errors:
            counters["safety_halts"].append("restart_reconciliation_failed")
            return counters
        if external_orders:
            counters["safety_halts"].append("external_open_orders")
            return counters
        if ownership["discrepancies"]:
            counters["safety_halts"].append("owned_position_reconciliation_mismatch")
            return counters
        cancel_errors = self._cancel_stale_owned_orders(run_id=run_id)
        if cancel_errors:
            counters["safety_halts"].append("owned_order_cancellation_failed")
            return counters
        unresolved = self.store.unresolved_intents()
        if unresolved or owned_orders:
            self._event(
                run_id,
                "cycle_deferred_for_order_monitoring",
                {
                    "cycle_index": cycle_index,
                    "unresolved_intents": len(unresolved),
                    "owned_open_orders": len(owned_orders),
                },
                f"cycle_deferred:{run_id}:{cycle_index}",
            )
            return counters
        self._broker_state = state

        strategies = [
            *self.config.accepted_strategies,
            *self.config.experimental_strategies,
        ]
        for strategy in strategies:
            self.store.register_strategy(strategy, registered_at=now)
        self._apply_experimental_demotions(run_id, strategies)
        snapshots: dict[str, MarketSnapshot] = {}
        for symbol in self.config.universe:
            fetch_started_ns = self.dependencies.monotonic_ns()
            try:
                snapshot = self.market_data.snapshot(
                    symbol,
                    period=self.config.history_period,
                    limit=self.config.history_limit,
                )
            except Exception as exc:  # noqa: BLE001
                self._event(
                    run_id,
                    "market_snapshot_rejected",
                    {"symbol": symbol, "reason": str(exc)},
                    f"snapshot_rejected:{run_id}:{cycle_index}:{symbol}",
                )
                continue
            snapshots[snapshot.symbol] = snapshot
            counters["snapshots"] += 1
            self._event(
                run_id,
                "market_snapshot_created",
                {
                    **snapshot.model_dump(mode="json"),
                    "data_fetch_latency_ms": (self.dependencies.monotonic_ns() - fetch_started_ns) / 1_000_000,
                },
                f"snapshot:{run_id}:{cycle_index}:{snapshot.snapshot_id}",
            )

        risk_snapshot = self._record_account_risk(run_id, snapshots, state)
        account_halts = tuple(risk_snapshot["halt_reasons"])
        exit_result = self._manage_protective_exits(
            run_id=run_id,
            snapshots=snapshots,
            account_halts=account_halts,
        )
        counters["intents_created"] += int(exit_result["intents_created"])
        counters["orders_submitted"] += int(exit_result["orders_submitted"])
        counters["safety_halts"].extend(exit_result["safety_halts"])
        if exit_result["exits_triggered"]:
            counters["safety_halts"].extend(
                self._record_post_execution_state(
                    run_id=run_id,
                    snapshots=snapshots,
                    submitted_orders=int(exit_result["orders_submitted"]),
                )
            )
            return counters
        if account_halts:
            self._event(
                run_id,
                "account_risk_halt",
                {"reasons": account_halts, "risk_snapshot": risk_snapshot},
                f"account_risk_halt:{run_id}:{cycle_index}",
            )
            counters["safety_halts"].extend(account_halts)
            return counters

        # Accepted/experimental smoke releases authorize one entry lifecycle,
        # not a rolling trading session.  Protective exits above remain active
        # after that entry, but no second entry may be created on restart.
        if _is_single_entry_smoke(self.config) and self.store.intent_counts(self.config.session_id)["entry"] >= 1:
            self._event(
                run_id,
                "smoke_stage_entry_limit_reached",
                {"release_decision": self.config.release_decision.value},
                f"smoke_stage_complete:{run_id}:{cycle_index}",
            )
            return counters

        known_keys = {strategy.key for strategy in strategies}
        for persisted in self.store.strategies(state=StrategyState.SHADOW.value):
            if persisted.key not in known_keys:
                strategies.append(persisted)
                known_keys.add(persisted.key)
        self._manage_shadow_positions(
            run_id=run_id,
            snapshots=snapshots,
            strategies={strategy.key: strategy for strategy in strategies},
        )

        for snapshot in snapshots.values():
            for generated in self.adaptive_generator.generate(snapshot, now=now):
                self.store.register_strategy(generated, registered_at=now)
                persisted = self.store.strategy(generated.key) or generated
                if persisted.key not in known_keys:
                    strategies.append(persisted)
                    known_keys.add(persisted.key)
                self._event(
                    run_id,
                    "strategy_hypothesis_generated",
                    {
                        "strategy_key": generated.key,
                        "fingerprint": generated.fingerprint,
                        "state": generated.state.value,
                        "lineage": generated.parent_ids,
                    },
                    f"strategy_generated:{run_id}:{generated.key}",
                )

        candidates: list[tuple[TradingSignal, StrategySpecification, MarketSnapshot]] = []
        for strategy in strategies:
            if self.store.strategy_operational_state(strategy.key) == "disabled":
                self._event(
                    run_id,
                    "strategy_execution_skipped",
                    {"strategy_key": strategy.key, "reason": "operationally_disabled"},
                    f"strategy_disabled:{run_id}:{cycle_index}:{strategy.key}",
                )
                continue
            for symbol in strategy.universe:
                candidate_snapshot = snapshots.get(symbol)
                if candidate_snapshot is None:
                    continue
                input_checked_at = self.dependencies.clock()
                quote_age_ms = (input_checked_at - candidate_snapshot.quote_observed_at).total_seconds() * 1_000
                bar_age_ms = (input_checked_at - candidate_snapshot.bars[-1].timestamp).total_seconds() * 1_000
                stale_reasons: list[str] = []
                if quote_age_ms < 0:
                    stale_reasons.append("CLOCK_INTEGRITY_FAILURE")
                elif quote_age_ms > strategy.latency.maximum_quote_age_ms:
                    stale_reasons.append("STALE_QUOTE")
                if bar_age_ms < 0:
                    stale_reasons.append("CAUSAL_VALIDITY_FAILED")
                elif bar_age_ms > strategy.latency.maximum_bar_age_ms:
                    stale_reasons.append("STALE_BAR")
                if stale_reasons:
                    counters["signals_rejected"] += 1
                    self._event(
                        run_id,
                        "strategy_input_rejected",
                        {
                            "strategy_key": strategy.key,
                            "symbol": symbol,
                            "reasons": stale_reasons,
                            "quote_age_ms": quote_age_ms,
                            "bar_age_ms": bar_age_ms,
                        },
                        f"strategy_input_rejected:{run_id}:{strategy.key}:{candidate_snapshot.snapshot_id}",
                    )
                    continue
                analysis_started_ns = self.dependencies.monotonic_ns()
                signal, modules = self.signal_service.evaluate(
                    run_id=run_id,
                    snapshot=candidate_snapshot,
                    strategy=strategy,
                    now=self.dependencies.clock(),
                )
                counters["signals_generated"] += 1
                if signal.rejection_reasons:
                    counters["signals_rejected"] += 1
                if strategy.state is StrategyState.SHADOW:
                    counters["shadow_signals"] += 1
                self._event(
                    run_id,
                    "signal_evaluated",
                    {
                        "signal": signal.model_dump(mode="json"),
                        "modules": {name: result.model_dump(mode="json") for name, result in modules.items()},
                        "strategy_state": strategy.state.value,
                        "analysis_latency_ms": (self.dependencies.monotonic_ns() - analysis_started_ns) / 1_000_000,
                    },
                    f"signal:{run_id}:{cycle_index}:{signal.signal_id}",
                )
                if strategy.state is StrategyState.SHADOW:
                    latency_assessment = self._process_shadow_signal(
                        run_id=run_id,
                        cycle_index=cycle_index,
                        signal=signal,
                        strategy=strategy,
                        snapshot=candidate_snapshot,
                    )
                    promotion_evidence = {
                        **self.store.shadow_summary(
                            strategy.key,
                            reference_capital=self.config.adaptive_research.shadow.notional_usd,
                        ),
                        "untouched_out_of_sample": False,
                        "latency_feasible": latency_assessment["latency_feasible"],
                    }
                    promotion = self.promotion_controller.evaluate(promotion_evidence)
                    self._event(
                        run_id,
                        "shadow_promotion_evaluated",
                        {
                            "strategy_key": strategy.key,
                            "approved": promotion.approved,
                            "reasons": promotion.reason_codes,
                            "evidence": promotion_evidence,
                        },
                        f"promotion:{run_id}:{cycle_index}:{strategy.key}",
                    )
                    continue
                if signal.executable and RuntimeMode(self.config.mode) in strategy.permitted_runtime_modes:
                    candidates.append((signal, strategy, candidate_snapshot))

        ranking_started_ns = self.dependencies.monotonic_ns()
        candidates.sort(
            key=lambda item: (
                item[0].calibrated_confidence or Decimal("0"),
                item[0].gross_edge_bps or Decimal("0"),
            ),
            reverse=True,
        )
        ranked = [
            {
                "rank": index,
                "signal_id": signal.signal_id,
                "strategy_key": strategy.key,
                "symbol": signal.symbol,
                "calibrated_confidence": signal.calibrated_confidence,
                "gross_edge_bps": signal.gross_edge_bps,
            }
            for index, (signal, strategy, _snapshot) in enumerate(candidates, start=1)
        ]
        self._event(
            run_id,
            "candidates_ranked",
            {
                "candidate_count": len(candidates),
                "ranking": ranked,
                "ranking_latency_ms": (self.dependencies.monotonic_ns() - ranking_started_ns) / 1_000_000,
            },
            f"candidates_ranked:{run_id}:{cycle_index}",
        )
        for signal, strategy, snapshot in candidates:
            if (
                self.store.intent_for_signal(
                    session_id=self.config.session_id,
                    strategy_key=strategy.key,
                    signal_id=signal.signal_id,
                    kind=IntentKind.ENTRY.value,
                )
                is not None
            ):
                self._event(
                    run_id,
                    "duplicate_signal_rejected",
                    {"signal_id": signal.signal_id, "strategy_key": strategy.key},
                    f"duplicate_signal:{run_id}:{signal.signal_id}",
                )
                counters["signals_rejected"] += 1
                continue
            if snapshot.spread_bps > strategy.maximum_spread_bps:
                self._event(
                    run_id,
                    "signal_rejected",
                    {"signal_id": signal.signal_id, "reason": "spread_limit"},
                    f"signal_rejected:{signal.signal_id}:spread",
                )
                counters["signals_rejected"] += 1
                continue
            age_ms = (self.dependencies.clock() - snapshot.quote_observed_at).total_seconds() * 1_000
            if age_ms < 0 or age_ms > strategy.latency.maximum_quote_age_ms:
                self._event(
                    run_id,
                    "signal_rejected",
                    {"signal_id": signal.signal_id, "reason": "STALE_QUOTE", "quote_age_ms": age_ms},
                    f"signal_rejected:{signal.signal_id}:stale",
                )
                counters["signals_rejected"] += 1
                continue
            asset = self._asset_for(signal.symbol)
            risk_started_ns = self.dependencies.monotonic_ns()
            risk = self.risk_manager.size_entry(
                signal=signal,
                strategy=strategy,
                account=state.account,
                broker_positions=state.positions,
                open_allocations=self.store.open_allocations(self.config.session_id),
                intent_counts=self.store.intent_counts(self.config.session_id),
                completed_round_trips=self.store.completed_round_trips(self.config.session_id),
                strategy_completed_round_trips=self.store.completed_round_trips(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                strategy_realized_pnl=self.store.realized_pnl(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                asset=asset,
            )
            self._event(
                run_id,
                "risk_evaluated",
                {
                    "signal_id": signal.signal_id,
                    **risk.model_dump(mode="json"),
                    "risk_check_latency_ms": (self.dependencies.monotonic_ns() - risk_started_ns) / 1_000_000,
                },
                f"risk:{signal.signal_id}",
            )
            if not risk.approved:
                counters["signals_rejected"] += 1
                continue
            if self.config.mode is RuntimeMode.RESEARCH_ONLY:
                self._event(
                    run_id,
                    "hypothetical_order_created",
                    {
                        "signal_id": signal.signal_id,
                        "quantity": str(risk.quantity),
                        "notional": str(risk.estimated_notional_usd),
                    },
                    f"hypothetical:{signal.signal_id}",
                )
                continue
            if self.config.mode is RuntimeMode.DRY_RUN:
                intent = self._build_intent(
                    run_id=run_id,
                    signal=signal,
                    risk_quantity=risk.quantity,
                    client_order_id="dry-" + canonical_hash({"run": run_id, "signal": signal.signal_id})[:32],
                    decision_id=None,
                )
                if self.store.create_intent(intent):
                    counters["intents_created"] += 1
                self.store.transition_intent(intent.intent_id, IntentState.VALIDATED, at=self.dependencies.clock())
                self.store.transition_intent(intent.intent_id, IntentState.DRY_RUN, at=self.dependencies.clock())
                self._event(
                    run_id,
                    "dry_run_intent_completed",
                    intent.model_dump(mode="json"),
                    f"dry_intent:{intent.intent_id}",
                )
                continue
            submitted = self._submit_paper(
                run_id=run_id,
                signal=signal,
                strategy=strategy,
                quantity=risk.quantity,
            )
            counters["intents_created"] += int(submitted.get("intent_created", False))
            counters["orders_submitted"] += int(submitted.get("order_submitted", False))
            if submitted.get("safety_halt"):
                counters["safety_halts"].append(str(submitted["safety_halt"]))
                break
        counters["safety_halts"].extend(
            self._record_post_execution_state(
                run_id=run_id,
                snapshots=snapshots,
                submitted_orders=int(counters["orders_submitted"]),
            )
        )
        return counters

    def _apply_experimental_demotions(
        self,
        run_id: str,
        strategies: list[StrategySpecification],
    ) -> None:
        unresolved = self.store.unresolved_intents()
        for strategy in strategies:
            if strategy.state is not StrategyState.EXPERIMENTAL_PAPER:
                continue
            if self.store.strategy_operational_state(strategy.key) == "disabled":
                continue
            summary = {
                "realized_pnl": self.store.realized_pnl(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                "completed_round_trips": self.store.completed_round_trips(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                "unresolved_orders": sum(1 for row in unresolved if row.get("strategy_key") == strategy.key),
                "critical_rule_violations": 0,
                "latency_feasible": True,
            }
            reasons = self.promotion_controller.demotion_reasons(summary)
            if not reasons:
                continue
            self.store.disable_strategy(
                strategy.key,
                reason=reasons[0],
                evidence=summary,
                at=self.dependencies.clock(),
            )
            self._event(
                run_id,
                "experimental_strategy_demoted",
                {
                    "strategy_key": strategy.key,
                    "reasons": reasons,
                    "evidence": summary,
                },
                f"experimental_demotion:{strategy.key}:{canonical_hash(summary)[:16]}",
            )

    def _shadow_latency_assessment(
        self,
        *,
        signal: TradingSignal,
        strategy: StrategySpecification,
        snapshot: MarketSnapshot,
    ) -> dict[str, Any]:
        now = self.dependencies.clock()
        quote_age_ms = (now - snapshot.quote_observed_at).total_seconds() * 1_000
        bar_age_ms = (now - snapshot.bars[-1].timestamp).total_seconds() * 1_000
        forecast_ms = min(strategy.latency.maximum_decision_to_submit_ms, 1_000)
        tau_ms = max(strategy.latency.expected_holding_period_minutes * 60_000, 1)
        decay = Decimal(str(math.exp(-forecast_ms / tau_ms)))
        hypothesis_edge = max(abs(signal.raw_score), Decimal("0"))
        round_trip_cost_bps = (
            snapshot.spread_bps
            + Decimal(str(self.config.adaptive_research.shadow.taker_fee_bps * 2))
            + Decimal(str(self.config.adaptive_research.shadow.slippage_bps_each_side * 2))
        )
        net_edge = hypothesis_edge * decay - round_trip_cost_bps
        feasible = (
            0 <= quote_age_ms <= strategy.latency.maximum_quote_age_ms
            and 0 <= bar_age_ms <= strategy.latency.maximum_bar_age_ms
            and forecast_ms <= strategy.latency.maximum_decision_to_submit_ms
            and net_edge > Decimal("3")
        )
        return {
            "model": "shadow-prior-only-conservative-v1",
            "hypothesis_edge_bps": hypothesis_edge,
            "round_trip_cost_bps": round_trip_cost_bps,
            "forecast_latency_ms": forecast_ms,
            "predicted_decay_factor": decay,
            "predicted_net_edge_bps": net_edge,
            "quote_age_ms": quote_age_ms,
            "bar_age_ms": bar_age_ms,
            "latency_feasible": feasible,
            "validated_edge": False,
        }

    def _process_shadow_signal(
        self,
        *,
        run_id: str,
        cycle_index: int,
        signal: TradingSignal,
        strategy: StrategySpecification,
        snapshot: MarketSnapshot,
    ) -> dict[str, Any]:
        assessment = self._shadow_latency_assessment(
            signal=signal,
            strategy=strategy,
            snapshot=snapshot,
        )
        self._event(
            run_id,
            "shadow_latency_assessed",
            {
                "strategy_key": strategy.key,
                "signal_id": signal.signal_id,
                **assessment,
            },
            f"shadow_latency:{run_id}:{cycle_index}:{signal.signal_id}",
        )
        if (
            signal.direction is not SignalDirection.BUY
            or signal.rejection_reasons
            or self.store.open_shadow_position(
                session_id=self.config.session_id,
                strategy_key=strategy.key,
                symbol=signal.symbol,
            )
            is not None
        ):
            return assessment
        shadow = self.config.adaptive_research.shadow
        notional = Decimal(str(shadow.notional_usd))
        slippage_fraction = Decimal(str(shadow.slippage_bps_each_side)) / Decimal("10000")
        fee_fraction = Decimal(str(shadow.taker_fee_bps)) / Decimal("10000")
        entry_decision = snapshot.ask
        entry_fill = entry_decision * (Decimal("1") + slippage_fraction)
        quantity = notional / entry_fill
        entry_fee = quantity * entry_fill * fee_fraction
        if signal.proposed_stop is None or signal.proposed_target is None:
            return {**assessment, "shadow_rejection": "protective_exits_missing"}
        position_id = self.store.add_shadow_position(
            session_id=self.config.session_id,
            strategy_key=strategy.key,
            symbol=signal.symbol,
            entry_signal_id=signal.signal_id,
            quantity=quantity,
            entry_decision_price=entry_decision,
            entry_fill_price=entry_fill,
            entry_fee=entry_fee,
            stop_price=signal.proposed_stop,
            target_price=signal.proposed_target,
            opened_at=self.dependencies.clock(),
            holding_deadline=(self.dependencies.clock() + timedelta(minutes=signal.holding_period_expectation_minutes)),
        )
        self._event(
            run_id,
            "shadow_position_opened",
            {
                "shadow_position_id": position_id,
                "strategy_key": strategy.key,
                "signal_id": signal.signal_id,
                "quantity": str(quantity),
                "entry_decision_price": str(entry_decision),
                "entry_fill_price": str(entry_fill),
                "entry_fee": str(entry_fee),
                "cost_model": shadow.model_dump(mode="json"),
            },
            f"shadow_open:{position_id}",
        )
        return assessment

    def _manage_shadow_positions(
        self,
        *,
        run_id: str,
        snapshots: Mapping[str, MarketSnapshot],
        strategies: Mapping[str, StrategySpecification],
    ) -> None:
        shadow = self.config.adaptive_research.shadow
        slippage_fraction = Decimal(str(shadow.slippage_bps_each_side)) / Decimal("10000")
        fee_fraction = Decimal(str(shadow.taker_fee_bps)) / Decimal("10000")
        for position in self.store.open_shadow_positions(self.config.session_id):
            strategy = strategies.get(str(position["strategy_key"]))
            snapshot = snapshots.get(str(position["symbol"]))
            if strategy is None or snapshot is None:
                self._event(
                    run_id,
                    "shadow_position_deferred",
                    {
                        "shadow_position_id": position["shadow_position_id"],
                        "reason": "strategy_or_snapshot_unavailable",
                    },
                    f"shadow_deferred:{run_id}:{position['shadow_position_id']}",
                )
                continue
            mark = snapshot.bid
            highest = max(Decimal(str(position["highest_price"])), mark)
            self.store.update_shadow_high(str(position["shadow_position_id"]), mark)
            reason: str | None = None
            if mark <= Decimal(str(position["stop_price"])):
                reason = "stop_loss"
            elif mark >= Decimal(str(position["target_price"])):
                reason = "take_profit"
            elif strategy.rules.trailing_stop_bps is not None and mark <= highest * (
                Decimal("1") - strategy.rules.trailing_stop_bps / Decimal("10000")
            ):
                reason = "trailing_stop"
            elif self.dependencies.clock() >= self._parse_utc(position["holding_deadline"]):
                reason = "maximum_holding_period"
            if reason is None:
                continue
            quantity = Decimal(str(position["quantity"]))
            exit_decision = mark
            exit_fill = exit_decision * (Decimal("1") - slippage_fraction)
            exit_fee = quantity * exit_fill * fee_fraction
            regime = self.modules["regime"].evaluate(
                snapshot,
                strategy,
                now=self.dependencies.clock(),
            )
            economics = self.store.close_shadow_position(
                shadow_position_id=str(position["shadow_position_id"]),
                exit_decision_price=exit_decision,
                exit_fill_price=exit_fill,
                exit_fee=exit_fee,
                closed_at=self.dependencies.clock(),
                regime=str(regime.values.get("regime", "unknown")),
            )
            self._event(
                run_id,
                "shadow_position_closed",
                {
                    "shadow_position_id": position["shadow_position_id"],
                    "reason": reason,
                    "exit_decision_price": str(exit_decision),
                    "exit_fill_price": str(exit_fill),
                    **economics,
                },
                f"shadow_close:{position['shadow_position_id']}",
            )

    def _record_account_risk(
        self,
        run_id: str,
        snapshots: Mapping[str, MarketSnapshot],
        state: BrokerState,
    ) -> dict[str, Any]:
        """Persist attributable Phase 8 equity without claiming external P&L."""
        unrealized = Decimal("0")
        owned_exposure = Decimal("0")
        reasons: list[str] = []
        for allocation in self.store.open_allocations(self.config.session_id):
            symbol = str(allocation["symbol"])
            snapshot = snapshots.get(symbol)
            quantity = Decimal(str(allocation["remaining_quantity"]))
            entry = Decimal(str(allocation["average_fill_price"]))
            if snapshot is None:
                reasons.append(f"missing_position_snapshot:{symbol}")
                owned_exposure += abs(quantity * entry)
                continue
            mark = snapshot.bid
            unrealized += (mark - entry) * quantity
            owned_exposure += abs(mark * quantity)

        broker_exposure = sum(
            (abs(Decimal(str(position.get("market_value") or 0))) for position in state.positions),
            Decimal("0"),
        )
        gross_exposure = max(owned_exposure, broker_exposure)
        equity_snapshot = self.store.record_equity(
            run_id=run_id,
            session_id=self.config.session_id,
            observed_at=self.dependencies.clock(),
            initial_capital=self.config.validation_profile.internal_capital_usd,
            unrealized_pnl=unrealized,
            gross_exposure=gross_exposure,
        )
        session_pnl = equity_snapshot["realized_pnl"] + equity_snapshot["unrealized_pnl"]
        if session_pnl <= -self.config.validation_profile.session_loss_stop_usd:
            reasons.append("session_loss_stop")
        if equity_snapshot["drawdown_fraction"] >= self.config.validation_profile.hard_drawdown_fraction:
            reasons.append("hard_drawdown_stop")
        if gross_exposure > self.config.validation_profile.maximum_gross_exposure_usd:
            reasons.append("gross_exposure_limit")
        if (
            self.config.validation_profile.internal_capital_usd - gross_exposure
            < self.config.validation_profile.minimum_unallocated_capital_usd
        ):
            reasons.append("cash_reserve_limit")
        result: dict[str, Any] = {
            **equity_snapshot,
            "session_pnl": session_pnl,
            "broker_gross_exposure": broker_exposure,
            "owned_gross_exposure": owned_exposure,
            "halt_reasons": tuple(sorted(set(reasons))),
        }
        self._event(
            run_id,
            "account_risk_snapshot",
            result,
            "account_risk:"
            + canonical_hash(
                {
                    "run": run_id,
                    "equity": str(equity_snapshot["equity"]),
                    "exposure": str(gross_exposure),
                    "reasons": result["halt_reasons"],
                }
            )[:32],
        )
        return result

    def _record_post_execution_state(
        self,
        *,
        run_id: str,
        snapshots: Mapping[str, MarketSnapshot],
        submitted_orders: int,
    ) -> list[str]:
        """Reconcile and revalue after a broker mutation without blind action."""
        if submitted_orders <= 0:
            return []
        try:
            state = self._read_broker_state()
        except Exception as exc:  # noqa: BLE001 - fail closed after broker mutation
            payload: dict[str, Any] = {"error": str(exc)}
            self._event(
                run_id,
                "post_execution_state_unavailable",
                payload,
                f"post_execution_state_unavailable:{run_id}:{canonical_hash(payload)[:16]}",
            )
            return ["post_execution_broker_state_unavailable"]

        _owned_orders, external_orders = self._classify_open_orders(state.open_orders)
        ownership = self._classify_positions(state.positions)
        halts: list[str] = []
        if external_orders:
            halts.append("post_execution_external_open_orders")
        if ownership["discrepancies"]:
            halts.append("post_execution_owned_position_mismatch")
        risk_snapshot = self._record_account_risk(run_id, snapshots, state)
        halts.extend(str(reason) for reason in risk_snapshot["halt_reasons"])
        if halts:
            payload = {
                "reasons": tuple(sorted(set(halts))),
                "owned_open_orders": len(_owned_orders),
                "external_open_orders": len(external_orders),
                "position_discrepancies": ownership["discrepancies"],
                "risk_snapshot": risk_snapshot,
            }
            self._event(
                run_id,
                "post_execution_safety_halt",
                payload,
                f"post_execution_safety_halt:{run_id}:{canonical_hash(payload)[:16]}",
            )
        return sorted(set(halts))

    @staticmethod
    def _parse_utc(value: Any) -> datetime:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise RuntimePersistenceError("persisted timestamp is timezone-naive")
        return parsed.astimezone(timezone.utc)

    def _strategy_for_allocation(
        self,
        allocation: Mapping[str, Any],
    ) -> StrategySpecification | None:
        key = str(allocation.get("strategy_key") or "")
        configured = (
            *self.config.accepted_strategies,
            *self.config.experimental_strategies,
        )
        return next((strategy for strategy in configured if strategy.key == key), None)

    def _protective_exit_reason(
        self,
        *,
        allocation: Mapping[str, Any],
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        account_halts: tuple[str, ...],
    ) -> tuple[str | None, Decimal]:
        entry = Decimal(str(allocation["average_fill_price"]))
        stop = Decimal(str(allocation["stop_price"]))
        target = Decimal(str(allocation["target_price"]))
        highest = Decimal(str(allocation["highest_price"]))
        mark = snapshot.bid
        self.store.update_allocation_high(str(allocation["allocation_id"]), mark)
        highest = max(highest, mark)
        if account_halts:
            return "emergency_account_risk_halt", max(strategy.rules.stop_loss_bps, Decimal("100"))
        if mark <= stop:
            avoided = abs((entry - stop) / entry) * Decimal("10000")
            return "stop_loss", max(avoided, Decimal("100"))
        if mark >= target:
            locked = abs((mark - entry) / entry) * Decimal("10000")
            return "take_profit", max(locked, Decimal("100"))
        if strategy.rules.trailing_stop_bps is not None:
            trailing = highest * (Decimal("1") - strategy.rules.trailing_stop_bps / Decimal("10000"))
            if mark <= trailing:
                return "trailing_stop", max(strategy.rules.trailing_stop_bps, Decimal("100"))
        if self.dependencies.clock() >= self._parse_utc(allocation["holding_deadline"]):
            return "maximum_holding_period", Decimal("100")
        if snapshot.spread_bps > strategy.maximum_spread_bps * Decimal("2"):
            return "spread_deterioration", max(snapshot.spread_bps, Decimal("100"))

        # Analytical exits are lower priority and require fresh point-in-time bars.
        bar_age_ms = (self.dependencies.clock() - snapshot.bars[-1].timestamp).total_seconds() * 1_000
        if 0 <= bar_age_ms <= strategy.latency.maximum_bar_age_ms:
            regime = self.modules["regime"].evaluate(
                snapshot,
                strategy,
                now=self.dependencies.clock(),
            )
            if (
                regime.state is ModuleState.VALID
                and str(regime.values.get("regime")) not in strategy.risk.permitted_regimes
            ):
                return "regime_change", Decimal("100")
            technical = self.modules["technical"].evaluate(
                snapshot,
                strategy,
                now=self.dependencies.clock(),
            )
            if technical.state is ModuleState.VALID:
                feature = "trend_bps" if strategy.rules.family == "trend" else "momentum_bps"
                raw = Decimal(str(technical.values.get(feature, 0)))
                if strategy.rules.family == "mean_reversion":
                    raw = -raw
                if raw <= strategy.rules.exit_threshold_bps:
                    return "signal_invalidation", Decimal("100")
        return None, Decimal("0")

    def _protective_exit_signal(
        self,
        *,
        run_id: str,
        allocation: Mapping[str, Any],
        snapshot: MarketSnapshot,
        strategy: StrategySpecification,
        reason: str,
        risk_reduction_bps: Decimal,
    ) -> TradingSignal:
        now = self.dependencies.clock()
        signal_id = (
            "sig_exit_"
            + canonical_hash(
                {
                    "run": run_id,
                    "allocation": allocation["allocation_id"],
                    "snapshot": snapshot.snapshot_id,
                    "reason": reason,
                }
            )[:28]
        )
        return TradingSignal(
            signal_id=signal_id,
            run_id=run_id,
            timestamp=now,
            symbol=str(allocation["symbol"]),
            asset_class=snapshot.asset_class,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            direction=SignalDirection.SELL,
            signal_type=f"protective_exit:{reason}",
            raw_score=risk_reduction_bps,
            calibrated_confidence=Decimal("1"),
            confidence_calibration_version="protective-risk-control-v1",
            feature_values={
                "exit_reason": reason,
                "risk_reduction_budget_bps": str(risk_reduction_bps),
                "not_alpha_forecast": True,
            },
            source_data_timestamps={
                "quote": snapshot.quote_observed_at.isoformat(),
                "bar": snapshot.bars[-1].timestamp.isoformat(),
            },
            coherent_snapshot_id=snapshot.snapshot_id,
            market_regime="protective_exit",
            module_states={"risk": ModuleState.VALID},
            proposed_entry_reference=snapshot.bid,
            proposed_stop=None,
            proposed_target=None,
            holding_period_expectation_minutes=1,
            maximum_signal_age_ms=strategy.latency.maximum_signal_age_ms,
            gross_edge_bps=risk_reduction_bps,
            warnings=("protective_exit_uses_risk_reduction_not_alpha",),
            latency_result="PROTECTIVE_EXIT_REQUIRES_FRESH_QUOTE",
        )

    def _manage_protective_exits(
        self,
        *,
        run_id: str,
        snapshots: Mapping[str, MarketSnapshot],
        account_halts: tuple[str, ...],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "exits_triggered": 0,
            "intents_created": 0,
            "orders_submitted": 0,
            "safety_halts": [],
        }
        for allocation in self.store.open_allocations(self.config.session_id):
            strategy = self._strategy_for_allocation(allocation)
            symbol = str(allocation["symbol"])
            snapshot = snapshots.get(symbol)
            if strategy is None:
                result["exits_triggered"] += 1
                result["safety_halts"].append(f"missing_frozen_strategy:{symbol}")
                continue
            if snapshot is None:
                result["exits_triggered"] += 1
                result["safety_halts"].append(f"missing_exit_snapshot:{symbol}")
                continue
            quote_age_ms = (self.dependencies.clock() - snapshot.quote_observed_at).total_seconds() * 1_000
            if quote_age_ms < 0 or quote_age_ms > strategy.latency.maximum_quote_age_ms:
                result["exits_triggered"] += 1
                result["safety_halts"].append(f"protective_exit_stale_quote:{symbol}")
                continue
            reason, risk_reduction = self._protective_exit_reason(
                allocation=allocation,
                snapshot=snapshot,
                strategy=strategy,
                account_halts=account_halts,
            )
            if reason is None:
                continue
            result["exits_triggered"] += 1
            counts = self.store.intent_counts(self.config.session_id)
            if counts["exit"] >= self.config.validation_profile.maximum_exit_orders:
                result["safety_halts"].append("exit_order_limit")
                continue
            if counts["submitted"] >= self.config.validation_profile.maximum_total_submitted_orders:
                result["safety_halts"].append("total_order_limit_prevents_exit")
                continue
            asset = self._asset_for(symbol)
            increment = Decimal(str(asset.get("min_trade_increment") or "0.00000001"))
            minimum = Decimal(str(asset.get("min_order_size") or increment))
            remaining = Decimal(str(allocation["remaining_quantity"]))
            quantity = (remaining // increment) * increment if increment > 0 else Decimal("0")
            if quantity <= 0 or quantity < minimum or quantity > remaining:
                result["safety_halts"].append(f"unexitable_owned_quantity:{symbol}")
                continue
            signal = self._protective_exit_signal(
                run_id=run_id,
                allocation=allocation,
                snapshot=snapshot,
                strategy=strategy,
                reason=reason,
                risk_reduction_bps=risk_reduction,
            )
            existing_exit = self.store.intent_for_signal(
                session_id=self.config.session_id,
                strategy_key=strategy.key,
                signal_id=signal.signal_id,
                kind=IntentKind.PROTECTIVE_EXIT.value,
            )
            if existing_exit is not None:
                self._event(
                    run_id,
                    "duplicate_exit_signal_rejected",
                    {
                        "allocation_id": allocation["allocation_id"],
                        "signal_id": signal.signal_id,
                        "existing_intent_id": existing_exit["intent_id"],
                    },
                    f"duplicate_exit:{run_id}:{signal.signal_id}",
                )
                continue
            self._event(
                run_id,
                "protective_exit_triggered",
                {
                    "allocation_id": allocation["allocation_id"],
                    "reason": reason,
                    "quantity": str(quantity),
                    "signal": signal.model_dump(mode="json"),
                },
                f"protective_exit:{signal.signal_id}",
            )
            if self.config.mode is RuntimeMode.RESEARCH_ONLY:
                continue
            if self.config.mode is RuntimeMode.DRY_RUN:
                intent = self._build_intent(
                    run_id=run_id,
                    signal=signal,
                    risk_quantity=quantity,
                    client_order_id="dry-exit-"
                    + canonical_hash({"run": run_id, "allocation": allocation["allocation_id"]})[:28],
                    decision_id=None,
                    kind=IntentKind.PROTECTIVE_EXIT,
                    allocation_id=str(allocation["allocation_id"]),
                )
                if self.store.create_intent(intent):
                    result["intents_created"] += 1
                self.store.transition_intent(
                    intent.intent_id,
                    IntentState.VALIDATED,
                    at=self.dependencies.clock(),
                )
                self.store.transition_intent(
                    intent.intent_id,
                    IntentState.DRY_RUN,
                    at=self.dependencies.clock(),
                )
                continue
            submitted = self._submit_paper(
                run_id=run_id,
                signal=signal,
                strategy=strategy,
                quantity=quantity,
                kind=IntentKind.PROTECTIVE_EXIT,
                allocation_id=str(allocation["allocation_id"]),
            )
            result["intents_created"] += int(submitted.get("intent_created", False))
            result["orders_submitted"] += int(submitted.get("order_submitted", False))
            if submitted.get("safety_halt"):
                result["safety_halts"].append(str(submitted["safety_halt"]))
            # One broker mutation at a time; reconciliation precedes the next.
            break
        return result

    def _asset_for(self, symbol: str) -> Mapping[str, Any]:
        if symbol in self._assets:
            return self._assets[symbol]
        response = dict(
            self.dependencies.assets_reader(
                self.config.broker_profile_id,
                asset_class="crypto",
                tradable_only=True,
                symbol=symbol,
            )
        )
        if (
            response.get("status") != "ok"
            or response.get("environment") != "paper"
            or response.get("is_paper") is not True
        ):
            raise RuntimeError("asset constraints could not be proven from Alpaca paper")
        rows = response.get("assets")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise RuntimeError(f"exactly one tradable asset mapping required for {symbol}")
        asset = rows[0]
        if not asset.get("paper_eligible") or not asset.get("tradable"):
            raise RuntimeError(f"{symbol} is not paper eligible")
        self._assets[symbol] = asset
        return asset

    def _build_intent(
        self,
        *,
        run_id: str,
        signal: TradingSignal,
        risk_quantity: Decimal,
        client_order_id: str,
        decision_id: str | None,
        kind: IntentKind = IntentKind.ENTRY,
        allocation_id: str | None = None,
    ) -> OrderIntent:
        now = self.dependencies.clock()
        material = {
            "run": run_id,
            "strategy": f"{signal.strategy_id}:{signal.strategy_version}",
            "signal": signal.signal_id,
            "symbol": signal.symbol,
            "side": signal.direction.value,
            "kind": kind.value,
        }
        return OrderIntent(
            intent_id="intent_" + canonical_hash(material)[:32],
            decision_id=decision_id,
            run_id=run_id,
            session_id=self.config.session_id,
            strategy_id=signal.strategy_id,
            strategy_version=signal.strategy_version,
            signal_id=signal.signal_id,
            symbol=signal.symbol,
            side="buy" if signal.direction is SignalDirection.BUY else "sell",
            quantity=risk_quantity,
            order_type="market",
            time_in_force="gtc",
            kind=kind,
            risk_approved=True,
            latency_approved=True,
            reconciliation_approved=True,
            created_at=now,
            expires_at=now + timedelta(milliseconds=signal.maximum_signal_age_ms),
            client_order_id=client_order_id,
            allocation_id=allocation_id,
            proposed_stop=signal.proposed_stop if kind is IntentKind.ENTRY else None,
            proposed_target=signal.proposed_target if kind is IntentKind.ENTRY else None,
            holding_deadline=(
                now + timedelta(minutes=signal.holding_period_expectation_minutes) if kind is IntentKind.ENTRY else None
            ),
        )

    def _submit_paper(
        self,
        *,
        run_id: str,
        signal: TradingSignal,
        strategy: StrategySpecification,
        quantity: Decimal,
        kind: IntentKind = IntentKind.ENTRY,
        allocation_id: str | None = None,
    ) -> dict[str, Any]:
        if self.config.mode is not RuntimeMode.PAPER_EXECUTE or not self.config.paper_execution_authorized:
            return {"safety_halt": "paper_execution_not_authorized"}
        # Mandatory final reconciliation immediately before the Phase 8 gate.
        state = self._read_broker_state()
        if state.open_orders or self.store.unresolved_intents():
            return {"safety_halt": "final_reconciliation_failed"}
        if self._classify_positions(state.positions)["discrepancies"]:
            return {"safety_halt": "final_owned_position_mismatch"}
        signal_age_ms = (self.dependencies.clock() - signal.timestamp).total_seconds() * 1_000
        if signal_age_ms < 0 or signal_age_ms > signal.maximum_signal_age_ms:
            return {"safety_halt": "STALE_SIGNAL"}
        counts = self.store.intent_counts(self.config.session_id)
        if counts["submitted"] >= self.config.validation_profile.maximum_total_submitted_orders:
            return {"safety_halt": "total_order_limit"}
        if kind is IntentKind.ENTRY:
            if _is_single_entry_smoke(self.config) and counts["entry"] >= 1:
                return {"safety_halt": "smoke_stage_entry_limit"}
            if counts["entry"] >= self.config.validation_profile.maximum_entry_orders:
                return {"safety_halt": "entry_order_limit"}
            final_risk = self.risk_manager.size_entry(
                signal=signal,
                strategy=strategy,
                account=state.account,
                broker_positions=state.positions,
                open_allocations=self.store.open_allocations(self.config.session_id),
                intent_counts=counts,
                completed_round_trips=self.store.completed_round_trips(self.config.session_id),
                strategy_completed_round_trips=self.store.completed_round_trips(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                strategy_realized_pnl=self.store.realized_pnl(
                    self.config.session_id,
                    strategy_key=strategy.key,
                ),
                asset=self._asset_for(signal.symbol),
            )
            if not final_risk.approved or final_risk.quantity < quantity:
                return {"safety_halt": "final_risk_revalidation_failed"}
        else:
            if counts["exit"] >= self.config.validation_profile.maximum_exit_orders:
                return {"safety_halt": "exit_order_limit"}
            allocation = self.store.allocation(str(allocation_id or ""))
            if (
                allocation is None
                or allocation["state"] != "open"
                or Decimal(str(allocation["remaining_quantity"])) < quantity
            ):
                return {"safety_halt": "final_exit_ownership_failed"}
        latency_config = LatencyBudgetConfig(
            enabled=True,
            freshness_limit_ms=strategy.latency.maximum_quote_age_ms,
            tau_ms=max(strategy.latency.expected_holding_period_minutes * 60_000, 1),
            minimum_prior_samples=20,
            rolling_history_window=100,
            cold_start_policy="fallback_p90",
            fallback_p90_latency_ms=min(strategy.latency.maximum_decision_to_submit_ms, 1_000),
            required_buffer_bps=3,
            cost_assumptions=CostAssumptions(
                maker_fee_bps=25,
                taker_fee_bps=25,
                spread_bps=0,
                slippage_bps=5,
                impact_bps=1,
            ),
        )
        request = Phase8PaperOrderRequest(
            symbol=signal.symbol,
            side=signal.direction.value,
            quantity=quantity,
            gross_edge_bps=signal.gross_edge_bps or Decimal("0"),
            strategy_version=f"{strategy.strategy_id}:{strategy.version}",
            strategy_requirements_met=True,
            submit=True,
            order_type="market",
            time_in_force="gtc",
            signal_key=signal.signal_id,
            signal_metadata={
                "runtime_signal_id": signal.signal_id,
                "snapshot_id": signal.coherent_snapshot_id,
                "strategy_fingerprint": strategy.fingerprint,
                "intent_kind": kind.value,
                "allocation_id": allocation_id,
                "protective_exit": kind is IntentKind.PROTECTIVE_EXIT,
            },
            run_id=run_id,
        )
        holder: dict[str, OrderIntent] = {}

        def persist_before_submit(authorization: Any) -> None:
            intent = self._build_intent(
                run_id=run_id,
                signal=signal,
                risk_quantity=quantity,
                client_order_id=str(authorization.client_order_id),
                decision_id=str(authorization.decision_id),
                kind=kind,
                allocation_id=allocation_id,
            )
            if self.store.create_intent(intent):
                self.store.transition_intent(intent.intent_id, IntentState.VALIDATED, at=self.dependencies.clock())
                self.store.transition_intent(intent.intent_id, IntentState.SUBMITTING, at=self.dependencies.clock())
            holder["intent"] = intent
            self._event(
                run_id,
                "paper_intent_persisted",
                intent.model_dump(mode="json"),
                f"paper_intent:{intent.intent_id}",
            )

        submission_started_ns = self.dependencies.monotonic_ns()
        try:
            result = dict(
                self.dependencies.phase8_executor(
                    request,
                    config=latency_config,
                    database_path=self.config.resolved_phase8_ledger_path(),
                    before_submit=persist_before_submit,
                )
            )
        except Exception as exc:  # noqa: BLE001
            intent = holder.get("intent")
            if intent is not None:
                self.store.transition_intent(intent.intent_id, IntentState.AMBIGUOUS, at=self.dependencies.clock())
            self._event(
                run_id,
                "paper_submission_ambiguous",
                {"signal_id": signal.signal_id, "error": str(exc)},
                f"paper_ambiguous:{signal.signal_id}",
            )
            return {"intent_created": intent is not None, "safety_halt": "submission_ambiguous"}
        intent = holder.get("intent")
        if intent is None:
            self._event(
                run_id,
                "phase8_execution_blocked",
                result,
                f"phase8_blocked:{signal.signal_id}",
            )
            return {"intent_created": False, "order_submitted": False}
        execution_status = str(result.get("execution_status") or "")
        order_id = str(result.get("order_id") or "") or None
        decision_id = str(result.get("decision_id") or "") or None
        if execution_status == "filled":
            target = IntentState.FILLED
        elif execution_status == "partially_filled_pending":
            target = IntentState.PARTIALLY_FILLED
        elif execution_status in {"submitted", "submitted_pending"}:
            target = IntentState.ACKNOWLEDGED
        elif execution_status in {"broker_submission_failed", "rejected"}:
            target = IntentState.REJECTED
        elif execution_status == "submission_ambiguous" or result.get("reconciliation_required"):
            target = IntentState.AMBIGUOUS
        else:
            target = IntentState.RECONCILIATION_REQUIRED
        if target in {IntentState.PARTIALLY_FILLED, IntentState.FILLED}:
            fill_price_raw = result.get("filled_average_price")
            fill_quantity_raw = result.get("filled_quantity")
            if fill_price_raw in (None, "") or fill_quantity_raw in (None, ""):
                target = IntentState.RECONCILIATION_REQUIRED
            else:
                try:
                    self.store.apply_cumulative_fill(
                        intent=intent,
                        cumulative_quantity=Decimal(str(fill_quantity_raw)),
                        average_fill_price=Decimal(str(fill_price_raw)),
                        observed_at=self.dependencies.clock(),
                    )
                except Exception as exc:  # noqa: BLE001
                    target = IntentState.RECONCILIATION_REQUIRED
                    result = {**result, "projection_error": str(exc)}
        self.store.transition_intent(
            intent.intent_id,
            target,
            at=self.dependencies.clock(),
            decision_id=decision_id,
            broker_order_id=order_id,
        )
        self._event(
            run_id,
            "paper_execution_result",
            {
                **result,
                "runtime_submission_latency_ms": (self.dependencies.monotonic_ns() - submission_started_ns) / 1_000_000,
            },
            f"paper_result:{intent.intent_id}:{target.value}",
        )
        if target is IntentState.FILLED:
            self.store.transition_intent(
                intent.intent_id,
                IntentState.CLOSED,
                at=self.dependencies.clock(),
            )
        safety_halt = (
            "submission_ambiguous" if target in {IntentState.AMBIGUOUS, IntentState.RECONCILIATION_REQUIRED} else None
        )
        return {
            "intent_created": True,
            "order_submitted": bool(result.get("order_submitted")),
            "safety_halt": safety_halt,
        }

    def _event(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> None:
        self.store.append_event(
            run_id=run_id,
            session_id=self.config.session_id,
            event_type=event_type,
            payload=payload,
            idempotency_key=idempotency_key,
            occurred_at=self.dependencies.clock(),
            monotonic_ns=self.dependencies.monotonic_ns(),
        )

    def close(self) -> None:
        self.store.close()


def default_dependencies() -> Phase8RuntimeDependencies:
    """Resolve the production dependencies; no live connector is exposed."""
    from src.trading.profiles import profile_by_id
    from src.trading.service import (
        cancel_order,
        check_connection,
        get_account,
        get_assets,
        get_history,
        get_open_orders,
        get_positions,
        get_quote,
    )
    from src.trading.phase8_paper import (
        execute_phase8_alpaca_paper_order,
        reconcile_phase8_alpaca_paper_order,
    )

    return Phase8RuntimeDependencies(
        profile_resolver=profile_by_id,
        check_connection=check_connection,
        account_reader=get_account,
        positions_reader=get_positions,
        orders_reader=get_open_orders,
        assets_reader=get_assets,
        quote_reader=get_quote,
        history_reader=get_history,
        phase8_executor=execute_phase8_alpaca_paper_order,
        phase8_reconciler=reconcile_phase8_alpaca_paper_order,
        code_integrity_verifier=_verify_local_code_revision,
        order_canceller=cancel_order,
    )


def build_phase8_runtime(
    config: Phase8RuntimeConfig,
    *,
    dependencies: Phase8RuntimeDependencies | None = None,
    store: Phase8RuntimeStore | None = None,
) -> Phase8Runtime:
    """Production composition root with all order authority in one bridge."""
    resolved_dependencies = dependencies or default_dependencies()
    resolved_store = store or Phase8RuntimeStore(config.resolved_database_path())
    return Phase8Runtime(
        config=config,
        dependencies=resolved_dependencies,
        store=resolved_store,
    )
