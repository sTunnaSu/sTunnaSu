from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile

import pytest
import pandas as pd

from backtest import validation


def test_rejects_missing_run_dir_argument() -> None:
    with pytest.raises(SystemExit, match="Usage: python -m backtest.validation <run_dir>"):
        validation._parse_run_dir(["validation"])


def test_rejects_blank_run_dir() -> None:
    with pytest.raises(SystemExit, match="run_dir must be a non-empty path"):
        validation._parse_run_dir(["validation", "   "])


def test_rejects_malformed_run_dir() -> None:
    with pytest.raises(SystemExit, match="Invalid run_dir path:"):
        validation._parse_run_dir(["validation", "\0bad"])


def test_rejects_missing_directory() -> None:
    missing_dir = Path(tempfile.gettempdir()) / "validation-cli-missing-dir"

    with pytest.raises(SystemExit, match=rf"run_dir does not exist: .*{missing_dir.name}"):
        validation._parse_run_dir(["validation", str(missing_dir)])


def test_rejects_non_directory_path() -> None:
    with tempfile.NamedTemporaryFile() as handle:
        with pytest.raises(SystemExit, match=rf"run_dir is not a directory: .*{Path(handle.name).name}"):
            validation._parse_run_dir(["validation", handle.name])


def test_accepts_existing_directory() -> None:
    with tempfile.TemporaryDirectory() as run_dir:
        parsed = validation._parse_run_dir(["validation", run_dir])

    assert parsed == Path(run_dir)


def test_main_writes_strict_json_for_non_finite_results(tmp_path: Path, monkeypatch, capsys) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (tmp_path / "config.json").write_text('{"initial_cash": 1000}', encoding="utf-8")

    monkeypatch.setattr(validation, "_load_equity", lambda _run_dir: object())
    monkeypatch.setattr(validation, "_load_trades", lambda _run_dir: [])
    monkeypatch.setattr(
        validation,
        "monte_carlo_test",
        lambda *_args, **_kwargs: {"actual_sharpe": math.nan},
    )
    monkeypatch.setattr(
        validation,
        "bootstrap_sharpe_ci",
        lambda *_args, **_kwargs: {"ci": [math.inf, -math.inf, 1.0]},
    )
    monkeypatch.setattr(
        validation,
        "rolling_window_analysis",
        lambda *_args, **_kwargs: {"windows": [{"sharpe": math.nan}]},
    )

    result = validation.main(tmp_path)

    raw = (artifacts / "validation.json").read_text(encoding="utf-8")
    stdout = capsys.readouterr().out
    assert "NaN" not in raw
    assert "Infinity" not in raw
    assert "NaN" not in stdout
    assert "Infinity" not in stdout

    loaded = json.loads(raw)
    assert loaded == result
    assert loaded["monte_carlo"]["actual_sharpe"] is None
    assert loaded["bootstrap"]["ci"] == [None, None, 1.0]
    assert loaded["rolling_window"]["windows"][0]["sharpe"] is None


def test_load_trades_preserves_zero_gross_cost_only_loss(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    pd.DataFrame(
        [
            {
                "timestamp": "2025-01-01",
                "code": "TEST",
                "side": "buy",
                "price": 100.0,
                "fill_price": 100.0,
                "qty": 1.0,
                "reason": "signal",
                "pnl": 0.0,
                "gross_pnl": 0.0,
                "commission": 0.0,
                "slippage_cost": 0.0,
                "net_pnl": 0.0,
                "holding_days": 0,
                "return_pct": 0.0,
            },
            {
                "timestamp": "2025-01-02",
                "code": "TEST",
                "side": "sell",
                "price": 100.0,
                "fill_price": 100.0,
                "qty": 1.0,
                "reason": "signal",
                "pnl": 0.0,
                "gross_pnl": 0.0,
                "commission": 1.0,
                "slippage_cost": 0.5,
                "net_pnl": -1.5,
                "holding_days": 1,
                "return_pct": 0.0,
            },
        ]
    ).to_csv(artifacts / "trades.csv", index=False)

    trades = validation._load_trades(tmp_path)

    assert len(trades) == 1
    assert trades[0].gross_pnl == 0.0
    assert trades[0].net_pnl == -1.5
    assert (
        validation.monte_carlo_test(
            [trades[0], trades[0], trades[0]],
            1000,
            n_simulations=2,
        )["actual_sharpe"]
        < 0
    )
