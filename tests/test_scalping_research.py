"""Analyse des erreurs et apprentissage en avançant : pas de regard vers le futur, résultats en R."""

import numpy as np
import pandas as pd
import pytest

from goldbot.config import load_settings
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.scalping.backtest import Instrument
from goldbot.scalping.engine import BREAKOUT, LONG, SimTrade
from goldbot.scalping.research import bucket_report, learn, predict, signal_table, walk_forward
from tests.conftest import CONFIG_PATH, server_epoch_of

SETTINGS = load_settings(CONFIG_PATH)
CONFIG = SETTINGS.scalping


def test_sim_trade_records_its_best_and_worst_excursions():
    trade = SimTrade("t", LONG, 4000.48, 3999.82, 4001.27, 0, 120_000, 0.0)
    trade.on_tick(1_000, 4000.18, 4000.34)  # -0,30 au bid
    trade.on_tick(2_000, 4000.98, 4001.14)  # +0,50
    trade.on_tick(3_000, 4000.60, 4000.76)
    assert trade.mae == pytest.approx(-0.30) and trade.mfe == pytest.approx(0.50)


def _breakout_ticks(base_ms):
    points = [(base_ms + k * 1000, 4000.00 + 0.02 * (k % 2)) for k in range(-120, 0)]
    points += [(base_ms + 5_000 + k * 1000, 4000.40) for k in range(5)]
    points += [(base_ms + 10_000 + k * 1000, 4000.50) for k in range(200)]
    ticks = pd.DataFrame(
        {
            "time_msc_server": [t for t, _ in points],
            "bid": [round(mid - 0.08, 2) for _, mid in points],
            "ask": [round(mid + 0.08, 2) for _, mid in points],
        }
    )
    minutes = np.arange(base_ms // 1000 - 300 * 60, base_ms // 1000, 60)
    close = np.linspace(3990.0, 4000.0, len(minutes))
    bars = pd.DataFrame({"time_server": minutes, "close": close, "high": close + 0.5, "low": close - 0.5})
    return ticks, bars


def test_signal_table_simulates_each_signal_with_its_context_and_result_in_r():
    base_ms = server_epoch_of("2026-01-06 12:00") * 1000
    ticks, bars = _breakout_ticks(base_ms)
    table, refusals = signal_table(
        ticks,
        bars,
        CONFIG,
        instrument=Instrument(0.01, 100.0, 0.01, 100.0, 0.01, 0, 0),
        schedule=MarketSchedule.from_config(SETTINGS.market_hours),
        rule=ServerTimeRule.from_config(SETTINGS.server_time),
        strategies=(BREAKOUT,),
    )
    (row,) = table.to_dict("records")
    assert row["strategie"] == BREAKOUT and row["sens"] == LONG and row["sortie"] == "durée max"
    assert row["heure"] == pytest.approx(11 + 15 / 3600, abs=1 / 60)  # 12:00:15 serveur = 11:00 à Paris (hiver)
    assert row["meme_sens_ouverts"] == 0 and row["trend_aligned"] == 1.0
    assert row["stop_atr"] > 0 and 0 < row["cout_r"] < 1 and row["break_atr"] > 0
    # Prix plat après l'entrée : sortie au bid à 120 s, perte = spread, en part du stop.
    assert row["r"] == pytest.approx(-0.16 / (4000.58 - 3999.87), abs=1e-6)
    assert row["mfe_r"] <= 0 and row["mae_r"] <= 0
    assert refusals == {}


def _table(days_and_results):
    rows = []
    for day, bucket, r in days_and_results:
        rows.append({"jour": day, "x": bucket, "r": r})
    return pd.DataFrame(rows)


def test_learn_shrinks_small_buckets_toward_the_overall_mean():
    table = _table([(0, 0.5, 1.0)] * 40 + [(0, 1.5, -1.0)] * 40 + [(0, 2.5, 3.0)] * 2)
    base, effects = learn(table, {"x": [0, 1, 2, 3]}, prior=50.0)
    assert effects["x"][0] > 0 > effects["x"][1]
    assert abs(effects["x"][2]) < 3.0 - base  # deux trades seulement : fortement rétréci
    expected = predict(pd.DataFrame({"x": [0.5, 1.5]}), base, effects, {"x": [0, 1, 2, 3]})
    assert expected.iloc[0] > 0 > expected.iloc[1]


def test_walk_forward_learns_only_from_past_days():
    # Jours 0 à 4 : la tranche « 0 » gagne. Jour 5 : elle perd. Le filtre du jour 5 ne doit pas le savoir.
    past = [(day, 0.5, 1.0) for day in range(5) for _ in range(20)]
    past += [(day, 1.5, -1.0) for day in range(5) for _ in range(20)]
    future = [(5, 0.5, -1.0)] * 20 + [(5, 1.5, 1.0)] * 20
    tested = walk_forward(_table(past + future), {"x": [0, 1, 2]}, min_train_days=5)
    assert set(tested["jour"]) == {5}
    assert tested.loc[tested["x"] == 0.5, "pris"].all() and not tested.loc[tested["x"] == 1.5, "pris"].any()
    assert tested.loc[tested["pris"], "r"].mean() == -1.0  # il se trompe, comme en vrai : pas de triche


def test_bucket_report_counts_and_averages_each_bucket():
    table = _table([(0, 0.5, 1.0), (0, 0.5, -1.0), (0, 1.5, -0.5)])
    report = bucket_report(table, "x", [0, 1, 2])
    assert report["trades"].tolist() == [2, 1]
    assert report["esperance_r"].tolist() == [0.0, -0.5]
