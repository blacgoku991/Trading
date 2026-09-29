"""Protocole de test des hypothèses : périodes, statistiques, test multiple, plateaux, journal."""

import csv
import math

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest.engine import Instrument
from goldbot.backtest.protocol import (
    PERIODS,
    Period,
    deflated_sharpe,
    expected_max_sharpe,
    grid,
    log_trials,
    plateau,
    probabilistic_sharpe,
    r_stats,
    result_row,
    run_intents,
)
from goldbot.backtest.runner import Dataset
from goldbot.strategies.base import LONG, MARKET, OrderIntent
from tests.test_backtest_engine import frame

SYMBOL = {
    "name": "XAUUSD", "point": 0.01, "trade_contract_size": 100.0, "volume_min": 0.01, "volume_max": 20.0,
    "volume_step": 0.01, "trade_stops_level": 0, "swap_mode": 1, "swap_long": -61.6, "swap_short": 40.5,
    "swap_rollover3days": 3,
}  # fmt: skip


def test_periods_are_consecutive_and_fixed():
    dev, val, final = PERIODS["developpement"], PERIODS["validation"], PERIODS["final"]
    assert dev.start == pd.Timestamp("2019-07-22", tz="UTC")
    assert dev.end == val.start == pd.Timestamp("2024-01-01", tz="UTC")
    assert val.end == final.start == pd.Timestamp("2025-10-01", tz="UTC") and final.end is None


def test_r_stats_on_a_known_sample():
    r = np.array([1.0, -1.0, 2.0, -1.0, 1.0, -0.5])
    stats = r_stats(r)
    assert stats["n"] == 6 and stats["mean_r"] == pytest.approx(0.25)
    assert stats["std_r"] == pytest.approx(np.std(r, ddof=1))
    assert stats["t_stat"] == pytest.approx(0.25 / np.std(r, ddof=1) * math.sqrt(6))
    assert stats["pf_r"] == pytest.approx(4.0 / 2.5)
    assert 0.5 < stats["psr"] < 1.0


def test_probabilistic_sharpe_grows_with_the_number_of_trades():
    assert probabilistic_sharpe(0.1, 50, 0.0, 3.0, 0.0) < probabilistic_sharpe(0.1, 500, 0.0, 3.0, 0.0)
    assert probabilistic_sharpe(0.0, 500, 0.0, 3.0, 0.0) == pytest.approx(0.5)
    # Sharpe 0,1 par trade sur 401 trades, rendements normaux : z = 0,1 x 20 / sqrt(1 + 0,5 x 0,01) ≈ 1,995.
    assert probabilistic_sharpe(0.1, 401, 0.0, 3.0, 0.0) == pytest.approx(0.977, abs=1e-3)


def test_more_trials_raise_the_bar():
    rng = np.random.default_rng(0)
    few, many = rng.normal(0, 0.05, 5), rng.normal(0, 0.05, 500)
    assert 0 < expected_max_sharpe(few) < expected_max_sharpe(many)
    r = rng.normal(0.08, 1.0, 400)
    assert deflated_sharpe(r, many) < deflated_sharpe(r, few) <= r_stats(r)["psr"]


def test_grid_and_plateau_prefer_a_stable_zone_over_an_isolated_peak():
    axes = {"a": [1, 2, 3], "b": [10, 20]}
    combos = grid(axes)
    assert len(combos) == 6 and combos[0] == {"a": 1, "b": 10} and combos[-1] == {"a": 3, "b": 20}
    values = {(1, 10): 0.30, (1, 20): -0.20, (2, 10): -0.10, (2, 20): 0.12, (3, 10): 0.10, (3, 20): 0.11}
    rows = pd.DataFrame([{**c, "exp_r": values[(c["a"], c["b"])], "trades": 100} for c in combos])
    scored = plateau(rows, axes)
    best = scored.loc[scored["voisinage"].idxmax()]
    assert (best["a"], best["b"]) == (3, 20)  # le pic isolé (1, 10) est entouré de pertes
    thin = plateau(rows.assign(trades=[100, 100, 100, 100, 100, 5]), axes, min_trades=50)
    assert thin.loc[5, "voisinage"] < scored.loc[5, "voisinage"]


def _dataset():
    # Trois jours de barres : 2023-12-29 (développement), 2024-01-02 et 2024-01-03 (validation).
    parts = [frame([(2000.0, 2000.5, 1999.5, 2000.0)] * 30, start=day) for day in
             ("2023-12-29 10:00", "2024-01-02 10:00", "2024-01-03 10:00")]  # fmt: skip
    bars = pd.concat(parts, ignore_index=True)
    return Dataset(bars, SYMBOL)


def _intent(bars, at, tag):
    time = bars["time"].iloc[at]
    return OrderIntent(time, LONG, MARKET, None, 1995.0, 2010.0, time + pd.Timedelta(minutes=2),
                       time + pd.Timedelta(minutes=10), tag, "test")  # fmt: skip


def test_run_intents_only_trades_inside_the_period(settings):
    dataset = _dataset()
    bars = dataset.bars
    intents = [_intent(bars, 2, "dev"), _intent(bars, 32, "val-1"), _intent(bars, 62, "val-2")]
    dev = run_intents(intents, dataset, PERIODS["developpement"], settings=settings)
    val = run_intents(intents, dataset, PERIODS["validation"], settings=settings)
    assert dev.trades["strategy_tag"].tolist() == ["dev"]
    assert val.trades["strategy_tag"].tolist() == ["val-1", "val-2"]
    stress = run_intents(intents, dataset, PERIODS["validation"], settings=settings, scenario="stress")
    assert (stress.trades["pnl"] < val.trades["pnl"]).all()  # spread x 1,5 et glissement doublé
    row = result_row(val, period="validation", scenario="reel")
    assert row["trades"] == 2 and row["annees_positives"] == "0/1" and row["exp_r"] < 0
    custom = Period("test", pd.Timestamp("2024-01-03", tz="UTC"), None)
    assert run_intents(intents, dataset, custom, settings=settings).trades["strategy_tag"].tolist() == ["val-2"]


def test_trial_journal_appends_every_run(tmp_path):
    path = tmp_path / "essais.csv"
    row = {"periode": "developpement", "scenario": "reel", "trades": 10, "exp_r": 0.1}
    log_trials(path, "H1", {"x": 1}, [row], stamp="t1")
    log_trials(path, "H1", {"x": 2}, [row, {**row, "scenario": "stress"}], stamp="t2")
    with path.open(encoding="utf-8") as handle:
        lines = list(csv.DictReader(handle, delimiter=";"))
    assert [line["parametres"] for line in lines] == ['{"x": 1}', '{"x": 2}', '{"x": 2}']
    assert lines[2]["scenario"] == "stress" and lines[0]["hypothese"] == "H1"


def test_instrument_from_the_test_symbol():
    assert Instrument.from_symbol(SYMBOL).contract_size == 100.0
