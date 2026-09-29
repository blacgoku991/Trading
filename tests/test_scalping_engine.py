"""Moteur de scalping : bougies de 5 s, cassure confirmée, plan de trade, simulation au tick."""

import numpy as np
import pandas as pd
import pytest

from goldbot.config import load_settings
from goldbot.data.market_hours import MarketSchedule
from goldbot.scalping.backtest import Instrument, run_backtest
from goldbot.scalping.engine import (
    LONG,
    SHORT,
    BreakoutDetector,
    Candle,
    CandleBuilder,
    SimTrade,
    ema_trend,
    plan_trade,
)
from tests.conftest import CONFIG_PATH, server_epoch_of

SETTINGS = load_settings(CONFIG_PATH)
CONFIG = SETTINGS.scalping


def candle(k, close, *, high=None, low=None, spread=0.16):
    """Bougie de 5 s numéro k, prix médians."""
    high = close + 0.05 if high is None else high
    low = close - 0.05 if low is None else low
    return Candle(k * 5000, close, high, low, close, close - spread / 2, close + spread / 2, spread, 10)


def test_candles_use_the_mid_price_and_close_when_a_new_bucket_starts():
    builder = CandleBuilder(5)
    assert builder.add(1_000, 4000.00, 4000.20) == []
    assert builder.add(3_000, 4001.00, 4001.40) == []  # médian 4001.20, spread 0.40
    (done,) = builder.add(5_000, 4002.00, 4002.20)
    assert done.start_ms == 0
    assert (done.open, done.high, done.low, done.close) == (4000.10, 4001.20, 4000.10, 4001.20)
    assert done.max_spread == pytest.approx(0.40) and done.ticks == 2
    assert (done.bid, done.ask) == (4001.00, 4001.40)


def test_a_candle_ends_with_the_clock_even_without_a_new_tick():
    builder = CandleBuilder(5)
    builder.add(1_000, 4000.0, 4000.2)
    assert builder.close_until(4_999) == []
    (done,) = builder.close_until(5_000)
    assert done.start_ms == 0 and builder.pending_start is None


def _feed(detector, closes, trend=LONG, start=0):
    return [detector.on_candle(candle(start + k, c), trend) for k, c in enumerate(closes)]


def test_breakout_needs_two_closes_beyond_the_previous_60_seconds_high():
    detector = BreakoutDetector(CONFIG)
    flat = [4000.0] * 12  # 60 s de range : plus haut 4000.05
    results = _feed(detector, [*flat, 4000.30, 4000.40])
    assert results[12] is None  # première clôture au-delà : en attente de confirmation
    setup = results[13]
    assert setup is not None and setup.side == LONG and setup.level == pytest.approx(4000.05)
    assert setup.tag == f"SC-{13 * 5000}-L"


def test_close_back_inside_cancels_the_breakout():
    detector = BreakoutDetector(CONFIG)
    results = _feed(detector, [4000.0] * 12 + [4000.30, 4000.00, 4000.02])
    assert all(r is None for r in results)


def test_the_signal_candle_is_excluded_from_the_level():
    detector = BreakoutDetector(CONFIG)
    # La bougie de signal a un plus haut énorme : s'il était compté, la cassure serait impossible.
    _feed(detector, [4000.0] * 12)
    assert detector.on_candle(candle(12, 4000.30, high=4010.0), LONG) is None
    setup = detector.on_candle(candle(13, 4000.40), LONG)
    assert setup is not None and setup.level == pytest.approx(4000.05)


def test_trend_filter_blocks_trades_against_the_m1_trend():
    detector = BreakoutDetector(CONFIG)
    assert all(r is None for r in _feed(detector, [4000.0] * 12 + [4000.30, 4000.40], trend=SHORT))


def test_no_repeat_until_the_setup_rearms():
    detector = BreakoutDetector(CONFIG)
    results = _feed(detector, [4000.0] * 12 + [4000.30, 4000.40, 4000.50, 4000.60, 4000.70])
    assert sum(r is not None for r in results) == 1  # la tendance continue, mais un seul signal
    # Retour sous le niveau joué, nouveau range, nouvelle cassure : nouveau signal permis.
    results = _feed(detector, [4000.0] * 12 + [4000.30, 4000.40], start=17)
    assert results[-1] is not None


def test_not_enough_history_means_no_signal():
    detector = BreakoutDetector(CONFIG)
    assert all(r is None for r in _feed(detector, [4000.0] * 3 + [4001.0, 4001.1]))


def _setup(closes=(4000.0,) * 12 + (4000.30, 4000.40)):
    detector = BreakoutDetector(CONFIG)
    results = _feed(detector, list(closes))
    return results[-1], list(detector.history)


def test_plan_puts_the_stop_behind_recent_structure_and_targets_1_2_times_it():
    setup, recent = _setup()
    plan = plan_trade(setup, 4000.32, 4000.48, recent, CONFIG, point=0.01, stops_level_points=1, freeze_level_points=0)
    # Plus bas médian des 30 dernières secondes : 3999.95 ; stop = 3999.95 - 0.08 (demi-spread) - 0.05 = 3999.82.
    assert plan.sl == pytest.approx(3999.82)
    assert plan.entry == 4000.48 and plan.stop_distance == pytest.approx(0.66)
    assert plan.tp == pytest.approx(4000.48 + 0.79)  # 1,2 x 0,66 arrondi
    assert plan.target == pytest.approx(0.79)


