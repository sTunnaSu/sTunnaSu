"""Agent tool for Phase 8-gated Alpaca paper orders."""

from __future__ import annotations

import json
from typing import Any

from src.agent.tools import BaseTool
from src.latency_budgeter.configuration.models import CostAssumptions, LatencyBudgetConfig
from src.security.secret_redaction import redact_sensitive_text
from src.trading.phase8_paper import (
    PAPER_PROFILE_ID,
    Phase8PaperOrderRequest,
    execute_phase8_alpaca_paper_order,
    reconcile_phase8_alpaca_paper_order,
)


class TradingPhase8PaperOrderTool(BaseTool):
    """Evaluate one opportunity without direct broker mutation."""

    name = "trading_phase8_paper_order"
    description = (
        "Run a dry Phase 8 latency/decay diagnostic for one Alpaca PAPER crypto "
        "opportunity. Direct submission is disabled; paper orders must use the "
        "complete src.phase8_runtime composition root."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "enum": [PAPER_PROFILE_ID],
                "default": PAPER_PROFILE_ID,
            },
            "symbol": {"type": "string", "description": "Alpaca crypto pair, e.g. BTC/USD."},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "exclusiveMinimum": 0},
            "gross_edge_bps": {
                "type": "number",
                "minimum": 0,
                "description": "Point-in-time strategy gross edge estimate in basis points.",
            },
            "strategy_version": {"type": "string"},
            "strategy_requirements_met": {"type": "boolean"},
            "submit": {
                "type": "boolean",
                "enum": [False],
                "default": False,
                "description": "Direct mutation is disabled; only false is accepted.",
            },
            "order_type": {"type": "string", "enum": ["market", "limit"], "default": "market"},
            "limit_price": {"type": "number", "exclusiveMinimum": 0},
            "time_in_force": {"type": "string", "enum": ["gtc", "ioc"], "default": "gtc"},
            "signal_key": {"type": "string", "default": "default"},
            "fee_bps": {"type": "number", "minimum": 0, "default": 25},
            "slippage_bps": {"type": "number", "minimum": 0, "default": 5},
            "impact_bps": {"type": "number", "minimum": 0, "default": 1},
            "required_buffer_bps": {"type": "number", "minimum": 0, "default": 3},
            "freshness_limit_ms": {"type": "integer", "minimum": 1, "default": 5000},
            "tau_ms": {"type": "integer", "minimum": 1, "default": 30000},
            "fallback_p90_latency_ms": {"type": "integer", "minimum": 1, "default": 1000},
            "poll_timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30, "default": 8},
        },
        "required": [
            "symbol",
            "side",
            "quantity",
            "gross_edge_bps",
            "strategy_version",
            "strategy_requirements_met",
        ],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> str:
        try:
            connection = str(kwargs.get("connection") or PAPER_PROFILE_ID).strip()
            if connection != PAPER_PROFILE_ID:
                raise ValueError(f"only {PAPER_PROFILE_ID} is accepted")
            if bool(kwargs.get("submit", False)):
                raise ValueError(
                    "direct Phase 8 paper submission is disabled; use "
                    "python -m src.phase8_runtime with reviewed release evidence"
                )
            config = LatencyBudgetConfig(
                enabled=True,
                tau_ms=int(kwargs.get("tau_ms", 30_000)),
                freshness_limit_ms=int(kwargs.get("freshness_limit_ms", 5_000)),
                minimum_prior_samples=20,
                rolling_history_window=100,
                cold_start_policy="fallback_p90",
                fallback_p90_latency_ms=int(kwargs.get("fallback_p90_latency_ms", 1_000)),
                required_buffer_bps=float(kwargs.get("required_buffer_bps", 3)),
                cost_assumptions=CostAssumptions(
                    maker_fee_bps=float(kwargs.get("fee_bps", 25)),
                    taker_fee_bps=float(kwargs.get("fee_bps", 25)),
                    spread_bps=0,
                    slippage_bps=float(kwargs.get("slippage_bps", 5)),
                    impact_bps=float(kwargs.get("impact_bps", 1)),
                ),
            )
            request = Phase8PaperOrderRequest(
                symbol=str(kwargs["symbol"]),
                side=str(kwargs["side"]),
                quantity=kwargs["quantity"],
                gross_edge_bps=kwargs["gross_edge_bps"],
                strategy_version=str(kwargs["strategy_version"]),
                strategy_requirements_met=bool(kwargs["strategy_requirements_met"]),
                submit=False,
                order_type=str(kwargs.get("order_type", "market")),
                limit_price=kwargs.get("limit_price"),
                time_in_force=str(kwargs.get("time_in_force", "gtc")),
                signal_key=str(kwargs.get("signal_key", "default")),
                signal_metadata={"agent_tool": self.name},
                poll_timeout_seconds=float(kwargs.get("poll_timeout_seconds", 8)),
            )
            return json.dumps(
                execute_phase8_alpaca_paper_order(request, config=config),
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "status": "error",
                    "error": redact_sensitive_text(exc),
                    "paper_only": True,
                    "order_submitted": None,
                    "order_submission_state": "unknown_due_to_unhandled_error",
                },
                ensure_ascii=False,
            )


class TradingPhase8PaperReconcileTool(BaseTool):
    """Resume ledger reconciliation for an already-submitted paper order."""

    name = "trading_phase8_paper_reconcile"
    description = (
        "Read Alpaca PAPER order state and resume Phase 8 lifecycle persistence for an "
        "existing decision. This never submits or resubmits an order."
    )
    is_readonly = False
    repeatable = True
    parameters = {
        "type": "object",
        "properties": {"decision_id": {"type": "string"}},
        "required": ["decision_id"],
        "additionalProperties": False,
    }

    def execute(self, **kwargs: Any) -> str:
        try:
            result = reconcile_phase8_alpaca_paper_order(str(kwargs["decision_id"]))
        except Exception as exc:  # noqa: BLE001
            result = {
                "status": "error",
                "error": redact_sensitive_text(exc),
                "paper_only": True,
                "order_submitted": None,
                "order_submission_state": "unknown_due_to_unhandled_error",
            }
        return json.dumps(result, ensure_ascii=False)
