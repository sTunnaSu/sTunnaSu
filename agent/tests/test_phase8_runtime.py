from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.phase8_runtime.configuration import (
    AdaptiveResearchConfig,
    Phase8RuntimeConfig,
    load_runtime_config,
)
from src.phase8_runtime.models import (
    Bar,
    ExperimentalRiskProfile,
    ExecutableRules,
    IntentKind,
    IntentState,
    LatencyRequirements,
    ModuleRequirement,
    ReleaseDecision,
    RuntimeMode,
    OrderIntent,
    Phase8ValidationProfile,
    PromotionEvidence,
    StrategyRiskRules,
    StrategySpecification,
    StrategyState,
)
from src.phase8_runtime.persistence import Phase8RuntimeStore, RuntimePersistenceError
from src.phase8_runtime.reporting import build_execution_metrics
from src.phase8_runtime.runtime import (
    Phase8RuntimeDependencies,
    build_phase8_runtime,
)
from src.phase8_runtime.services import PromotionController


NOW = datetime(2026, 7, 22, 20, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        self.value += timedelta(milliseconds=1)
        return self.value


def accepted_strategy() -> StrategySpecification:
    rules = ExecutableRules(
        family="trend",
        fast_window=5,
        slow_window=20,
        entry_threshold_bps=Decimal("1"),
        stop_loss_bps=Decimal("100"),
        take_profit_bps=Decimal("200"),
        maximum_holding_cycles=60,
    )
    return StrategySpecification(
        strategy_id="accepted-btc-trend",
        version="1.0.0",
        created_at=NOW - timedelta(days=30),
        creator_component="independent-review",
        code_revision="abc123",
        configuration_hash="cfg123",
        hypothesis="Persistent BTC trends may continue after costs.",
        edge_rationale="Behavioral underreaction and risk transfer.",
        eligible_asset_classes=("crypto",),
        universe=("BTC/USD",),
        market_regime_assumptions=("bull", "bear", "sideways"),
        required_features=("moving_averages", "returns", "volatility"),
        required_data_sources=("alpaca-paper-quote", "alpaca-paper-bars"),
        rules=rules,
        risk=StrategyRiskRules(
            maximum_position_value_usd=Decimal("150"),
            maximum_loss_per_trade_usd=Decimal("2"),
        ),
        latency=LatencyRequirements(maximum_quote_age_ms=5_000, maximum_bar_age_ms=120_000),
        module_policy={
            "technical": ModuleRequirement.REQUIRED,
            "regime": ModuleRequirement.REQUIRED,
            "ai": ModuleRequirement.NOT_APPLICABLE,
            "sentiment": ModuleRequirement.NOT_APPLICABLE,
            "news": ModuleRequirement.NOT_APPLICABLE,
        },
        liquidity_minimum_notional_usd=Decimal("10"),
        maximum_spread_bps=Decimal("50"),
        expected_gross_edge_bps=Decimal("100"),
        edge_estimator_version="accepted-oos-edge-v1",
        confidence_calibration_version="accepted-calibration-v1",
        invalidation_conditions=("negative OOS expectancy", "latency budget failure"),
        known_weaknesses=("trend reversals", "cost sensitivity"),
        permitted_runtime_modes=(
            RuntimeMode.RESEARCH_ONLY,
            RuntimeMode.DRY_RUN,
            RuntimeMode.PAPER_EXECUTE,
        ),
        state=StrategyState.ACCEPTED_PAPER,
        execution_permissions=("paper_order",),
    )


def fake_dependencies(
    *,
    clock: FakeClock,
    executor=None,
    profile=None,
    broker_positions: list[dict[str, Any]] | None = None,
    broker_orders: list[dict[str, Any]] | None = None,
    reconciler=None,
    market: dict[str, str] | None = None,
    quote_age_ms: int = 100,
    last_bar_age_minutes: int = 1,
) -> tuple[Phase8RuntimeDependencies, dict[str, int]]:
    calls = {
        "connection": 0,
        "account": 0,
        "positions": 0,
        "orders": 0,
        "execute": 0,
        "cancel": 0,
    }
    profile = profile or SimpleNamespace(
        id="alpaca-paper-trade",
        connector="alpaca",
        environment="paper",
        transport="broker_sdk",
        readonly=False,
    )
    market = market or {"bid": "64990", "ask": "65000"}

    def check_connection(_profile_id):
        calls["connection"] += 1
        return {
            "status": "ok",
            "profile_id": "alpaca-paper-trade",
            "connector": "alpaca",
            "environment": "paper",
            "host": "https://paper-api.alpaca.markets",
        }

    def account(_profile_id):
        calls["account"] += 1
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "account": {
                "status": "ACTIVE",
                "cash": "100000",
                "buying_power": "400000",
                "trading_blocked": False,
            },
        }

    def positions(_profile_id):
        calls["positions"] += 1
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "positions": list(broker_positions or []),
        }

    def orders(_profile_id, include_executions=False):
        calls["orders"] += 1
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "open_orders": list(broker_orders or []),
            "executions": [],
        }

    def assets(_profile_id, **_kwargs):
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "assets": [
                {
                    "symbol": "BTC/USD",
                    "tradable": True,
                    "paper_eligible": True,
                    "min_order_size": "0.00001",
                    "min_trade_increment": "0.00001",
                }
            ],
        }

    def quote(_symbol, _profile_id):
        return {
            "status": "ok",
            "profile_id": "alpaca-paper-trade",
            "environment": "paper",
            "symbol": "BTC/USD",
            "quote": {
                "bid": market["bid"],
                "ask": market["ask"],
                "bid_size": "1",
                "ask_size": "1",
                "time": (clock.value - timedelta(milliseconds=quote_age_ms)).isoformat(),
            },
        }

    def history(_symbol, _profile_id, **_kwargs):
        bars = []
        for i in range(25):
            close = Decimal("64000") + Decimal(i * 50)
            bars.append(
                {
                    "time": (clock.value - timedelta(minutes=24 - i + last_bar_age_minutes)).isoformat(),
                    "open": str(close - 10),
                    "high": str(close + 20),
                    "low": str(close - 20),
                    "close": str(close),
                    "volume": "100",
                }
            )
        return {
            "status": "ok",
            "profile_id": "alpaca-paper-trade",
            "environment": "paper",
            "symbol": "BTC/USD",
            "asset_class": "crypto",
            "bars": bars,
        }

    def execute(request, **kwargs):
        calls["execute"] += 1
        if executor is not None:
            return executor(request, **kwargs)
        raise AssertionError("paper executor must not be called")

    def cancel(*_args, **_kwargs):
        calls["cancel"] += 1
        return {
            "status": "ok",
            "environment": "paper",
            "is_paper": True,
            "cancelled": True,
        }

    deps = Phase8RuntimeDependencies(
        profile_resolver=lambda _profile_id: profile,
        check_connection=check_connection,
        account_reader=account,
        positions_reader=positions,
        orders_reader=orders,
        assets_reader=assets,
        quote_reader=quote,
        history_reader=history,
        phase8_executor=execute,
        phase8_reconciler=(reconciler or (lambda *_args, **_kwargs: {})),
        code_integrity_verifier=lambda revision: (
            revision == "test-revision",
            f"test revision {revision}",
        ),
        order_canceller=cancel,
        clock=clock,
        monotonic_ns=lambda: 123456789,
        sleeper=lambda _seconds: None,
    )
    return deps, calls


