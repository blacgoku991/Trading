"""Stratégies : règles de S1 et test anti look-ahead automatique."""

from datetime import time

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest.lookahead import lookahead_violations
from goldbot.broker.fake_broker import make_bars
from goldbot.data.history import bars_frame
from goldbot.indicators.core import daily_atr_per_bar
from goldbot.strategies.asian_breakout import AsianBreakout, AsianBreakoutParams
from goldbot.strategies.base import LONG, SHORT, STOP, OrderIntent
from tests.conftest import open_minutes

LENIENT = AsianBreakoutParams(min_range_atr=0.0, max_range_atr=100.0)


@pytest.fixture(scope="module")
def month_of_bars(schedule_module, rule_module):
    # 5 semaines : l'ATR journalier sur 14 jours est disponible à partir de la 3e semaine.
    return bars_frame(make_bars(open_minutes(schedule_module, "2026-01-05", "2026-02-07"), seed=3), rule_module)


@pytest.fixture(scope="module")
def schedule_module():
    from goldbot.config import load_settings
    from goldbot.data.market_hours import MarketSchedule
    from tests.conftest import CONFIG_PATH

    return MarketSchedule.from_config(load_settings(CONFIG_PATH).market_hours)


@pytest.fixture(scope="module")
def rule_module():
    from goldbot.config import load_settings
    from goldbot.data.timezones import ServerTimeRule
    from tests.conftest import CONFIG_PATH

    return ServerTimeRule.from_config(load_settings(CONFIG_PATH).server_time)


def test_s1_places_one_stop_order_per_side_around_the_night_range(month_of_bars):
    bars = month_of_bars
    intents = AsianBreakout(LENIENT).intents(bars)
    day = pd.Timestamp("2026-01-28")  # mercredi, hiver : Londres = UTC
    todays = [i for i in intents if i.tag.startswith("S1-2026-01-28")]
    assert [i.side for i in todays] == [LONG, SHORT]
    assert all(i.kind == STOP for i in todays)

    london = bars["time"].dt.tz_convert("Europe/London")
    start, end = day.tz_localize("Europe/London"), (day + pd.Timedelta(hours=7)).tz_localize("Europe/London")
    window = bars[(london >= start) & (london < end)]
    high, low = window["high"].max(), window["low"].min()
    atr = daily_atr_per_bar(bars)[window.index[-1]]
    buy, sell = todays
    assert buy.time == pd.Timestamp("2026-01-28 06:59", tz="UTC")  # clôture de la barre de 06:59 = fin du range
    assert buy.price == pytest.approx(round(high + 0.05 * atr, 2))
    assert buy.sl == pytest.approx(round(low, 2)) and sell.sl == pytest.approx(round(high, 2))
    assert buy.tp == pytest.approx(round(buy.price + 1.5 * (buy.price - buy.sl), 2), abs=0.011)
    assert buy.expires_at == pd.Timestamp("2026-01-28 11:00", tz="UTC")
    assert buy.exit_at == pd.Timestamp("2026-01-28 20:00", tz="UTC")


def test_s1_uses_london_time_through_daylight_saving(schedule_module, rule_module):
    # Fin mars : Londres passe en heure d'été le 29/03/2026 ; le range finit alors à 06:00 UTC.
    bars = bars_frame(make_bars(open_minutes(schedule_module, "2026-03-02", "2026-04-04"), seed=5), rule_module)
    intents = AsianBreakout(LENIENT).intents(bars)
    times = {i.tag[3:13]: i.time for i in intents}
    assert times["2026-03-27"] == pd.Timestamp("2026-03-27 06:59", tz="UTC")
    assert times["2026-03-30"] == pd.Timestamp("2026-03-30 05:59", tz="UTC")


def test_s1_range_filter(month_of_bars):
    none_pass = AsianBreakoutParams(min_range_atr=50.0, max_range_atr=100.0)
    assert AsianBreakout(none_pass).intents(month_of_bars) == []


def test_s1_never_looks_ahead(month_of_bars):
    bars = month_of_bars
    london = bars["time"].dt.tz_convert("Europe/London")
    minutes = london.dt.hour * 60 + london.dt.minute
    # Coupes pièges : juste avant et juste après la fin du range, en pleine fenêtre d'entrée, et au hasard.
    near_end = list(np.flatnonzero(minutes.isin([418, 419, 420, 421]).to_numpy())[-12:] + 1)
    rng = np.random.default_rng(0)
    cuts = near_end + list(rng.integers(len(bars) // 2, len(bars), 15))
    assert lookahead_violations(AsianBreakout(LENIENT), bars, cuts) == []


def test_lookahead_check_catches_a_cheating_strategy(month_of_bars):
    class Cheater(AsianBreakout):
        def intents(self, bars):
            # Triche : utilise le plus haut de TOUT l'historique fourni.
            best = float(bars["high"].max())
            return [
                OrderIntent(
                    time=bars["time"].iloc[100],
                    side=LONG,
                    kind=STOP,
                    price=best,
                    sl=best - 5,
                    tp=best + 5,
                    expires_at=bars["time"].iloc[200],
                    exit_at=None,
                    tag="triche",
                    reason="triche",
                )
            ]

    assert lookahead_violations(Cheater(), month_of_bars, [500, 5000])


def test_intent_rejects_a_stop_loss_on_the_wrong_side():
    stamp = pd.Timestamp("2026-01-06 10:00", tz="UTC")
    common = dict(time=stamp, kind=STOP, expires_at=stamp, exit_at=None, tag="x", reason="x", tp=None)
    with pytest.raises(ValueError, match="SL"):
        OrderIntent(side=LONG, price=2000.0, sl=2001.0, **common)
    with pytest.raises(ValueError, match="SL"):
        OrderIntent(side=SHORT, price=2000.0, sl=1999.0, **common)


def test_daily_atr_only_uses_previous_days(month_of_bars):
    bars = month_of_bars
    atr = daily_atr_per_bar(bars)
    day = bars["time_server"].to_numpy() // 86_400
    for value in np.unique(day)[15:20]:
        in_day = np.flatnonzero(day == value)
        assert np.all(atr[in_day] == atr[in_day[0]])  # constant sur la journée : connu dès l'ouverture
    truncated = daily_atr_per_bar(bars.iloc[: len(bars) // 2])
    assert np.allclose(truncated[-100:], atr[len(bars) // 2 - 100 : len(bars) // 2], equal_nan=True)


def test_time_parameters_are_london_clock_times():
    params = AsianBreakoutParams()
    assert (params.range_start, params.range_end) == (time(0, 0), time(7, 0))
