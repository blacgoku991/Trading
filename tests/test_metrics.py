"""Statistiques de backtest sur un résultat construit à la main."""

import math

import pandas as pd
import pytest

from goldbot.backtest.engine import BacktestResult
from goldbot.backtest.metrics import breakdown, max_drawdown, monthly_returns, summarize


def _result():
    base = pd.Timestamp("2026-01-05 09:00", tz="UTC")
    rows = []
    for k, (pnl, r) in enumerate([(100.0, 2.0), (-50.0, -1.0), (-50.0, -1.0), (150.0, 3.0)]):
        entry = base + pd.Timedelta(days=k)
        rows.append(
            {
                "entry_time": entry,
                "exit_time": entry + pd.Timedelta(hours=2),
                "pnl": pnl,
                "r_multiple": r,
                "side": 1 if k % 2 == 0 else -1,
                "exit_reason": "TP" if pnl > 0 else "SL",
                "mae_r": 0.5,
                "mfe_r": 1.5,
            }
        )
    trades = pd.DataFrame(rows)
    equity_values = [10_100.0, 10_050.0, 10_000.0, 10_150.0]
    equity = pd.Series(equity_values, index=trades["exit_time"])
    daily = pd.Series(equity_values, index=trades["exit_time"] + pd.Timedelta(hours=10))
    return BacktestResult(
        trades=trades,
        equity=equity,
        daily_equity=daily,
        skipped=pd.DataFrame(columns=["time", "tag", "reason"]),
        initial_equity=10_000.0,
    )


def test_summary_figures():
    summary = summarize(_result())
    assert summary["trades"] == 4
    assert summary["net_profit"] == pytest.approx(150.0)
    assert summary["profit_factor"] == pytest.approx(250.0 / 100.0)
    assert summary["expectancy_r"] == pytest.approx(0.75)
    assert summary["win_rate_pct"] == pytest.approx(50.0)
    assert summary["avg_win_r"] == pytest.approx(2.5) and summary["avg_loss_r"] == pytest.approx(-1.0)
    # Plus haut 10 100, plus bas ensuite 10 000 : -0,99 %.
    assert summary["max_drawdown_pct"] == pytest.approx((10_000 / 10_100 - 1) * 100)
    assert summary["best_five_share_pct"] == pytest.approx(100.0)  # les 4 trades : 150 / 150


def test_max_drawdown_duration_counts_until_recovery():
    index = pd.date_range("2026-01-01", periods=5, freq="D", tz="UTC")
    equity = pd.Series([110.0, 100.0, 105.0, 111.0, 108.0], index=index)
    drawdown, duration = max_drawdown(equity, 100.0)
    assert drawdown == pytest.approx((100 / 110 - 1) * 100)
    assert duration == pd.Timedelta(days=2)  # du 01 (plus haut) au 04 (nouveau plus haut)


def test_profit_factor_without_losses_is_infinite():
    result = _result()
    result.trades = result.trades[result.trades["pnl"] > 0]
    assert math.isinf(summarize(result)["profit_factor"])


def test_monthly_returns_and_breakdowns():
    result = _result()
    table = monthly_returns(result)
    assert table.loc[2026, 1] == pytest.approx(1.5)
    tables = breakdown(result.trades, "Europe/Paris")
    assert tables["sens"].loc["achat", "trades"] == 2
    assert tables["session"].loc["Londres", "trades"] == 4  # 09:00 UTC = 09:00 à Londres en janvier
