"""Deterministic, evidence-qualified reports for Phase 8 runtime runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.phase8_runtime.models import IntentState, StrategyState
from src.phase8_runtime.persistence import Phase8RuntimeStore


_UNRESOLVED_STATES = {
    IntentState.SUBMITTING.value,
    IntentState.ACKNOWLEDGED.value,
    IntentState.PARTIALLY_FILLED.value,
    IntentState.CANCEL_PENDING.value,
    IntentState.AMBIGUOUS.value,
    IntentState.RECONCILIATION_REQUIRED.value,
    IntentState.FILLED.value,
}


def _decimal(value: Any, *, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _fraction(numerator: Decimal, denominator: Decimal) -> str | None:
    if denominator == 0:
        return None
    return str(numerator / denominator)


def _maximum_concurrent_positions(
    allocations: Sequence[Mapping[str, Any]],
    intents_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[int, int]:
    """Return maximum concurrency and closed intervals lacking a close time."""
    timeline: list[tuple[datetime, int]] = []
    missing_close_times = 0
    for allocation in allocations:
        opened_at = _utc(allocation.get("opened_at"))
        if opened_at is None:
            continue
        timeline.append((opened_at, 1))
        if str(allocation.get("state")) != "closed":
            continue
        exit_intent = intents_by_id.get(str(allocation.get("exit_intent_id") or ""))
        closed_at = _utc(exit_intent.get("updated_at")) if exit_intent is not None else None
        if closed_at is None or closed_at < opened_at:
            missing_close_times += 1
            continue
        timeline.append((closed_at, -1))

    # Process exits before entries at an identical timestamp so concurrency is
    # not overstated during an instantaneous hand-off.
    timeline.sort(key=lambda item: (item[0], item[1]))
    current = 0
    maximum = 0
    for _timestamp, delta in timeline:
        current = max(0, current + delta)
        maximum = max(maximum, current)
    return maximum, missing_close_times


def _terminal_outcomes(
    intents: Sequence[Mapping[str, Any]],
    session_events: Sequence[Mapping[str, Any]],
    fills_by_intent: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """Recover the best-supported terminal outcome for each mutable intent."""
    outcomes: dict[str, str] = {}
    by_decision = {
        str(intent.get("decision_id")): str(intent["intent_id"]) for intent in intents if intent.get("decision_id")
    }
    by_signal = {str(intent["signal_id"]): str(intent["intent_id"]) for intent in intents}
    for event in session_events:
        event_type = str(event.get("event_type"))
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event_type == "intent_reconciled":
            reconciled_intent_id = str(payload.get("intent_id") or "")
            target = str(payload.get("target_state") or "")
            if reconciled_intent_id and target:
                outcomes[reconciled_intent_id] = target
        elif event_type == "paper_execution_result":
            paper_intent_id = by_decision.get(str(payload.get("decision_id") or ""))
            status = str(payload.get("execution_status") or "").lower()
            mapped = {
                "filled": IntentState.FILLED.value,
                "rejected": IntentState.REJECTED.value,
                "broker_submission_failed": IntentState.REJECTED.value,
                "submission_ambiguous": IntentState.AMBIGUOUS.value,
            }.get(status)
            if paper_intent_id and mapped:
                outcomes[paper_intent_id] = mapped
        elif event_type == "paper_submission_ambiguous":
            ambiguous_intent_id = by_signal.get(str(payload.get("signal_id") or ""))
            if ambiguous_intent_id:
                outcomes[ambiguous_intent_id] = IntentState.AMBIGUOUS.value

    for intent in intents:
        intent_id = str(intent["intent_id"])
        fill = fills_by_intent.get(intent_id)
        if fill is not None:
            filled = _decimal(fill.get("cumulative_quantity"))
            requested = _decimal(fill.get("requested_quantity"))
            if requested > 0 and filled >= requested:
                outcomes[intent_id] = IntentState.FILLED.value
        state = str(intent.get("state") or "")
        if state in {
            IntentState.CANCELLED.value,
            IntentState.REJECTED.value,
            IntentState.EXPIRED.value,
            IntentState.AMBIGUOUS.value,
            IntentState.RECONCILIATION_REQUIRED.value,
        }:
            outcomes[intent_id] = state
    return outcomes


def _execution_cost_evidence(
    *,
    fills: Sequence[Mapping[str, Any]],
    session_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reconcile observed fill slippage and decision-time modelled fees."""
    result_events: dict[str, Mapping[str, Any]] = {}
    for event in session_events:
        if event.get("event_type") != "paper_execution_result":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        decision_id = str(payload.get("decision_id") or "")
        if decision_id:
            result_events[decision_id] = payload

    observed_slippage = Decimal("0")
    modelled_fees = Decimal("0")
    slippage_covered = 0
    fee_covered = 0
    for fill in fills:
        payload = result_events.get(str(fill.get("decision_id") or ""))
        if payload is None:
            continue
        quantity = _decimal(fill.get("cumulative_quantity"))
        fill_price = _decimal(fill.get("average_fill_price"))
        reference = _decimal(payload.get("decision_reference_price"))
        if quantity > 0 and fill_price > 0 and reference > 0:
            direction = Decimal("1") if str(fill.get("side")) == "buy" else Decimal("-1")
            observed_slippage += (fill_price - reference) * quantity * direction
            slippage_covered += 1
        economics = payload.get("decision_economics")
        fee_bps = _decimal(economics.get("fee_bps")) if isinstance(economics, Mapping) else Decimal("-1")
        if quantity > 0 and fill_price > 0 and fee_bps >= 0:
            modelled_fees += quantity * fill_price * fee_bps / Decimal("10000")
            fee_covered += 1

    fill_count = len(fills)
    return {
        "observed_slippage_cost_usd": _money(observed_slippage) if slippage_covered else None,
        "observed_slippage_fill_coverage": f"{slippage_covered}/{fill_count}",
        "modelled_fee_cost_usd": (
            "0" if fill_count == 0 else (_money(modelled_fees) if fee_covered == fill_count else None)
        ),
        "modelled_fee_fill_coverage": f"{fee_covered}/{fill_count}",
        "broker_reported_fee_cost_usd": None,
        "fee_evidence_status": (
            "not_applicable_no_fills"
            if fill_count == 0
            else (
                "complete_modelled_fee_evidence;broker_reported_fees_unavailable"
                if fee_covered == fill_count
                else "incomplete_modelled_fee_evidence;broker_reported_fees_unavailable"
            )
        ),
        "modelled_fees": (Decimal("0") if fill_count == 0 else (modelled_fees if fee_covered == fill_count else None)),
    }


