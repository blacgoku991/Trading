"""Validation anti-overfitting et portefeuille construit depuis la config."""

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest.validation import best_trades_share, monte_carlo_drawdowns, rolling_windows
from goldbot.strategies.catalog import session_portfolio


def test_monte_carlo_drawdown_of_alternating_trades():
    # +1 R / -1 R en alternance, 1 % de risque : le pire ordre aligne toutes les pertes.
    r = np.array([1.0, -1.0] * 10)
    drawdowns = monte_carlo_drawdowns(r, risk_pct=1.0, runs=500, skip_share=0.0)
    assert drawdowns.max() <= -0.99  # au moins une perte d'un risque
    assert drawdowns.min() >= (0.99**10 - 1) * 100 - 1e-9  # au pire les 10 pertes d'affilée


def test_monte_carlo_is_reproducible():
    r = np.random.default_rng(1).normal(0.1, 1.0, 200)
    first = monte_carlo_drawdowns(r, risk_pct=0.5, runs=50, seed=3)
    assert np.array_equal(first, monte_carlo_drawdowns(r, risk_pct=0.5, runs=50, seed=3))


def test_rolling_windows_and_concentration():
    times = pd.to_datetime(["2025-01-10", "2025-02-10", "2025-04-10", "2025-05-10"]).tz_localize("UTC")
    trades = pd.DataFrame({"exit_time": times, "r_multiple": [1.0, -0.5, 2.0, -1.0], "pnl": [10.0, -5.0, 20.0, -10.0]})
    table = rolling_windows(trades, "QE")
    assert table["trades"].tolist() == [2, 2]
    assert table["R total"].tolist() == [0.5, 1.0]
    assert best_trades_share(trades["pnl"], 1) == pytest.approx(20.0 / 15.0 * 100)


def test_portfolio_from_settings(settings):
    portfolio = session_portfolio(settings.strategies.session_momentum)
    codes = [strategy.code for strategy in portfolio.strategies]
    assert codes == ["S8A", "S7L", "S7N"]
    asia = portfolio.strategies[0].params
    assert asia.fade and asia.zone == "Asia/Tokyo"
    assert all(not s.params.fade for s in portfolio.strategies[1:])