def runtime_config(tmp_path: Path, *, mode: RuntimeMode, accepted=()) -> Phase8RuntimeConfig:
    release = {
        RuntimeMode.RESEARCH_ONLY: ReleaseDecision.GO_FOR_RESEARCH_ONLY,
        RuntimeMode.DRY_RUN: ReleaseDecision.GO_FOR_DRY_RUN,
        RuntimeMode.PAPER_EXECUTE: ReleaseDecision.GO_FOR_ACCEPTED_STRATEGY_SMOKE_TEST,
    }[mode]
    audit_path = tmp_path / f"release-{release.value}.md"
    audit_path.write_text(
        (
            f"# Test release evidence\n\n"
            f"Runtime release decision: **{release.value}**\n"
            "Runtime code revision: **test-revision**\n"
        ),
        encoding="utf-8",
    )
    audit_hash = hashlib.sha256(audit_path.read_bytes()).hexdigest()
    return Phase8RuntimeConfig(
        mode=mode,
        paper_execution_authorized=mode is RuntimeMode.PAPER_EXECUTE,
        release_decision=release,
        accepted_strategies=tuple(accepted),
        adaptive_research={"enabled": True, "maximum_new_hypotheses_per_cycle": 1},
        maximum_cycles=1,
        database_path=str(tmp_path / "runtime.sqlite3"),
        phase8_ledger_path=str(tmp_path / "phase8.sqlite3"),
        report_directory=str(tmp_path / "reports"),
        code_revision="test-revision",
        final_audit_path=str(audit_path),
        release_audit_sha256=audit_hash,
    )