def build_execution_metrics(
    *,
    run_id: str,
    session_id: str,
    summary: Mapping[str, Any],
    store: Phase8RuntimeStore,
) -> dict[str, Any]:
    """Build session-level execution metrics with explicit evidence limits."""
    intents = store.intents(session_id)
    fills = store.intent_fills(session_id)
    allocations = store.allocations(session_id)
    equity = store.equity_snapshots(session_id)
    session_events = [event for event in store.events() if str(event.get("session_id")) == session_id]
    intents_by_id = {str(intent["intent_id"]): intent for intent in intents}
    fills_by_intent = {str(fill["intent_id"]): fill for fill in fills}
    outcomes = _terminal_outcomes(intents, session_events, fills_by_intent)

    result_events = [
        event
        for event in session_events
        if event.get("event_type") == "paper_execution_result"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("order_submitted") is True
    ]
    submitted_decisions = {
        str(event["payload"].get("decision_id")) for event in result_events if event["payload"].get("decision_id")
    }
    submitted_intents = [
        intent
        for intent in intents
        if str(intent.get("decision_id") or "") in submitted_decisions or intent.get("broker_order_id")
    ]
    full_fills = 0
    partial_fills = 0
    for fill in fills:
        quantity = _decimal(fill.get("cumulative_quantity"))
        requested = _decimal(fill.get("requested_quantity"))
        if quantity > 0 and requested > 0 and quantity >= requested:
            full_fills += 1
        elif quantity > 0:
            partial_fills += 1

    closed_allocations = [allocation for allocation in allocations if allocation.get("state") == "closed"]
    strategies = {strategy.key: strategy.state for strategy in store.strategies()}
    accepted_trades = sum(
        strategies.get(str(allocation.get("strategy_key"))) is StrategyState.ACCEPTED_PAPER
        for allocation in closed_allocations
    )
    experimental_trades = sum(
        strategies.get(str(allocation.get("strategy_key"))) is StrategyState.EXPERIMENTAL_PAPER
        for allocation in closed_allocations
    )
    max_positions, missing_close_times = _maximum_concurrent_positions(allocations, intents_by_id)

    latest_equity = equity[-1] if equity else None
    realized_before_fees = _decimal(latest_equity.get("realized_pnl")) if latest_equity else Decimal("0")
    unrealized_before_fees = _decimal(latest_equity.get("unrealized_pnl")) if latest_equity else Decimal("0")
    attributable_before_fees = realized_before_fees + unrealized_before_fees
    maximum_exposure = max((_decimal(row.get("gross_exposure")) for row in equity), default=Decimal("0"))
    maximum_drawdown = max((_decimal(row.get("drawdown_fraction")) for row in equity), default=Decimal("0"))
    profile = summary.get("validation_profile")
    initial_capital = (
        _decimal(profile.get("internal_capital_usd"), default=Decimal("0"))
        if isinstance(profile, Mapping)
        else Decimal("0")
    )
    minimum_cash_reserve = initial_capital - maximum_exposure if initial_capital else None

    costs = _execution_cost_evidence(fills=fills, session_events=session_events)
    modelled_fees = costs.pop("modelled_fees")
    net_pnl = attributable_before_fees - modelled_fees if modelled_fees is not None else None
    closed_trade_pnls = [_decimal(allocation.get("realized_pnl")) for allocation in closed_allocations]
    winners = [value for value in closed_trade_pnls if value > 0]
    losers = [value for value in closed_trade_pnls if value < 0]
    gross_profit = sum(winners, Decimal("0"))
    gross_loss = abs(sum(losers, Decimal("0")))
    trade_count = len(closed_trade_pnls)

    latency_rejections = 0
    risk_rejections = 0
    reconciliation_discrepancies = 0
    preflight_failures = 0
    for event in session_events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        event_type = str(event.get("event_type"))
        if event_type == "phase8_execution_blocked":
            latency_rejections += 1
        elif event_type == "signal_rejected" and "stale" in str(payload.get("reason", "")).lower():
            latency_rejections += 1
        elif event_type == "strategy_input_rejected" and any(
            "stale" in str(reason).lower() or "future" in str(reason).lower() for reason in payload.get("reasons", [])
        ):
            latency_rejections += 1
        if event_type == "risk_evaluated" and payload.get("approved") is False:
            risk_rejections += 1
        if event_type == "restart_reconciliation_completed":
            reconciliation_discrepancies += len(payload.get("errors", []))
        if event_type == "preflight_completed":
            preflight_failures += sum(
                item.get("status") == "fail" for item in payload.get("items", []) if isinstance(item, Mapping)
            )

    safety_halts = [str(item) for item in summary.get("safety_halts", [])]
    reconciliation_discrepancies += sum(
        "reconciliation" in item or "mismatch" in item or item == "external_open_orders" for item in safety_halts
    )
    unresolved = sum(str(intent.get("state")) in _UNRESOLVED_STATES for intent in intents)
    outcome_counts = {
        state: sum(outcome == state for outcome in outcomes.values())
        for state in (
            IntentState.CANCELLED.value,
            IntentState.REJECTED.value,
            IntentState.EXPIRED.value,
            IntentState.AMBIGUOUS.value,
            IntentState.RECONCILIATION_REQUIRED.value,
        )
    }

    return {
        "scope": "cumulative_session",
        "session_id": session_id,
        "reporting_run_id": run_id,
        "submission_attempts": sum(bool(intent.get("decision_id")) for intent in intents),
        "submitted_orders": len(submitted_intents),
        "entry_orders": sum(intent.get("kind") == "entry" for intent in submitted_intents),
        "exit_orders": sum(intent.get("kind") != "entry" for intent in submitted_intents),
        "cancelled_orders": outcome_counts[IntentState.CANCELLED.value],
        "rejected_orders": outcome_counts[IntentState.REJECTED.value],
        "expired_orders": outcome_counts[IntentState.EXPIRED.value],
        "ambiguous_orders": outcome_counts[IntentState.AMBIGUOUS.value],
        "reconciliation_required_orders": outcome_counts[IntentState.RECONCILIATION_REQUIRED.value],
        "partial_fills_current": partial_fills,
        "full_fills": full_fills,
        "unresolved_orders": unresolved,
        "completed_round_trips": len(closed_allocations),
        "accepted_strategy_trades": accepted_trades,
        "experimental_strategy_trades": experimental_trades,
        "maximum_concurrent_positions": max_positions,
        "concurrency_evidence_missing_close_times": missing_close_times,
        "maximum_exposure_usd": _money(maximum_exposure),
        "minimum_attributable_cash_reserve_usd": _money(minimum_cash_reserve),
        "realized_pnl_before_modelled_fees_usd": _money(realized_before_fees),
        "unrealized_pnl_before_modelled_fees_usd": _money(unrealized_before_fees),
        "attributable_pnl_before_modelled_fees_usd": _money(attributable_before_fees),
        "net_pnl_usd": _money(net_pnl),
        "net_pnl_basis": (
            "fill_price_pnl_minus_decision_time_modelled_fees"
            if net_pnl is not None
            else "unavailable_incomplete_fee_evidence"
        ),
        "percentage_return_fraction": (
            _fraction(net_pnl, initial_capital) if net_pnl is not None and initial_capital else None
        ),
        "trade_statistics_basis": "fill_price_pnl_before_unrecorded_fees",
        "win_rate_fraction": _fraction(Decimal(len(winners)), Decimal(trade_count)) if trade_count else None,
        "average_winner_usd": _money(gross_profit / len(winners)) if winners else None,
        "average_loser_usd": _money(sum(losers, Decimal("0")) / len(losers)) if losers else None,
        "profit_factor": _fraction(gross_profit, gross_loss) if gross_loss else None,
        "expectancy_usd": _money(sum(closed_trade_pnls, Decimal("0")) / trade_count) if trade_count else None,
        "maximum_drawdown_fraction_before_modelled_fees": str(maximum_drawdown),
        **costs,
        "latency_rejection_count": latency_rejections,
        "risk_rejection_count": risk_rejections,
        "reconciliation_discrepancies": reconciliation_discrepancies,
        "preflight_rule_failures": preflight_failures,
        "critical_rule_violation_events": sum(
            event.get("event_type") == "critical_rule_violation" for event in session_events
        ),
        "safety_halts": safety_halts,
        "limitations": [
            "partial_fills_current counts cumulative fill projections, not every historical partial-fill transition",
            "broker-reported fees are unavailable; net P&L is emitted only with complete modelled-fee coverage",
            "drawdown and trade statistics use fill-price P&L before modelled fees",
            "maximum exposure is bounded by persisted account-risk snapshot frequency",
        ],
    }