def test_plan_refuses_a_target_too_small_for_the_costs():
    setup, recent = _setup()
    # Spread de 0,40 : coût estimé 0,40 + 0,10 = 0,50 ; objectif 1,2 x 0,93 = 1,12 < 3 x 0,50.
    refusal = plan_trade(setup, 4000.20, 4000.60, recent, CONFIG, point=0.01, stops_level_points=1,
                         freeze_level_points=0)  # fmt: skip
    assert isinstance(refusal, str) and "trop faible" in refusal


def test_plan_refuses_stops_too_far():
    # Creux à 3990 dans les 30 dernières secondes : le stop derrière la structure serait à plus de 10 $.
    closes = (4000.0,) * 9 + (3990.0,) + (4000.0,) * 2 + (4000.30, 4000.40)
    setup, recent = _setup(closes)
    refusal = plan_trade(setup, 4000.32, 4000.48, recent, CONFIG, point=0.01, stops_level_points=1,
                         freeze_level_points=0)  # fmt: skip
    assert isinstance(refusal, str) and "trop loin" in refusal


def test_commission_counts_in_the_cost_filter():
    setup, recent = _setup()
    kwargs = dict(point=0.01, stops_level_points=1, freeze_level_points=0)
    plan = plan_trade(setup, 4000.32, 4000.48, recent, CONFIG, **kwargs)
    assert plan.cost == pytest.approx(0.26)  # spread 0,16 + 2 x 0,05 de glissement
    # 0,07 $ par once aller-retour (7 $ par lot) : coût 0,33, objectif 0,79 < 3 x 0,33.
    refusal = plan_trade(setup, 4000.32, 4000.48, recent, CONFIG, commission_per_oz=0.07, **kwargs)
    assert isinstance(refusal, str) and "trop faible" in refusal


def test_simulated_result_deducts_the_commission_once():
    trade = SimTrade("t", LONG, 4000.48, 3999.82, 4001.27, 0, 120_000, 0.0, fee=0.07)
    assert trade.move_at(4000.58, 4000.74) == pytest.approx(0.10 - 0.07)
    assert trade.on_tick(1_000, 4001.30, 4001.46)
    assert trade.move == pytest.approx(0.79 - 0.07)


def test_simulated_long_hits_its_target_at_the_target_price():
    trade = SimTrade("t", LONG, 4000.48, 3999.82, 4001.27, 0, 120_000, 0.0)
    assert not trade.on_tick(1_000, 4001.00, 4001.16)
    assert trade.on_tick(2_000, 4001.30, 4001.46)
    assert trade.reason == "objectif" and trade.exit_price == 4001.27
    assert trade.move == pytest.approx(0.79)


def test_simulated_stop_uses_the_bid_and_gaps_make_it_worse():
    trade = SimTrade("t", LONG, 4000.48, 3999.82, 4001.27, 0, 120_000, 0.05)
    assert trade.on_tick(1_000, 3999.50, 3999.66)  # le bid saute sous le stop
    assert trade.reason == "stop" and trade.exit_price == pytest.approx(3999.45)


def test_simulated_short_stop_is_triggered_by_the_ask():
    trade = SimTrade("t", SHORT, 4000.00, 4000.50, 3999.40, 0, 120_000, 0.0)
    assert not trade.on_tick(1_000, 4000.30, 4000.46)
    assert trade.on_tick(2_000, 4000.36, 4000.52)  # l'ask saute au-dessus du stop : exécuté à 4000.52
    assert trade.reason == "stop" and trade.move == pytest.approx(-0.52)


def test_max_duration_closes_even_a_losing_trade():
    trade = SimTrade("t", LONG, 4000.48, 3999.82, 4001.27, 0, 120_000, 0.0)
    assert not trade.on_tick(60_000, 4000.20, 4000.36)
    assert trade.on_tick(120_000, 4000.10, 4000.26)
    assert trade.reason == "durée max" and trade.move == pytest.approx(-0.38)


def test_ema_trend():
    assert ema_trend([float(v) for v in range(100)], 20, 50) == 1
    assert ema_trend([float(v) for v in range(100, 0, -1)], 20, 50) == -1
    assert ema_trend([1.0] * 10, 20, 50) == 0


# --- test sur l'historique -------------------------------------------------------------------------

INSTRUMENT = Instrument(0.01, 100.0, 0.01, 100.0, 0.01, 0, 0)


def _history(base_ms):
    """Range calme de 2 minutes puis cassure confirmée, tendance M1 haussière."""
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
    bars = pd.DataFrame({"time_server": minutes, "close": np.linspace(3990.0, 4000.0, len(minutes))})
    return ticks, bars


def _replay(base_ms):
    ticks, bars = _history(base_ms)
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    return run_backtest(ticks, bars, CONFIG, instrument=INSTRUMENT, schedule=schedule, initial_equity=5700.0,
                        slippage_points=0.0)  # fmt: skip


def test_backtest_trades_the_confirmed_breakout_and_exits_at_max_duration():
    result = _replay(server_epoch_of("2026-01-06 12:00") * 1000)
    assert result.signals == 1 and len(result.trades) == 1
    (trade,) = result.trades.to_dict("records")
    assert trade["reason"] == "durée max" and trade["exit_ms"] - trade["open_ms"] >= 120_000


def test_backtest_refuses_an_entry_when_the_market_closes_before_the_max_duration():
    result = _replay(server_epoch_of("2026-01-06 23:57") * 1000 + 30_000)  # pause quotidienne à 23:59
    assert result.signals == 1 and result.trades.empty
    assert result.refusals == {"le marché ferme avant la durée max": 1}
