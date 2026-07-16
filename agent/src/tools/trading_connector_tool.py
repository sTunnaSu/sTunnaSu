"""Connector-first trading tools.

Tools take an optional ``connection`` profile id. If omitted, they use the
selected profile from ``~/.vibe-trading/trading-connections.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from src.agent.tools import BaseTool
from src.trading.profiles import (
    list_profiles,
    load_selected_profile_id,
    profile_by_id,
    save_selected_profile_id,
)
from src.trading.service import (
    cancel_order,
    check_connection,
    get_account,
    get_assets,
    get_history,
    get_open_orders,
    get_positions,
    get_quote,
    place_order,
)


def _json_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


def _connection(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _num_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _symbol_key(value: Any) -> str:
    """Return a separator-insensitive key for broker symbol comparisons."""
    return "".join(char for char in str(value or "").upper() if char.isalnum())


def _payload_ok(payload: Any) -> bool:
    """Return whether a connector response is a successful JSON object."""
    return isinstance(payload, dict) and payload.get("status") == "ok"


def _quote_age_seconds(value: Any) -> float | None:
    """Parse a broker quote timestamp and return its non-negative UTC age."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())


TRADING_COMMON_PARAMETERS = {
    "connection": {
        "type": "string",
        "description": "Trading connector profile id, e.g. ibkr-paper-local or robinhood-live-mcp. Defaults to the selected profile.",
    },
    "host": {
        "type": "string",
        "description": "Optional local TWS/Gateway host override for local profiles.",
    },
    "port": {
        "type": "integer",
        "description": "Optional local TWS/Gateway port override for local profiles.",
    },
    "client_id": {
        "type": "integer",
        "description": "Optional local TWS/Gateway client id override for local profiles.",
    },
    "account": {
        "type": "string",
        "description": "Optional account code filter when supported by the connector.",
    },
}


def _overrides(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": _connection(kwargs.get("host")),
        "port": _int_or_none(kwargs.get("port")),
        "client_id": _int_or_none(kwargs.get("client_id")),
        "account": _connection(kwargs.get("account")),
    }