def _adaptive_metrics(
    *,
    strategies: Sequence[Any],
    session_events: Sequence[Mapping[str, Any]],
    shadow_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    state_counts = {state.value: 0 for state in StrategyState}
    for strategy in strategies:
        state_counts[strategy.state.value] += 1
    promotion_events = [event for event in session_events if event.get("event_type") == "shadow_promotion_evaluated"]
    return {
        "strategy_state_counts": state_counts,
        "generated_hypotheses": sum(
            event.get("event_type") == "strategy_hypothesis_generated" for event in session_events
        ),
        "promotion_evaluations": len(promotion_events),
        "approved_promotions": sum(
            isinstance(event.get("payload"), Mapping) and event["payload"].get("approved") is True
            for event in promotion_events
        ),
        "demotions": sum(event.get("event_type") == "experimental_strategy_demoted" for event in session_events),
        "shadow_evidence": dict(shadow_evidence),
        "overfitting_warning": (
            "No strategy may be promoted without frozen selection registry, cost model, and untouched OOS hashes."
        ),
    }


def write_runtime_report(
    *,
    report_directory: Path,
    run_id: str,
    summary: Mapping[str, Any],
    store: Phase8RuntimeStore,
) -> Path:
    """Write machine and human reports derived only from persisted evidence."""
    report_directory.mkdir(parents=True, exist_ok=True)
    events = store.events(run_id)
    event_counts: dict[str, int] = {}
    for event in events:
        event_type = str(event["event_type"])
        event_counts[event_type] = event_counts.get(event_type, 0) + 1
    session_id = str(summary.get("session_id") or "")
    strategies = store.strategies()
    shadow_evidence = {
        strategy.key: store.shadow_summary(strategy.key)
        for strategy in strategies
        if strategy.state is StrategyState.SHADOW
    }
    session_events = [event for event in store.events() if str(event.get("session_id")) == session_id]
    execution = build_execution_metrics(
        run_id=run_id,
        session_id=session_id,
        summary=summary,
        store=store,
    )
    preflight = next(
        (event["payload"] for event in reversed(events) if event["event_type"] == "preflight_completed"),
        None,
    )
    metrics = {
        "event_counts": event_counts,
        "intent_counts": store.intent_counts(session_id),
        "open_owned_allocations": len(store.open_allocations(session_id)),
        "completed_round_trips": store.completed_round_trips(session_id),
        "latest_attributable_equity": store.latest_equity(session_id),
        "registered_strategy_versions": len(strategies),
        "adaptive_research": _adaptive_metrics(
            strategies=strategies,
            session_events=session_events,
            shadow_evidence=shadow_evidence,
        ),
        "execution": execution,
    }
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "summary": dict(summary),
        "preflight": preflight,
        "metrics": metrics,
        "event_chain_valid": store.verify_event_chain(),
        "events": events,
    }
    json_path = report_directory / f"{run_id}.json"
    json_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    markdown = [
        "# Phase 8 Runtime Report",
        "",
        "## Executive result",
        "",
        f"- Run: `{run_id}`",
        f"- Mode: `{summary.get('mode', 'unknown')}`",
        f"- Status: `{summary.get('status', 'unknown')}`",
        f"- Preflight: `{summary.get('preflight_passed', False)}`",
        f"- Event chain valid: `{payload['event_chain_valid']}`",
        f"- Release decision: `{summary.get('release_decision', 'unknown')}`",
        f"- Paper orders submitted: `{execution['submitted_orders']}`",
        "",
        "## Execution evidence (cumulative session)",
        "",
        f"- Entry / exit orders: `{execution['entry_orders']} / {execution['exit_orders']}`",
        f"- Full / current partial fills: `{execution['full_fills']} / {execution['partial_fills_current']}`",
        f"- Unresolved orders: `{execution['unresolved_orders']}`",
        f"- Completed round trips: `{execution['completed_round_trips']}`",
        f"- Maximum concurrent positions: `{execution['maximum_concurrent_positions']}`",
        f"- Maximum exposure: `${execution['maximum_exposure_usd']}`",
        f"- Minimum attributable cash reserve: `${execution['minimum_attributable_cash_reserve_usd']}`",
        f"- Net P&L: `{execution['net_pnl_usd']}` ({execution['net_pnl_basis']})",
        f"- Maximum drawdown before modelled fees: `{execution['maximum_drawdown_fraction_before_modelled_fees']}`",
        f"- Latency / risk rejections: `{execution['latency_rejection_count']} / {execution['risk_rejection_count']}`",
        f"- Reconciliation discrepancies: `{execution['reconciliation_discrepancies']}`",
        "",
        "## Safety halts",
        "",
    ]
    halts = list(summary.get("safety_halts", []))
    markdown.extend([f"- {halt}" for halt in halts] or ["- None"])
    markdown.extend(
        [
            "",
            "## Evidence and limitations",
            "",
            f"- Persisted run events: `{len(events)}`",
            f"- Registered strategy versions: `{metrics['registered_strategy_versions']}`",
            f"- Event types: `{event_counts}`",
            f"- Fee evidence: `{execution['fee_evidence_status']}`",
            *[f"- Limitation: {item}" for item in execution["limitations"]],
            f"- JSON report: `{json_path.name}`",
            "",
            "Paper-only runtime. Live trading is prohibited.",
        ]
    )
    markdown_path = report_directory / f"{run_id}.md"
    markdown_path.write_text("\n".join(markdown) + "\n", encoding="utf-8")
    return json_path