def test_research_only_runs_adaptive_shadow_without_order_calls(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert result.preflight_passed is True
    assert result.shadow_signals == 1
    assert result.orders_submitted == 0
    assert calls["execute"] == 0
    assert Path(result.report_path or "").exists()
    payload = json.loads(Path(result.report_path or "").read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2
    assert payload["metrics"]["execution"]["submitted_orders"] == 0
    assert payload["metrics"]["execution"]["net_pnl_usd"] == "0"
    assert payload["metrics"]["execution"]["fee_evidence_status"] == "not_applicable_no_fills"
    assert payload["preflight"]["run_id"] == result.run_id


def test_research_runtime_runs_multiple_non_overlapping_cycles(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY).model_copy(
        update={"maximum_cycles": 2, "cycle_interval_seconds": 0.001}
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
        cycle_events = [
            event for event in runtime.store.events(result.run_id) if event["event_type"] == "cycle_completed"
        ]
    finally:
        runtime.close()
    assert result.safety_halts == ()
    assert len(cycle_events) == 2
    assert calls["execute"] == 0


def test_dry_run_uses_full_path_but_never_calls_broker_submission(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.DRY_RUN,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
        events = runtime.store.events(result.run_id)
    finally:
        runtime.close()
    assert result.preflight_passed is True
    assert result.intents_created == 1
    assert result.orders_submitted == 0
    assert calls["execute"] == 0
    assert any(event["event_type"] == "dry_run_intent_completed" for event in events)


def test_paper_order_persists_exact_client_id_before_executor_mutation(tmp_path: Path) -> None:
    clock = FakeClock()
    observed: dict[str, Any] = {}

    def executor(_request, **kwargs):
        authorization = SimpleNamespace(client_order_id="dec_" + "a" * 32, decision_id="dec_" + "a" * 32)
        kwargs["before_submit"](authorization)
        observed["callback_completed"] = True
        return {
            "status": "ok",
            "execution_status": "submitted_pending",
            "decision_id": authorization.decision_id,
            "order_id": "paper-order-1",
            "order_submitted": True,
        }

    deps, calls = fake_dependencies(clock=clock, executor=executor)
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.PAPER_EXECUTE,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
        unresolved = runtime.store.unresolved_intents()
    finally:
        runtime.close()
    assert observed["callback_completed"] is True
    assert calls["execute"] == 1
    assert result.orders_submitted == 1
    assert unresolved[0]["client_order_id"] == "dec_" + "a" * 32
    assert unresolved[0]["state"] == "ACKNOWLEDGED"


def test_post_execution_position_mismatch_halts_before_another_cycle(tmp_path: Path) -> None:
    clock = FakeClock()

    def executor(request, **kwargs):
        authorization = SimpleNamespace(client_order_id="dec_" + "b" * 32, decision_id="dec_" + "b" * 32)
        kwargs["before_submit"](authorization)
        return {
            "status": "ok",
            "execution_status": "filled",
            "decision_id": authorization.decision_id,
            "order_id": "paper-order-mismatch",
            "order_submitted": True,
            "filled_quantity": str(request.quantity),
            "filled_average_price": "65000",
        }

    deps, calls = fake_dependencies(clock=clock, executor=executor, broker_positions=[])
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.PAPER_EXECUTE,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
        events = runtime.store.events(result.run_id)
    finally:
        runtime.close()

    assert calls["execute"] == 1
    assert "post_execution_owned_position_mismatch" in result.safety_halts
    assert any(event["event_type"] == "post_execution_safety_halt" for event in events)


def test_paper_entry_to_protective_exit_round_trip_is_owned_and_reconciled(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    market = {"bid": "64990", "ask": "65000"}
    broker_positions: list[dict[str, Any]] = []
    sequence = 0

    def executor(request, **kwargs):
        nonlocal sequence
        sequence += 1
        authorization = SimpleNamespace(
            client_order_id="dec_" + str(sequence) * 32,
            decision_id="dec_" + str(sequence) * 32,
        )
        kwargs["before_submit"](authorization)
        if request.side == "buy":
            broker_positions[:] = [
                {
                    "symbol": request.symbol,
                    "qty": str(request.quantity),
                    "market_value": str(request.quantity * Decimal(market["ask"])),
                }
            ]
        else:
            broker_positions.clear()
        return {
            "status": "ok",
            "execution_status": "filled",
            "decision_id": authorization.decision_id,
            "order_id": f"paper-order-{sequence}",
            "order_submitted": True,
            "filled_quantity": str(request.quantity),
            "filled_average_price": market["ask"] if request.side == "buy" else market["bid"],
        }

    deps, calls = fake_dependencies(
        clock=clock,
        executor=executor,
        broker_positions=broker_positions,
        market=market,
    )
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.PAPER_EXECUTE,
        accepted=(accepted_strategy(),),
    )
    first_runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        first = first_runtime.run()
        allocation = first_runtime.store.open_allocations(config.session_id)[0]
        owned_quantity = Decimal(str(allocation["remaining_quantity"]))
        first_equity = first_runtime.store.latest_equity(config.session_id)
    finally:
        first_runtime.close()
    assert first.orders_submitted == 1
    assert first.safety_halts == ()
    assert first_equity is not None
    assert Decimal(str(first_equity["gross_exposure"])) > 0

    assert Decimal(str(broker_positions[0]["qty"])) == owned_quantity
    market.update({"bid": "64000", "ask": "64010"})
    second_runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        second = second_runtime.run()
        open_allocations = second_runtime.store.open_allocations(config.session_id)
        intents = second_runtime.store.intents(config.session_id)
        second_equity = second_runtime.store.latest_equity(config.session_id)
        execution_report = json.loads(Path(second.report_path or "").read_text(encoding="utf-8"))["metrics"][
            "execution"
        ]
    finally:
        second_runtime.close()
    assert second.orders_submitted == 1
    assert second.safety_halts == ()
    assert open_allocations == []
    assert second_equity is not None
    assert Decimal(str(second_equity["gross_exposure"])) == 0
    assert Decimal(str(second_equity["realized_pnl"])) < 0
    assert [row["kind"] for row in intents] == [
        IntentKind.ENTRY.value,
        IntentKind.PROTECTIVE_EXIT.value,
    ]
    assert calls["execute"] == 2
    assert execution_report["submitted_orders"] == 2
    assert execution_report["entry_orders"] == 1
    assert execution_report["exit_orders"] == 1
    assert execution_report["full_fills"] == 2
    assert execution_report["completed_round_trips"] == 1
    assert execution_report["accepted_strategy_trades"] == 1
    assert execution_report["maximum_concurrent_positions"] == 1
    assert execution_report["net_pnl_usd"] is None
    assert execution_report["net_pnl_basis"] == "unavailable_incomplete_fee_evidence"

    broker_positions.clear()
    market.update({"bid": "64990", "ask": "65000"})
    third_runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        third = third_runtime.run()
        third_events = third_runtime.store.events(third.run_id)
    finally:
        third_runtime.close()
    assert third.orders_submitted == 0
    assert calls["execute"] == 2
    assert any(event["event_type"] == "smoke_stage_entry_limit_reached" for event in third_events)


def test_live_profile_fails_before_any_connector_call(tmp_path: Path) -> None:
    clock = FakeClock()
    live = SimpleNamespace(
        id="alpaca-live-trade",
        connector="alpaca",
        environment="live",
        transport="broker_sdk",
        readonly=False,
    )
    deps, calls = fake_dependencies(clock=clock, profile=live)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert result.preflight_passed is False
    assert calls["connection"] == 0
    assert calls["account"] == 0
    assert calls["execute"] == 0


def test_runtime_store_is_append_only_idempotent_and_detects_conflicts(tmp_path: Path) -> None:
    store = Phase8RuntimeStore(tmp_path / "store.sqlite3")
    try:
        first = store.append_event(
            run_id="run_test",
            session_id="session",
            event_type="test",
            payload={
                "value": 1,
                "api_key": "must-not-persist",
                "error": "request failed Authorization: Bearer fake-secret-token",
            },
            idempotency_key="same",
            occurred_at=NOW,
        )
        assert (
            store.append_event(
                run_id="run_test",
                session_id="session",
                event_type="test",
                payload={
                    "value": 1,
                    "api_key": "different-secret",
                    "error": "request failed Authorization: Bearer another-secret-token",
                },
                idempotency_key="same",
                occurred_at=NOW,
            )
            == first
        )
        assert store.events()[0]["payload"]["api_key"] == "[REDACTED]"
        assert "fake-secret-token" not in store.events()[0]["payload"]["error"]
        assert store.verify_event_chain() is True
        with pytest.raises(RuntimePersistenceError):
            store.append_event(
                run_id="run_test",
                session_id="session",
                event_type="different",
                payload={"value": 1},
                idempotency_key="same",
                occurred_at=NOW,
            )
    finally:
        store.close()


def test_config_rejects_embedded_secrets_and_live_profile(tmp_path: Path) -> None:
    secret = tmp_path / "secret.yaml"
    secret.write_text("api_key: nope\n", encoding="utf-8")
    with pytest.raises(ValueError, match="credentials"):
        load_runtime_config(secret)
    with pytest.raises(ValueError, match="alpaca-paper-trade"):
        Phase8RuntimeConfig(broker_profile_id="alpaca-live-trade")
    with pytest.raises(ValueError, match="paper_execution_authorized"):
        Phase8RuntimeConfig(mode=RuntimeMode.PAPER_EXECUTE)
    with pytest.raises(ValueError, match="Extra"):
        Phase8RuntimeConfig.model_validate({"unknown_safety_override": True})


def test_paper_execution_requires_config_and_cli_authorization(tmp_path: Path) -> None:
    config_path = tmp_path / "phase8.yaml"
    config_path.write_text(
        "mode: research_only\npaper_execution_authorized: false\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="paper_execution_authorized"):
        load_runtime_config(
            config_path,
            mode=RuntimeMode.PAPER_EXECUTE,
            authorize_paper_execution=True,
        )

    config_path.write_text(
        "mode: paper_execute\npaper_execution_authorized: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="separate CLI authorization"):
        load_runtime_config(config_path)

    config = load_runtime_config(
        config_path,
        mode=RuntimeMode.PAPER_EXECUTE,
        authorize_paper_execution=True,
    )
    assert config.mode is RuntimeMode.PAPER_EXECUTE
    assert config.paper_execution_authorized is True


@pytest.mark.parametrize(
    ("quote_age_ms", "last_bar_age_minutes", "expected_check"),
    [
        (10_000, 1, "market timestamps valid"),
        (100, 10, "market timestamps valid"),
    ],
)
def test_preflight_rejects_stale_market_inputs(
    tmp_path: Path,
    quote_age_ms: int,
    last_bar_age_minutes: int,
    expected_check: str,
) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(
        clock=clock,
        quote_age_ms=quote_age_ms,
        last_bar_age_minutes=last_bar_age_minutes,
    )
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        report = runtime.preflight("run-stale")
    finally:
        runtime.close()
    item = next(row for row in report.items if row.name == expected_check)
    assert item.status.value == "fail"
    assert calls["execute"] == 0


def test_required_unavailable_module_blocks_preflight(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    base = accepted_strategy()
    required_ai = base.model_copy(
        update={
            "module_policy": {
                **dict(base.module_policy),
                "ai": ModuleRequirement.REQUIRED,
            }
        }
    )
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.DRY_RUN,
        accepted=(required_ai,),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert result.preflight_passed is False
    assert result.intents_created == 0
    assert calls["execute"] == 0


def test_release_decision_cannot_be_bypassed_by_cli_mode(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(tmp_path, mode=RuntimeMode.DRY_RUN).model_copy(
        update={"release_decision": ReleaseDecision.GO_FOR_RESEARCH_ONLY}
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert result.preflight_passed is False
    assert result.intents_created == 0
    assert calls["execute"] == 0


def test_dry_run_fails_closed_when_running_code_is_not_the_reviewed_revision(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    deps.code_integrity_verifier = lambda _revision: (
        False,
        "configured code revision does not match the running checkout",
    )
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.DRY_RUN,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
    finally:
        runtime.close()
    assert result.preflight_passed is False
    assert result.intents_created == 0
    assert calls["execute"] == 0


def test_paper_smoke_preflight_requires_exactly_one_accepted_strategy(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(tmp_path, mode=RuntimeMode.PAPER_EXECUTE)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        result = runtime.run()
        preflight = next(
            event for event in runtime.store.events(result.run_id) if event["event_type"] == "preflight_completed"
        )
    finally:
        runtime.close()
    scope = next(item for item in preflight["payload"]["items"] if item["name"] == "requested-stage strategy set valid")
    assert scope["status"] == "fail"
    assert result.preflight_passed is False
    assert calls["execute"] == 0


def test_validation_profile_defaults_preserve_700_dollars() -> None:
    config = Phase8RuntimeConfig()
    profile = config.validation_profile
    assert profile.internal_capital_usd - profile.maximum_gross_exposure_usd == Decimal("700")
    assert profile.maximum_position_value_usd == Decimal("150")
    assert profile.maximum_total_submitted_orders == 50


def test_named_validation_and_experimental_profiles_cannot_be_weakened() -> None:
    with pytest.raises(ValueError, match="profile is locked"):
        Phase8ValidationProfile(maximum_position_value_usd=Decimal("149"))
    with pytest.raises(ValueError, match="locked cap"):
        ExperimentalRiskProfile(maximum_position_value_usd=Decimal("51"))
    with pytest.raises(ValueError, match="locked Phase 8 validation profile"):
        Phase8RuntimeConfig(
            mode=RuntimeMode.DRY_RUN,
            validation_profile={"name": "custom-research-profile"},
        )


def test_market_signal_and_intent_contracts_reject_noncausal_or_unsafe_values() -> None:
    with pytest.raises(ValueError, match="OHLC"):
        Bar(
            timestamp=NOW - timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("99"),
            low=Decimal("98"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )

    with pytest.raises(ValueError, match="positive and finite"):
        OrderIntent.model_validate(
            {
                **_entry_intent().model_dump(mode="python"),
                "quantity": Decimal("0"),
            }
        )


def _entry_intent(*, intent_id: str = "intent-entry", decision_id: str = "decision-entry") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        decision_id=decision_id,
        run_id="run-recovery",
        session_id="phase8-bounded-validation",
        strategy_id="accepted-btc-trend",
        strategy_version="1.0.0",
        signal_id="signal-entry",
        symbol="BTC/USD",
        side="buy",
        quantity=Decimal("0.002"),
        order_type="market",
        time_in_force="gtc",
        kind=IntentKind.ENTRY,
        risk_approved=True,
        latency_approved=True,
        reconciliation_approved=True,
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=5),
        client_order_id="client-entry",
        proposed_stop=Decimal("64000"),
        proposed_target=Decimal("67000"),
        holding_deadline=NOW + timedelta(hours=1),
    )


def test_cumulative_partial_fills_are_idempotent_for_entry_and_exit(tmp_path: Path) -> None:
    store = Phase8RuntimeStore(tmp_path / "fills.sqlite3")
    try:
        entry = _entry_intent()
        assert store.create_intent(entry) is True
        first = store.apply_cumulative_fill(
            intent=entry,
            cumulative_quantity=Decimal("0.001"),
            average_fill_price=Decimal("65000"),
            observed_at=NOW,
        )
        duplicate = store.apply_cumulative_fill(
            intent=entry,
            cumulative_quantity=Decimal("0.001"),
            average_fill_price=Decimal("65000"),
            observed_at=NOW,
        )
        assert first["remaining_quantity"] == duplicate["remaining_quantity"] == "0.001"
        completed_entry = store.apply_cumulative_fill(
            intent=entry,
            cumulative_quantity=Decimal("0.002"),
            average_fill_price=Decimal("65100"),
            observed_at=NOW,
        )
        assert completed_entry["quantity"] == "0.002"
        assert completed_entry["remaining_quantity"] == "0.002"

        exit_intent = _entry_intent(
            intent_id="intent-exit",
            decision_id="decision-exit",
        ).model_copy(
            update={
                "signal_id": "signal-exit",
                "side": "sell",
                "kind": IntentKind.PROTECTIVE_EXIT,
                "client_order_id": "client-exit",
                "allocation_id": completed_entry["allocation_id"],
                "proposed_stop": None,
                "proposed_target": None,
                "holding_deadline": None,
            }
        )
        assert store.create_intent(exit_intent) is True
        store.apply_cumulative_fill(
            intent=exit_intent,
            cumulative_quantity=Decimal("0.001"),
            average_fill_price=Decimal("66000"),
            observed_at=NOW,
        )
        same_exit = store.apply_cumulative_fill(
            intent=exit_intent,
            cumulative_quantity=Decimal("0.001"),
            average_fill_price=Decimal("66000"),
            observed_at=NOW,
        )
        assert same_exit["remaining_quantity"] == "0.001"
        closed = store.apply_cumulative_fill(
            intent=exit_intent,
            cumulative_quantity=Decimal("0.002"),
            average_fill_price=Decimal("66100"),
            observed_at=NOW,
        )
        assert closed["remaining_quantity"] == "0.000"
        assert closed["state"] == "closed"
    finally:
        store.close()


def test_execution_report_reconciles_observed_slippage_and_modelled_fees(tmp_path: Path) -> None:
    store = Phase8RuntimeStore(tmp_path / "execution-report.sqlite3")
    try:
        strategy = accepted_strategy()
        store.register_strategy(strategy, registered_at=NOW)
        entry = _entry_intent().model_copy(
            update={
                "quantity": Decimal("0.002"),
                "proposed_stop": Decimal("90"),
                "proposed_target": Decimal("120"),
            }
        )
        assert store.create_intent(entry) is True
        allocation = store.apply_cumulative_fill(
            intent=entry,
            cumulative_quantity=Decimal("0.002"),
            average_fill_price=Decimal("100"),
            observed_at=NOW,
        )
        exit_intent = entry.model_copy(
            update={
                "intent_id": "intent-report-exit",
                "decision_id": "decision-report-exit",
                "signal_id": "signal-report-exit",
                "side": "sell",
                "kind": IntentKind.PROTECTIVE_EXIT,
                "client_order_id": "client-report-exit",
                "allocation_id": allocation["allocation_id"],
                "proposed_stop": None,
                "proposed_target": None,
                "holding_deadline": None,
            }
        )
        assert store.create_intent(exit_intent) is True
        store.apply_cumulative_fill(
            intent=exit_intent,
            cumulative_quantity=Decimal("0.002"),
            average_fill_price=Decimal("110"),
            observed_at=NOW + timedelta(minutes=1),
        )
        for intent, reference in ((entry, "99"), (exit_intent, "111")):
            store.append_event(
                run_id="run-report",
                session_id=entry.session_id,
                event_type="paper_execution_result",
                payload={
                    "decision_id": intent.decision_id,
                    "order_submitted": True,
                    "execution_status": "filled",
                    "decision_reference_price": reference,
                    "decision_economics": {"fee_bps": 10},
                },
                idempotency_key=f"paper-result:{intent.intent_id}",
                occurred_at=NOW,
            )
        store.record_equity(
            run_id="run-report",
            session_id=entry.session_id,
            observed_at=NOW + timedelta(minutes=2),
            initial_capital=Decimal("1000"),
            unrealized_pnl=Decimal("0"),
            gross_exposure=Decimal("0"),
        )

        metrics = build_execution_metrics(
            run_id="run-report",
            session_id=entry.session_id,
            summary={
                "validation_profile": {
                    "internal_capital_usd": "1000",
                },
                "safety_halts": [],
            },
            store=store,
        )
    finally:
        store.close()

    assert metrics["full_fills"] == 2
    assert metrics["completed_round_trips"] == 1
    assert metrics["observed_slippage_cost_usd"] == "0.004"
    assert metrics["modelled_fee_cost_usd"] == "0.00042"
    assert metrics["net_pnl_usd"] == "0.01958"
    assert metrics["percentage_return_fraction"] == "0.00001958"


def test_restart_closes_persisted_but_unsubmitted_intent(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, _calls = fake_dependencies(clock=clock)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        intent = _entry_intent(decision_id="").model_copy(update={"decision_id": None})
        runtime.store.create_intent(intent)
        runtime.store.transition_intent(intent.intent_id, IntentState.VALIDATED, at=clock())
        assert runtime._recover_runtime_intents() == []
        assert runtime.store.get_intent(intent.intent_id)["state"] == IntentState.CLOSED.value
        assert deps.phase8_reconciler is not None
    finally:
        runtime.close()


def test_restart_recovers_ambiguous_filled_entry_without_resubmission(tmp_path: Path) -> None:
    clock = FakeClock()
    reconcile_calls: list[str] = []

    def reconciler(decision_id, **_kwargs):
        reconcile_calls.append(decision_id)
        return {
            "status": "ok",
            "execution_status": "filled",
            "order_id": "paper-order-recovered",
            "filled_quantity": "0.002",
            "filled_average_price": "65100",
        }

    deps, calls = fake_dependencies(clock=clock, reconciler=reconciler)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        intent = _entry_intent()
        runtime.store.create_intent(intent)
        runtime.store.transition_intent(intent.intent_id, IntentState.VALIDATED, at=clock())
        runtime.store.transition_intent(intent.intent_id, IntentState.SUBMITTING, at=clock())
        runtime.store.transition_intent(intent.intent_id, IntentState.AMBIGUOUS, at=clock())
        assert runtime._recover_runtime_intents() == []
        assert reconcile_calls == ["decision-entry"]
        assert calls["execute"] == 0
        recovered = runtime.store.allocation_for_entry(intent.intent_id)
        assert recovered is not None
        assert recovered["remaining_quantity"] == "0.002"
        assert runtime.store.get_intent(intent.intent_id)["state"] == IntentState.CLOSED.value
    finally:
        runtime.close()


def test_stale_owned_paper_order_is_cancelled_at_most_once(tmp_path: Path) -> None:
    clock = FakeClock()
    deps, calls = fake_dependencies(clock=clock)
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.PAPER_EXECUTE,
        accepted=(accepted_strategy(),),
    ).model_copy(update={"pending_order_cancel_after_seconds": 1.0})
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        intent = _entry_intent()
        runtime.store.create_intent(intent)
        runtime.store.transition_intent(intent.intent_id, IntentState.VALIDATED, at=clock())
        runtime.store.transition_intent(intent.intent_id, IntentState.SUBMITTING, at=clock())
        runtime.store.transition_intent(
            intent.intent_id,
            IntentState.ACKNOWLEDGED,
            at=clock(),
            broker_order_id="paper-order-stale",
        )
        clock.value += timedelta(seconds=2)
        assert runtime._cancel_stale_owned_orders(run_id="run-cancel") == []
        assert runtime._cancel_stale_owned_orders(run_id="run-cancel") == []
        assert calls["cancel"] == 1
        assert runtime.store.get_intent(intent.intent_id)["state"] == IntentState.CANCEL_PENDING.value
    finally:
        runtime.close()


def test_dry_run_protective_exit_precedes_new_entries(tmp_path: Path) -> None:
    clock = FakeClock()
    broker_position = {"symbol": "BTC/USD", "qty": "0.001", "market_value": "64.99"}
    deps, calls = fake_dependencies(clock=clock, broker_positions=[broker_position])
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.DRY_RUN,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        runtime.store.add_allocation(
            session_id=config.session_id,
            strategy_key=accepted_strategy().key,
            symbol="BTC/USD",
            entry_intent_id="prior-filled-entry",
            quantity=Decimal("0.001"),
            average_fill_price=Decimal("65000"),
            stop_price=Decimal("64995"),
            target_price=Decimal("67000"),
            opened_at=NOW - timedelta(minutes=5),
            holding_deadline=NOW + timedelta(hours=1),
        )
        result = runtime.run()
        events = runtime.store.events(result.run_id)
        intents = runtime.store.intents(config.session_id)
    finally:
        runtime.close()
    assert result.intents_created == 1
    assert result.orders_submitted == 0
    assert calls["execute"] == 0
    assert intents[0]["kind"] == IntentKind.PROTECTIVE_EXIT.value
    assert any(event["event_type"] == "protective_exit_triggered" for event in events)
    assert not any(event["event_type"] == "dry_run_intent_completed" for event in events)


def test_session_loss_halt_generates_emergency_exit_not_new_entry(tmp_path: Path) -> None:
    clock = FakeClock()
    market = {"bid": "40000", "ask": "40010"}
    broker_position = {"symbol": "BTC/USD", "qty": "0.001", "market_value": "40"}
    deps, _calls = fake_dependencies(
        clock=clock,
        broker_positions=[broker_position],
        market=market,
    )
    config = runtime_config(
        tmp_path,
        mode=RuntimeMode.DRY_RUN,
        accepted=(accepted_strategy(),),
    )
    runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        runtime.store.add_allocation(
            session_id=config.session_id,
            strategy_key=accepted_strategy().key,
            symbol="BTC/USD",
            entry_intent_id="loss-entry",
            quantity=Decimal("0.001"),
            average_fill_price=Decimal("65000"),
            stop_price=Decimal("64000"),
            target_price=Decimal("67000"),
            opened_at=NOW - timedelta(minutes=5),
            holding_deadline=NOW + timedelta(hours=1),
        )
        result = runtime.run()
        exit_events = [
            event for event in runtime.store.events(result.run_id) if event["event_type"] == "protective_exit_triggered"
        ]
    finally:
        runtime.close()
    assert result.intents_created == 1
    assert exit_events[0]["payload"]["reason"] == "emergency_account_risk_halt"


def test_adaptive_shadow_position_closes_with_costed_trade_evidence(tmp_path: Path) -> None:
    clock = FakeClock()
    market = {"bid": "64990", "ask": "65000"}
    deps, calls = fake_dependencies(clock=clock, market=market)
    config = runtime_config(tmp_path, mode=RuntimeMode.RESEARCH_ONLY)
    first_runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        first = first_runtime.run()
        assert len(first_runtime.store.open_shadow_positions(config.session_id)) == 1
    finally:
        first_runtime.close()

    market.update({"bid": "67000", "ask": "67010"})
    second_runtime = build_phase8_runtime(config, dependencies=deps)
    try:
        second = second_runtime.run()
        summaries = [
            second_runtime.store.shadow_summary(strategy.key)
            for strategy in second_runtime.store.strategies(state=StrategyState.SHADOW.value)
        ]
        events = second_runtime.store.events(second.run_id)
    finally:
        second_runtime.close()
    assert first.orders_submitted == second.orders_submitted == 0
    assert calls["execute"] == 0
    assert any(summary["completed_trades"] == 1 for summary in summaries)
    assert any(event["event_type"] == "shadow_position_closed" for event in events)


def test_strategy_version_registry_rejects_semantic_mutation(tmp_path: Path) -> None:
    store = Phase8RuntimeStore(tmp_path / "strategies.sqlite3")
    try:
        strategy = accepted_strategy()
        store.register_strategy(strategy, registered_at=NOW)
        mutated = strategy.model_copy(update={"maximum_spread_bps": Decimal("25")})
        with pytest.raises(RuntimePersistenceError, match="mutated"):
            store.register_strategy(mutated, registered_at=NOW)
    finally:
        store.close()


def test_controlled_promotion_creates_new_immutable_experimental_version() -> None:
    accepted = accepted_strategy()
    shadow = accepted.model_copy(
        update={
            "strategy_id": "adaptive-trend-btc-usd",
            "version": "v1-shadow",
            "state": StrategyState.SHADOW,
            "expected_gross_edge_bps": Decimal("0"),
            "edge_estimator_version": "unvalidated",
            "confidence_calibration_version": "uncalibrated",
            "execution_permissions": (),
            "permitted_runtime_modes": (
                RuntimeMode.RESEARCH_ONLY,
                RuntimeMode.DRY_RUN,
            ),
        }
    )
    evidence = PromotionEvidence(
        strategy_key=shadow.key,
        evidence_sha256="a" * 64,
        registered_at=NOW,
        independent_signals=40,
        completed_trades=30,
        net_pnl_after_costs_usd=Decimal("7.50"),
        net_expectancy_usd=Decimal("0.25"),
        profit_factor=Decimal("1.4"),
        average_winner_usd=Decimal("0.80"),
        average_loser_usd=Decimal("-0.40"),
        win_rate_fraction=Decimal("0.60"),
        downside_deviation_usd=Decimal("0.30"),
        turnover_usd=Decimal("900"),
        maximum_exposure_usd=Decimal("50"),
        maximum_drawdown_fraction=Decimal("0.05"),
        profitable_time_slices_fraction=Decimal("0.70"),
        maximum_single_trade_pnl_fraction=Decimal("0.20"),
        maximum_single_symbol_pnl_fraction=Decimal("0.40"),
        profitable_regimes_fraction=Decimal("0.67"),
        slippage_stress_expectancy_usd=Decimal("0.10"),
        latency_stress_expectancy_usd=Decimal("0.08"),
        missing_data_behavior_passed=True,
        module_failure_behavior_passed=True,
        duplicate_similarity_fraction=Decimal("0.20"),
        selection_registry_sha256="c" * 64,
        transaction_cost_model_sha256="d" * 64,
        untouched_out_of_sample=True,
        untouched_out_of_sample_artifact_sha256="b" * 64,
        latency_feasible=True,
        validated_gross_edge_bps=Decimal("20"),
        edge_estimator_version="prospective-edge-v1",
        confidence_calibration_version="prospective-calibration-v1",
    )
    promoted = PromotionController(AdaptiveResearchConfig()).promote(
        shadow,
        evidence,
        code_revision="reviewed-revision",
    )
    assert promoted.state is StrategyState.EXPERIMENTAL_PAPER
    assert promoted.key != shadow.key
    assert shadow.key in promoted.parent_ids
    assert promoted.risk.maximum_position_value_usd == Decimal("50")
    assert promoted.execution_permissions == ("paper_order",)


def test_controlled_promotion_rejects_missing_untouched_oos_evidence() -> None:
    accepted = accepted_strategy()
    shadow = accepted.model_copy(
        update={
            "state": StrategyState.SHADOW,
            "expected_gross_edge_bps": Decimal("0"),
            "edge_estimator_version": "unvalidated",
            "confidence_calibration_version": "uncalibrated",
            "execution_permissions": (),
        }
    )
    evidence = PromotionEvidence(
        strategy_key=shadow.key,
        evidence_sha256="c" * 64,
        registered_at=NOW,
        independent_signals=40,
        completed_trades=30,
        net_pnl_after_costs_usd=Decimal("7.50"),
        net_expectancy_usd=Decimal("0.25"),
        profit_factor=Decimal("1.4"),
        average_winner_usd=Decimal("0.80"),
        average_loser_usd=Decimal("-0.40"),
        win_rate_fraction=Decimal("0.60"),
        downside_deviation_usd=Decimal("0.30"),
        turnover_usd=Decimal("900"),
        maximum_exposure_usd=Decimal("50"),
        maximum_drawdown_fraction=Decimal("0.05"),
        profitable_time_slices_fraction=Decimal("0.70"),
        maximum_single_trade_pnl_fraction=Decimal("0.20"),
        maximum_single_symbol_pnl_fraction=Decimal("0.40"),
        profitable_regimes_fraction=Decimal("0.67"),
        slippage_stress_expectancy_usd=Decimal("0.10"),
        latency_stress_expectancy_usd=Decimal("0.08"),
        missing_data_behavior_passed=True,
        module_failure_behavior_passed=True,
        duplicate_similarity_fraction=Decimal("0.20"),
        selection_registry_sha256="e" * 64,
        transaction_cost_model_sha256="f" * 64,
        untouched_out_of_sample=False,
        untouched_out_of_sample_artifact_sha256="d" * 64,
        latency_feasible=True,
        validated_gross_edge_bps=Decimal("20"),
        edge_estimator_version="prospective-edge-v1",
        confidence_calibration_version="prospective-calibration-v1",
    )
    with pytest.raises(ValueError, match="untouched_out_of_sample_missing"):
        PromotionController(AdaptiveResearchConfig()).promote(
            shadow,
            evidence,
            code_revision="reviewed-revision",
        )