class TradingConnectionsTool(BaseTool):
    """List available trading connector profiles."""

    name = "trading_connections"
    description = (
        "List selectable trading connector profiles. Connectors come first; paper/live is a profile attribute."
    )
    parameters = {"type": "object", "properties": {}, "required": []}
    repeatable = True
    is_readonly = True

    def execute(self, **_: Any) -> str:
        """List connector profiles and mark the selected one."""
        try:
            selected = load_selected_profile_id()
            return _json_result(
                {
                    "status": "ok",
                    "selected_profile": selected,
                    "profiles": [profile.to_dict(selected=profile.id == selected) for profile in list_profiles()],
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingSelectConnectionTool(BaseTool):
    """Select the default trading connector profile."""

    name = "trading_select_connection"
    description = "Select the default trading connector profile for subsequent trading_* tool calls."
    parameters = {
        "type": "object",
        "properties": {
            "connection": {
                "type": "string",
                "description": "Profile id to select, e.g. ibkr-paper-local.",
            }
        },
        "required": ["connection"],
    }
    repeatable = True
    is_readonly = False

    def execute(self, **kwargs: Any) -> str:
        """Persist the selected profile id."""
        try:
            profile = profile_by_id(str(kwargs["connection"]).strip())
            path = save_selected_profile_id(profile.id)
            return _json_result({"status": "ok", "selected_profile": profile.id, "path": str(path)})
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingCheckTool(BaseTool):
    """Check a trading connector profile."""

    name = "trading_check"
    description = "Check whether a trading connector profile is configured and reachable. This never places orders."
    parameters = {
        "type": "object",
        "properties": TRADING_COMMON_PARAMETERS,
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Check connector readiness."""
        try:
            return _json_result(check_connection(_connection(kwargs.get("connection")), **_overrides(kwargs)))
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingAccountTool(BaseTool):
    """Read account summary from a trading connector profile."""

    name = "trading_account"
    description = "Read account summary from the selected trading connector profile. Read-only."
    parameters = TradingCheckTool.parameters
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read account summary."""
        try:
            return _json_result(get_account(_connection(kwargs.get("connection")), **_overrides(kwargs)))
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingAssetsTool(BaseTool):
    """Discover broker-supported assets and precision rules."""

    name = "trading_assets"
    description = (
        "List supported assets and broker trading constraints. Alpaca supports "
        "crypto discovery including canonical pair symbols, tradability, "
        "fractionability, minimum order size, and increments. Read-only."
    )
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "asset_class": {
                "type": "string",
                "enum": ["crypto", "us_equity"],
                "default": "crypto",
            },
            "tradable_only": {"type": "boolean", "default": True},
            "symbol": {
                "type": "string",
                "description": "Optional exact asset lookup, e.g. BTC/USD.",
            },
            "quote_currency": {
                "type": "string",
                "description": "Optional crypto quote currency filter, e.g. USD or USDC.",
            },
            "limit": {"type": "integer", "description": "Optional maximum rows."},
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """List assets through the selected connector profile."""
        try:
            return _json_result(
                get_assets(
                    _connection(kwargs.get("connection")),
                    asset_class=str(kwargs.get("asset_class") or "crypto"),
                    tradable_only=bool(kwargs.get("tradable_only", True)),
                    symbol=_connection(kwargs.get("symbol")),
                    quote_currency=_connection(kwargs.get("quote_currency")),
                    limit=_int_or_none(kwargs.get("limit")),
                    **_overrides(kwargs),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingPaperPreflightTool(BaseTool):
    """Return one compact, read-only Alpaca paper order preflight snapshot."""

    name = "trading_paper_preflight"
    description = (
        "Run a compact read-only preflight for one symbol on an Alpaca paper "
        "profile. It combines exact asset eligibility, a fresh quote, account "
        "state, matching open orders, and matching positions in one result. "
        "Live profiles are rejected before any broker request."
    )
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "symbol": {"type": "string", "description": "Exact symbol, e.g. BTC/USD or AAPL."},
            "asset_class": {
                "type": "string",
                "enum": ["crypto", "us_equity"],
                "default": "crypto",
            },
            "max_quote_age_seconds": {
                "type": "number",
                "default": 120,
                "description": "Reject quotes older than this many seconds.",
            },
            "max_spread_bps": {
                "type": "number",
                "default": 100,
                "description": "Reject quotes wider than this bid-ask spread in basis points.",
            },
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read all paper-order prerequisites without mutating broker state."""
        try:
            connection = _connection(kwargs.get("connection"))
            profile = profile_by_id(connection)
            if profile.connector != "alpaca" or profile.environment != "paper":
                return _json_result(
                    {
                        "status": "error",
                        "ready": False,
                        "error": "trading_paper_preflight only permits an Alpaca paper profile",
                        "profile_id": profile.id,
                        "environment": profile.environment,
                    }
                )

            symbol = str(kwargs["symbol"]).strip().upper()
            asset_class = str(kwargs.get("asset_class") or "crypto")
            overrides = _overrides(kwargs)
            assets_result = get_assets(
                profile.id,
                asset_class=asset_class,
                tradable_only=False,
                symbol=symbol,
                limit=1,
                **overrides,
            )
            quote_result = get_quote(
                symbol,
                profile.id,
                exchange="CRYPTO" if asset_class == "crypto" else "SMART",
                currency="USD",
                sec_type="CRYPTO" if asset_class == "crypto" else "STK",
                **overrides,
            )
            account_result = get_account(profile.id, **overrides)
            orders_result = get_open_orders(profile.id, include_executions=False, **overrides)
            positions_result = get_positions(profile.id, **overrides)

            assets = assets_result.get("assets", []) if _payload_ok(assets_result) else []
            asset = assets[0] if assets else {}
            canonical_symbol = str(asset.get("symbol") or symbol)
            symbol_key = _symbol_key(canonical_symbol)
            quote = quote_result.get("quote", {}) if _payload_ok(quote_result) else {}
            bid = float(quote.get("bid") or 0)
            ask = float(quote.get("ask") or 0)
            quote_age = _quote_age_seconds(quote.get("time"))
            midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
            spread_bps = ((ask - bid) / midpoint * 10_000) if midpoint else None
            max_quote_age = float(kwargs.get("max_quote_age_seconds") or 120)
            max_spread_bps = float(kwargs.get("max_spread_bps") or 100)
            account = account_result.get("account", {}) if _payload_ok(account_result) else {}
            open_orders = [
                order
                for order in orders_result.get("open_orders", [])
                if _symbol_key(order.get("symbol")) == symbol_key
            ] if _payload_ok(orders_result) else []
            positions = [
                position
                for position in positions_result.get("positions", [])
                if _symbol_key(position.get("symbol")) == symbol_key
                and abs(float(position.get("quantity") or position.get("qty") or 0)) > 0
            ] if _payload_ok(positions_result) else []

            blockers: list[str] = []
            for name, result in (
                ("assets", assets_result),
                ("quote", quote_result),
                ("account", account_result),
                ("orders", orders_result),
                ("positions", positions_result),
            ):
                if not _payload_ok(result):
                    blockers.append(f"{name}_read_failed")
            if not asset:
                blockers.append("asset_not_found")
            elif not asset.get("tradable") or not asset.get("paper_eligible", True):
                blockers.append("asset_not_paper_tradable")
            if bid <= 0 or ask <= 0:
                blockers.append("two_sided_quote_unavailable")
            elif ask < bid:
                blockers.append("quote_crossed")
            elif spread_bps is not None and spread_bps > max_spread_bps:
                blockers.append("quote_spread_too_wide")
            if quote_age is None:
                blockers.append("quote_timestamp_invalid")
            elif quote_age > max_quote_age:
                blockers.append("quote_stale")
            account_status = str(account.get("status") or "").split(".")[-1].upper()
            if account_status != "ACTIVE" or bool(account.get("trading_blocked")):
                blockers.append("account_not_trade_ready")
            if open_orders:
                blockers.append("matching_open_order_exists")
            if positions:
                blockers.append("matching_position_exists")

            return _json_result(
                {
                    "status": "ok",
                    "ready": not blockers,
                    "profile_id": profile.id,
                    "environment": "paper",
                    "is_paper": True,
                    "symbol": canonical_symbol,
                    "asset": {
                        key: asset.get(key)
                        for key in (
                            "tradable",
                            "fractionable",
                            "min_order_size",
                            "min_trade_increment",
                            "price_increment",
                            "paper_eligible",
                        )
                    },
                    "quote": {
                        key: quote.get(key)
                        for key in ("bid", "ask", "bid_size", "ask_size", "time")
                    },
                    "quote_age_seconds": round(quote_age, 3) if quote_age is not None else None,
                    "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
                    "account": {
                        "status": account_status,
                        "currency": account.get("currency"),
                        "cash": account.get("cash"),
                        "buying_power": account.get("buying_power"),
                        "trading_blocked": account.get("trading_blocked"),
                    },
                    "matching_open_order_count": len(open_orders),
                    "matching_position_count": len(positions),
                    "matching_open_orders": [
                        {
                            key: order.get(key)
                            for key in (
                                "order_id",
                                "side",
                                "status",
                                "quantity",
                                "filled_qty",
                                "notional",
                                "limit_price",
                            )
                        }
                        for order in open_orders
                    ],
                    "matching_positions": [
                        {
                            key: position.get(key)
                            for key in (
                                "side",
                                "quantity",
                                "quantity_available",
                                "average_cost",
                                "market_value",
                                "current_price",
                                "unrealized_pnl",
                            )
                        }
                        for position in positions
                    ],
                    "blockers": blockers,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "ready": False, "error": str(exc)})


class TradingPaperCryptoSnapshotTool(BaseTool):
    """Return compact paper preflight and recent-bar statistics for crypto candidates."""

    name = "trading_paper_crypto_snapshot"
    description = (
        "Build one compact, read-only Alpaca paper crypto research snapshot. "
        "It sequentially combines exact preflight data and recent bar statistics "
        "for at most two symbols, avoiding parallel SDK imports and cleared tool results."
    )
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "symbols": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 2,
                "default": ["BTC/USD", "ETH/USD"],
            },
            "period": {"type": "string", "default": "1m"},
            "limit": {"type": "integer", "default": 20},
            "max_quote_age_seconds": {"type": "number", "default": 30},
            "max_spread_bps": {"type": "number", "default": 20},
        },
        "required": [],
        "additionalProperties": False,
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read candidates sequentially and emit only decision-relevant statistics."""
        try:
            connection = _connection(kwargs.get("connection"))
            profile = profile_by_id(connection)
            if profile.connector != "alpaca" or profile.environment != "paper":
                return _json_result(
                    {
                        "status": "error",
                        "error": "trading_paper_crypto_snapshot only permits an Alpaca paper profile",
                        "profile_id": profile.id,
                        "environment": profile.environment,
                    }
                )

            raw_symbols = kwargs.get("symbols") or ["BTC/USD", "ETH/USD"]
            symbols = [str(symbol).strip().upper() for symbol in raw_symbols if str(symbol).strip()]
            if not symbols or len(symbols) > 2:
                return _json_result({"status": "error", "error": "symbols must contain one or two pairs"})

            period = str(kwargs.get("period") or "1m")
            limit = min(100, max(2, int(kwargs.get("limit") or 20)))
            max_quote_age = float(kwargs.get("max_quote_age_seconds") or 30)
            max_spread = float(kwargs.get("max_spread_bps") or 20)
            overrides = _overrides(kwargs)
            candidates: list[dict[str, Any]] = []

            for symbol in symbols:
                preflight = json.loads(
                    TradingPaperPreflightTool().execute(
                        connection=profile.id,
                        symbol=symbol,
                        asset_class="crypto",
                        max_quote_age_seconds=max_quote_age,
                        max_spread_bps=max_spread,
                        **overrides,
                    )
                )
                history = get_history(
                    symbol,
                    profile.id,
                    exchange="CRYPTO",
                    currency="USD",
                    sec_type="CRYPTO",
                    period=period,
                    limit=limit,
                    **overrides,
                )
                bars = history.get("bars", []) if _payload_ok(history) else []
                closes = [float(bar["close"]) for bar in bars if bar.get("close") not in (None, "")]
                highs = [float(bar["high"]) for bar in bars if bar.get("high") not in (None, "")]
                lows = [float(bar["low"]) for bar in bars if bar.get("low") not in (None, "")]

                def return_pct(lookback: int) -> float | None:
                    if len(closes) <= lookback or closes[-lookback - 1] == 0:
                        return None
                    return (closes[-1] / closes[-lookback - 1] - 1) * 100

                range_pct = None
                if closes and highs and lows and min(lows) > 0:
                    range_pct = (max(highs) / min(lows) - 1) * 100
                candidates.append(
                    {
                        "symbol": preflight.get("symbol", symbol),
                        "ready": bool(preflight.get("ready")),
                        "blockers": preflight.get("blockers", []),
                        "quote": preflight.get("quote", {}),
                        "quote_age_seconds": preflight.get("quote_age_seconds"),
                        "spread_bps": preflight.get("spread_bps"),
                        "asset": preflight.get("asset", {}),
                        "bar_status": history.get("status"),
                        "bar_count": len(bars),
                        "latest_close": closes[-1] if closes else None,
                        "return_1_bar_pct": round(return_pct(1), 6) if return_pct(1) is not None else None,
                        "return_5_bar_pct": round(return_pct(5), 6) if return_pct(5) is not None else None,
                        "observed_range_pct": round(range_pct, 6) if range_pct is not None else None,
                    }
                )

            return _json_result(
                {
                    "status": "ok",
                    "profile_id": profile.id,
                    "environment": "paper",
                    "is_paper": True,
                    "period": period,
                    "requested_bars": limit,
                    "ready_count": sum(bool(candidate["ready"]) for candidate in candidates),
                    "candidates": candidates,
                }
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingPositionsTool(BaseTool):
    """Read positions from a trading connector profile."""

    name = "trading_positions"
    description = "Read positions from the selected trading connector profile. Read-only."
    parameters = TradingCheckTool.parameters
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read positions."""
        try:
            return _json_result(get_positions(_connection(kwargs.get("connection")), **_overrides(kwargs)))
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingOrdersTool(BaseTool):
    """Read open orders from a trading connector profile."""

    name = "trading_orders"
    description = "Read open orders from the selected trading connector profile. Read-only."
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "include_executions": {"type": "boolean", "default": False},
        },
        "required": [],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read open orders."""
        try:
            return _json_result(
                get_open_orders(
                    _connection(kwargs.get("connection")),
                    include_executions=bool(kwargs.get("include_executions", False)),
                    **_overrides(kwargs),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingQuoteTool(BaseTool):
    """Read a quote from a trading connector profile."""

    name = "trading_quote"
    description = "Read a quote snapshot from the selected trading connector profile. Read-only."
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "symbol": {"type": "string", "description": "Symbol, e.g. AAPL or BTC/USD"},
            "exchange": {"type": "string", "default": "SMART"},
            "currency": {"type": "string", "default": "USD"},
            "sec_type": {"type": "string", "default": "STK"},
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read quote snapshot."""
        try:
            return _json_result(
                get_quote(
                    str(kwargs["symbol"]),
                    _connection(kwargs.get("connection")),
                    exchange=str(kwargs.get("exchange") or "SMART"),
                    currency=str(kwargs.get("currency") or "USD"),
                    sec_type=str(kwargs.get("sec_type") or "STK"),
                    **_overrides(kwargs),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingHistoryTool(BaseTool):
    """Read historical bars from a trading connector profile."""

    name = "trading_history"
    description = "Read historical bars from the selected trading connector profile. Read-only."
    parameters = {
        "type": "object",
        "properties": {
            **TradingQuoteTool.parameters["properties"],
            "duration": {"type": "string", "default": "30 D", "description": "IBKR (local_tws) duration string."},
            "bar_size": {"type": "string", "default": "1 day", "description": "IBKR (local_tws) bar size."},
            "what_to_show": {"type": "string", "default": "TRADES"},
            "use_rth": {"type": "boolean", "default": True},
            "period": {
                "type": "string",
                "default": "1d",
                "description": "Bar interval for SDK connectors (broker_sdk): 1m/5m/15m/30m/1h/4h/1d/1w/1M.",
            },
            "limit": {"type": "integer", "default": 90, "description": "Number of bars for SDK connectors."},
        },
        "required": ["symbol"],
    }
    repeatable = True
    is_readonly = True

    def execute(self, **kwargs: Any) -> str:
        """Read historical bars."""
        try:
            return _json_result(
                get_history(
                    str(kwargs["symbol"]),
                    _connection(kwargs.get("connection")),
                    exchange=str(kwargs.get("exchange") or "SMART"),
                    currency=str(kwargs.get("currency") or "USD"),
                    sec_type=str(kwargs.get("sec_type") or "STK"),
                    duration=str(kwargs.get("duration") or "30 D"),
                    bar_size=str(kwargs.get("bar_size") or "1 day"),
                    what_to_show=str(kwargs.get("what_to_show") or "TRADES"),
                    use_rth=bool(kwargs.get("use_rth", True)),
                    period=str(kwargs.get("period") or "1d"),
                    limit=int(kwargs.get("limit") or 90),
                    **_overrides(kwargs),
                )
            )
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingPlaceOrderTool(BaseTool):
    """Place an order through a trading connector profile.

    Paper profiles place against the broker's sandbox account. Live profiles
    route through the bounded-autonomy mandate gate (mandate + kill switch +
    fail-closed pre-trade checks + audit) before any order reaches the broker.
    Distinct lifecycle orders are allowed, but an identical successful request
    is blocked from accidental replay within the current agent session.
    """

    name = "trading_place_order"
    description = (
        "Place an order through the selected trading connector profile. Paper "
        "profiles trade a sandbox account; live profiles are gated by the user's "
        "mandate and kill switch. side is 'buy' or 'sell'; give exactly one of "
        "quantity (units) or notional (account-currency amount)."
    )
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "symbol": {"type": "string", "description": "Symbol, e.g. AAPL, BTC-USDT, 700.HK, HK.00700."},
            "side": {"type": "string", "enum": ["buy", "sell"]},
            "quantity": {"type": "number", "description": "Order size in units/shares/contracts. Exactly one of quantity/notional."},
            "notional": {"type": "number", "description": "Order size as an account-currency amount. Exactly one of quantity/notional."},
            "order_type": {"type": "string", "enum": ["market", "limit"], "default": "market"},
            "limit_price": {"type": "number", "description": "Required for limit orders."},
            "time_in_force": {
                "type": "string",
                "enum": ["day", "gtc", "ioc"],
                "default": "day",
                "description": "Equities: day/gtc. Crypto: gtc/ioc.",
            },
        },
        "required": ["symbol", "side"],
    }
    repeatable = True
    is_readonly = False

    def __init__(self) -> None:
        self._successful_request_keys: set[str] = set()

    def execute(self, **kwargs: Any) -> str:
        """Place an order via the connector profile."""
        # LLMs frequently populate BOTH sizing fields, leaving the unused one at
        # 0; a zero size is never valid, so treat it as absent to preserve the
        # "exactly one of quantity/notional" contract.
        quantity = _num_or_none(kwargs.get("quantity")) or None
        notional = _num_or_none(kwargs.get("notional")) or None
        try:
            connection = _connection(kwargs.get("connection"))
            profile_id = profile_by_id(connection).id
            request_key = json.dumps(
                {
                    "profile_id": profile_id,
                    "symbol": _symbol_key(kwargs.get("symbol")),
                    "side": str(kwargs.get("side") or "").strip().lower(),
                    "quantity": quantity,
                    "notional": notional,
                    "order_type": str(kwargs.get("order_type") or "market").strip().lower(),
                    "limit_price": _num_or_none(kwargs.get("limit_price")),
                    "time_in_force": str(kwargs.get("time_in_force") or "day").strip().lower(),
                },
                sort_keys=True,
            )
            if request_key in self._successful_request_keys:
                return _json_result(
                    {
                        "status": "error",
                        "error_code": "duplicate_order_request_blocked",
                        "error": "an identical order request already succeeded in this session",
                    }
                )
            result = place_order(
                str(kwargs["symbol"]),
                profile_id,
                side=str(kwargs.get("side") or ""),
                quantity=quantity,
                notional=notional,
                order_type=str(kwargs.get("order_type") or "market"),
                limit_price=_num_or_none(kwargs.get("limit_price")),
                time_in_force=str(kwargs.get("time_in_force") or "day"),
                **_overrides(kwargs),
            )
            if result.get("status") == "ok":
                self._successful_request_keys.add(request_key)
            return _json_result(result)
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})


class TradingCancelOrderTool(BaseTool):
    """Cancel an order through a trading connector profile (risk-reducing)."""

    name = "trading_cancel_order"
    description = "Cancel an open order on the selected trading connector profile by order id."
    parameters = {
        "type": "object",
        "properties": {
            **TRADING_COMMON_PARAMETERS,
            "order_id": {"type": "string", "description": "Broker order id to cancel."},
            "symbol": {"type": "string", "description": "Symbol (required by some brokers, e.g. OKX/Binance)."},
        },
        "required": ["order_id"],
    }
    repeatable = True
    is_readonly = False

    def __init__(self) -> None:
        self._successful_cancellations: set[str] = set()

    def execute(self, **kwargs: Any) -> str:
        """Cancel an order via the connector profile."""
        try:
            connection = _connection(kwargs.get("connection"))
            profile_id = profile_by_id(connection).id
            order_id = str(kwargs["order_id"]).strip()
            cancellation_key = f"{profile_id}:{order_id}"
            if cancellation_key in self._successful_cancellations:
                return _json_result(
                    {
                        "status": "error",
                        "error_code": "duplicate_cancel_request_blocked",
                        "error": "this order was already cancelled successfully in this session",
                    }
                )
            result = cancel_order(
                order_id,
                profile_id,
                symbol=_connection(kwargs.get("symbol")),
                **_overrides(kwargs),
            )
            if result.get("status") == "ok":
                self._successful_cancellations.add(cancellation_key)
            return _json_result(result)
        except Exception as exc:  # noqa: BLE001
            return _json_result({"status": "error", "error": str(exc)})
