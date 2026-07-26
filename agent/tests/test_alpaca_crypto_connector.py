"""Regression tests for Alpaca crypto discovery, data, and paper orders."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

import pytest

from src.live.mandate.model import AssetClass, InstrumentType
from src.trading import profiles, service
from src.trading.connectors.alpaca import sdk as al
from src.tools import build_registry
from src.tools.trading_connector_tool import (
    TradingAssetsTool,
    TradingPaperCryptoSnapshotTool,
    TradingPaperPreflightTool,
)

pytestmark = pytest.mark.unit


def _paper_cfg() -> al.AlpacaConfig:
    return al.AlpacaConfig(api_key="paper-key", secret_key="paper-secret", profile="paper")


def _enable_tap(monkeypatch: pytest.MonkeyPatch, response) -> list[tuple[str, str, str | None]]:
    calls: list[tuple[str, str, str | None]] = []

    def fake_forward(target, method, body, cred_headers, **_):  # noqa: ANN001
        calls.append((target, method, body))
        payload = response(target, method, body) if callable(response) else response
        return {"ok": True, "decision": "forwarded", "status": 200, "body": json.dumps(payload), "error": None}

    monkeypatch.setattr(al.tap_forward, "tap_enabled", lambda: True)
    monkeypatch.setattr(al.tap_forward, "forward", fake_forward)
    return calls


@pytest.mark.parametrize(
    ("raw", "canonical", "is_crypto"),
    [
        ("BTC/USD", "BTC/USD", True),
        ("btcusd", "BTC/USD", True),
        ("BTC-USD", "BTC/USD", True),
        ("eth_usdc", "ETH/USDC", True),
        ("AAPL", "AAPL", False),
        ("BRK-B", "BRK-B", False),
    ],
)
def test_alpaca_symbol_normalization(raw: str, canonical: str, is_crypto: bool) -> None:
    assert al.normalize_symbol(raw) == canonical
    assert al.is_crypto_symbol(raw) is is_crypto


def test_crypto_assets_are_discoverable_with_precision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_tap(
        monkeypatch,
        [
            {
                "id": "btc-id",
                "symbol": "BTC/USD",
                "name": "Bitcoin / US Dollar",
                "status": "active",
                "class": "crypto",
                "exchange": "CRYPTO",
                "tradable": True,
                "fractionable": True,
                "min_order_size": "0.00001",
                "min_trade_increment": "0.000000001",
                "price_increment": "0.01",
                "marginable": False,
                "shortable": False,
            }
        ],
    )

    result = al.get_assets(_paper_cfg(), symbol="BTCUSD", quote_currency="USD")

    assert calls[0][1] == "GET"
    assert calls[0][0].startswith("https://paper-api.alpaca.markets/v2/assets?")
    assert "asset_class=crypto" in calls[0][0]
    assert result["count"] == 1
    asset = result["assets"][0]
    assert asset["symbol"] == "BTC/USD"
    assert asset["tradable"] is True
    assert asset["fractionable"] is True
    assert asset["min_order_size"] == "0.00001"
    assert asset["paper_eligible"] is True


def test_crypto_quote_uses_crypto_endpoint_and_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_tap(
        monkeypatch,
        {"quotes": {"BTC/USD": {"bp": 65000.0, "ap": 65010.0, "bs": 1.2, "as": 1.1, "t": "2026-07-15T06:00:00Z"}}},
    )

    result = al.get_quote("btcusd", config=_paper_cfg())

    assert calls[0][1] == "GET"
    assert "/v1beta3/crypto/us/latest/quotes?" in calls[0][0]
    assert "BTC%2FUSD" in calls[0][0]
    assert result["symbol"] == "BTC/USD"
    assert result["asset_class"] == "crypto"
    assert result["quote"]["bid"] == 65000.0
    assert result["quote"]["ask"] == 65010.0
    assert result["quote"]["time"] == "2026-07-15T06:00:00Z"


def test_alpaca_clock_snapshot_uses_paper_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_tap(
        monkeypatch,
        {
            "timestamp": "2026-07-22T18:00:00Z",
            "is_open": True,
            "next_open": "2026-07-23T13:30:00Z",
            "next_close": "2026-07-22T20:00:00Z",
        },
    )

    result = al.get_clock_snapshot(_paper_cfg())

    assert calls == [("https://paper-api.alpaca.markets/v2/clock", "GET", None)]
    assert result["status"] == "ok"
    assert result["is_paper"] is True
    assert result["timestamp"] == "2026-07-22T18:00:00Z"


def test_crypto_history_uses_crypto_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_tap(
        monkeypatch,
        {"bars": {"ETH/USD": [{"t": "2026-07-15T00:00:00Z", "o": 3000, "h": 3100, "l": 2950, "c": 3050, "v": 12.5}]}},
    )

    result = al.get_historical_bars("ETH-USD", config=_paper_cfg(), period="1h", limit=10)

    assert "/v1beta3/crypto/us/bars?" in calls[0][0]
    assert "ETH%2FUSD" in calls[0][0]
    assert "timeframe=1Hour" in calls[0][0]
    assert result["asset_class"] == "crypto"
    assert result["bars"][0]["close"] == 3050


def test_empty_positions_remain_a_structured_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_tap(monkeypatch, [])
    result = al.get_positions(_paper_cfg())
    assert result["status"] == "ok"
    assert result["positions"] == []


def test_direct_alpaca_sdk_reads_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    state_lock = Lock()
    active = 0
    max_active = 0

    class FakeClient:
        def get_all_positions(self):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.01)
            with state_lock:
                active -= 1
            return []

    monkeypatch.setattr(al.tap_forward, "tap_enabled", lambda: False)
    monkeypatch.setattr(al, "_trading_client", lambda _cfg: FakeClient())

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _index: al.get_positions(_paper_cfg()), range(4)))

    assert all(result["status"] == "ok" for result in results)
    assert max_active == 1


def test_crypto_order_requires_crypto_tif_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _enable_tap(monkeypatch, {})
    result = al.place_order(_paper_cfg(), symbol="BTCUSD", side="buy", notional=5, time_in_force="day")
    assert result["status"] == "error"
    assert "gtc" in result["error"] and "ioc" in result["error"]
    assert calls == []


def test_crypto_paper_order_normalizes_symbol_and_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    def response(_target, _method, body):  # noqa: ANN001
        request = json.loads(body)
        return {
            "id": "paper-crypto-order",
            "symbol": request["symbol"],
            "status": "accepted",
            "filled_qty": "0",
        }

    calls = _enable_tap(monkeypatch, response)
    result = al.place_order(
        _paper_cfg(),
        symbol="BTC-USD",
        side="buy",
        notional=5,
        order_type="market",
        time_in_force="gtc",
        client_order_id="dec_0123456789abcdef0123456789abcdef",
    )

    submitted = json.loads(calls[0][2] or "{}")
    assert calls[0][0] == "https://paper-api.alpaca.markets/v2/orders"
    assert submitted["symbol"] == "BTC/USD"
    assert submitted["notional"] == "5.0"
    assert submitted["time_in_force"] == "gtc"
    assert submitted["client_order_id"] == "dec_0123456789abcdef0123456789abcdef"
    assert result["status"] == "ok"
    assert result["is_paper"] is True
    assert result["asset_class"] == "crypto"
    assert result["order_id"] == "paper-crypto-order"
    assert result["client_order_id"] == "dec_0123456789abcdef0123456789abcdef"


def test_alpaca_profiles_and_agent_registry_expose_asset_discovery() -> None:
    assert "assets.read" in profiles.profile_by_id("alpaca-paper-trade").capabilities
    assert "trading_assets" in build_registry(include_shell_tools=False).tool_names
    assert "trading_paper_preflight" in build_registry(include_shell_tools=False).tool_names
    assert "trading_paper_crypto_snapshot" in build_registry(include_shell_tools=False).tool_names
    assert "asset_class" in TradingAssetsTool.parameters["properties"]
    assert "symbol" in TradingAssetsTool.parameters["properties"]
    assert "max_quote_age_seconds" in TradingPaperPreflightTool.parameters["properties"]
    assert "max_spread_bps" in TradingPaperPreflightTool.parameters["properties"]
    assert "max_quote_age_seconds" not in TradingAssetsTool.parameters["properties"]


def test_compact_crypto_snapshot_combines_preflight_and_bar_statistics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_preflight(_self, **kwargs):  # noqa: ANN001
        symbol = kwargs["symbol"]
        return json.dumps(
            {
                "status": "ok",
                "ready": True,
                "symbol": symbol,
                "blockers": [],
                "quote": {"bid": 100.0, "ask": 100.1, "time": datetime.now(timezone.utc).isoformat()},
                "quote_age_seconds": 0.1,
                "spread_bps": 9.995,
                "asset": {"tradable": True, "paper_eligible": True},
            }
        )

    def fake_history(symbol, *_args, **_kwargs):  # noqa: ANN001
        base = 100.0 if symbol == "BTC/USD" else 200.0
        return {
            "status": "ok",
            "bars": [
                {"close": base + index, "high": base + index + 0.5, "low": base + index - 0.5} for index in range(6)
            ],
        }

    monkeypatch.setattr(TradingPaperPreflightTool, "execute", fake_preflight)
    monkeypatch.setattr("src.tools.trading_connector_tool.get_history", fake_history)

    result = json.loads(
        TradingPaperCryptoSnapshotTool().execute(
            connection="alpaca-paper-trade",
            symbols=["BTC/USD", "ETH/USD"],
            period="1m",
            limit=6,
        )
    )

    assert result["status"] == "ok"
    assert result["ready_count"] == 2
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["bar_count"] == 6
    assert result["candidates"][0]["return_5_bar_pct"] == 5.0


def test_compact_paper_preflight_keeps_all_order_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_assets",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "assets": [
                {
                    "symbol": "BTC/USD",
                    "tradable": True,
                    "fractionable": True,
                    "min_order_size": 0.00001,
                    "min_trade_increment": 0.000000001,
                    "price_increment": 0.01,
                    "paper_eligible": True,
                }
            ],
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_quote",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "quote": {
                "bid": 65000.0,
                "ask": 65010.0,
                "bid_size": 1.0,
                "ask_size": 1.0,
                "time": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_account",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "account": {
                "status": "AccountStatus.ACTIVE",
                "currency": "USD",
                "cash": "100000",
                "buying_power": "400000",
                "trading_blocked": False,
            },
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_open_orders",
        lambda *_args, **_kwargs: {"status": "ok", "open_orders": []},
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_positions",
        lambda *_args, **_kwargs: {"status": "ok", "positions": []},
    )

    result = json.loads(
        TradingPaperPreflightTool().execute(
            connection="alpaca-paper-trade",
            symbol="BTC/USD",
            asset_class="crypto",
        )
    )

    assert result["status"] == "ok"
    assert result["ready"] is True
    assert result["is_paper"] is True
    assert result["symbol"] == "BTC/USD"
    assert result["matching_open_order_count"] == 0
    assert result["matching_position_count"] == 0
    assert result["blockers"] == []


def test_paper_preflight_rejects_stale_or_crossed_quote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_assets",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "assets": [{"symbol": "BTC/USD", "tradable": True, "paper_eligible": True}],
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_quote",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "quote": {"bid": 65010.0, "ask": 65000.0, "time": "2020-01-01T00:00:00Z"},
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_account",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "account": {"status": "ACTIVE", "trading_blocked": False},
        },
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_open_orders",
        lambda *_args, **_kwargs: {"status": "ok", "open_orders": []},
    )
    monkeypatch.setattr(
        "src.tools.trading_connector_tool.get_positions",
        lambda *_args, **_kwargs: {"status": "ok", "positions": []},
    )

    result = json.loads(
        TradingPaperPreflightTool().execute(
            connection="alpaca-paper-trade",
            symbol="BTC/USD",
            asset_class="crypto",
        )
    )

    assert result["ready"] is False
    assert "quote_crossed" in result["blockers"]
    assert "quote_stale" in result["blockers"]


def test_paper_preflight_rejects_live_profile_before_broker_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_read(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("live preflight must fail before broker reads")

    monkeypatch.setattr("src.tools.trading_connector_tool.get_assets", unexpected_read)
    result = json.loads(
        TradingPaperPreflightTool().execute(
            connection="alpaca-live-trade",
            symbol="BTC/USD",
            asset_class="crypto",
        )
    )

    assert result["status"] == "error"
    assert result["ready"] is False
    assert result["environment"] == "live"


def test_alpaca_crypto_order_classification() -> None:
    instrument, asset_class = service._order_classification("alpaca", "BTC/USD")
    assert instrument is InstrumentType.CRYPTO
    assert asset_class is AssetClass.CRYPTO
