"""Moteur de scalping : bougies de 5 s, détecteurs (cassure, impulsion-repli), règles d'entrée, plan, simulation."""

import numpy as np
import pandas as pd
import pytest

from goldbot.config import load_settings
from goldbot.data.market_hours import MarketSchedule
from goldbot.scalping.backtest import Instrument, run_backtest
from goldbot.scalping.engine import (
    BREAKOUT,
    LONG,
    PULLBACK,
    SHORT,
    BreakoutDetector,
    Candle,
    CandleBuilder,
    PullbackDetector,
    SimTrade,
    ema_trend,
    make_detectors,
    plan_trade,
)
from goldbot.scalping.policy import Cadence, EntryPolicy, Exposure, Limits, reason_key, split_volume
from tests.conftest import CONFIG_PATH, server_epoch_of

SETTINGS = load_settings(CONFIG_PATH)
CONFIG = SETTINGS.scalping
KWARGS = dict(point=0.01, stops_level_points=1, freeze_level_points=0)


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


# --- cassure ---------------------------------------------------------------------------------------


def _feed(detector, closes, trend=LONG, start=0, atr=0.0):
    return [detector.on_candle(candle(start + k, c), trend, atr) for k, c in enumerate(closes)]


def test_breakout_needs_two_closes_beyond_the_previous_60_seconds_high():
    detector = BreakoutDetector(CONFIG)
    flat = [4000.0] * 12  # 60 s de range : plus haut 4000.05
    results = _feed(detector, [*flat, 4000.30, 4000.40])
    assert results[12] is None  # première clôture au-delà : en attente de confirmation
    setup = results[13]
    assert setup is not None and setup.side == LONG and setup.level == pytest.approx(4000.05)
    assert setup.strategy == BREAKOUT and setup.tag == f"SC-B-{13 * 5000}-L"
    assert setup.key == "B+1@4000.05" and "cassure de 4000.05 confirmée" in setup.reason
    assert setup.structure == pytest.approx(3999.95)  # plus bas des 30 dernières secondes


def test_close_back_inside_cancels_the_breakout():
    detector = BreakoutDetector(CONFIG)
    results = _feed(detector, [4000.0] * 12 + [4000.30, 4000.00, 4000.02])
    assert all(r is None for r in results)


def test_the_signal_candle_is_excluded_from_the_level():
    detector = BreakoutDetector(CONFIG)
    # La bougie de signal a un plus haut énorme : s'il était compté, la cassure serait impossible.
    _feed(detector, [4000.0] * 12)
    assert detector.on_candle(candle(12, 4000.30, high=4010.0), LONG, 0.0) is None
    setup = detector.on_candle(candle(13, 4000.40), LONG, 0.0)
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


# --- impulsion-repli -------------------------------------------------------------------------------

FLAT = [candle(k, 4000.0) for k in range(6)]
IMPULSE = [
    candle(6, 4000.40, high=4000.45, low=4000.00),
    candle(7, 4000.80, high=4000.85, low=4000.40),
    candle(8, 4001.10, high=4001.15, low=4000.80),  # 3999.95 -> 4001.15 : 1,20 $ en 20 s
]
PULLBACK_CANDLES = [
    candle(9, 4000.80, high=4001.00, low=4000.75),  # repli de 33 %
    candle(10, 4000.70, high=4000.85, low=4000.60),  # repli de 46 %, pas de reprise
]
RESUMPTION = candle(11, 4000.95, high=4001.00, low=4000.70)  # clôture au-dessus du plus haut précédent (4000.85)


def _run(detector, candles, atr=1.0):
    return [detector.on_candle(c, 0, atr) for c in candles]


def test_pullback_enters_on_the_resumption_after_an_impulse_and_a_partial_retracement():
    detector = PullbackDetector(CONFIG)
    results = _run(detector, [*FLAT, *IMPULSE, *PULLBACK_CANDLES, RESUMPTION])
    assert all(r is None for r in results[:-1])
    setup = results[-1]
    assert setup.strategy == PULLBACK and setup.side == LONG and setup.tag == f"SC-P-{11 * 5000}-L"
    assert setup.level == pytest.approx(4001.15)  # sommet de l'impulsion
    assert setup.structure == pytest.approx(4000.60)  # creux du repli : le stop va dessous
    assert setup.key == f"P+1@{5 * 5000}-{8 * 5000}"  # départ : le plus récent des plus bas égaux
    assert "impulsion de 1.20 $ (1.2 ATR M1) en 20 s, repli de 46 %" in setup.reason


def test_pullback_short_is_the_mirror_of_the_long():
    detector = PullbackDetector(CONFIG)
    candles = [
        *FLAT,
        candle(6, 3999.60, high=4000.00, low=3999.55),
        candle(7, 3999.20, high=3999.60, low=3999.15),
        candle(8, 3998.90, high=3999.20, low=3998.85),  # 4000.05 -> 3998.85
        candle(9, 3999.20, high=3999.25, low=3999.00),
        candle(10, 3999.30, high=3999.40, low=3999.15),
        candle(11, 3999.05, high=3999.30, low=3999.00),  # clôture sous le plus bas précédent (3999.15)
    ]
    setup = _run(detector, candles)[-1]
    assert setup.side == SHORT and setup.tag.endswith("-S")
    assert setup.level == pytest.approx(3998.85) and setup.structure == pytest.approx(3999.40)


def test_a_too_deep_pullback_cancels_the_impulse():
    detector = PullbackDetector(CONFIG)
    deep = candle(9, 4000.30, high=4000.90, low=4000.20)  # repli de 79 % > 70 %
    after = [candle(10, 4000.60, high=4000.65, low=4000.40), candle(11, 4000.90, high=4000.95, low=4000.60)]
    assert all(r is None for r in _run(detector, [*FLAT, *IMPULSE, deep, *after]))


def test_no_entry_when_the_resumption_comes_too_late():
    detector = PullbackDetector(CONFIG)
    drift = [candle(9 + k, 4000.75, high=4000.80, low=4000.70) for k in range(13)]  # 65 s sans reprise
    late = candle(22, 4001.00, high=4001.05, low=4000.80)
    assert all(r is None for r in _run(detector, [*FLAT, *IMPULSE, *drift, late]))


def test_a_shallow_pullback_is_not_enough():
    detector = PullbackDetector(CONFIG)
    shallow = [candle(9, 4001.05, high=4001.10, low=4001.00), candle(10, 4001.00, high=4001.05, low=4000.95)]
    resumption = candle(11, 4001.12, high=4001.14, low=4001.00)  # repli de 17 % seulement
    assert all(r is None for r in _run(detector, [*FLAT, *IMPULSE, *shallow, resumption]))


def test_no_impulse_without_atr_or_below_its_threshold():
    candles = [*FLAT, *IMPULSE, *PULLBACK_CANDLES, RESUMPTION]
    assert all(r is None for r in _run(PullbackDetector(CONFIG), candles, atr=0.0))
    assert all(r is None for r in _run(PullbackDetector(CONFIG), candles, atr=1.5))  # 1,20 $ < 1 x 1,50


def test_one_entry_per_impulse_and_a_new_impulse_is_a_new_occasion():
    detector = PullbackDetector(CONFIG)
    first = _run(detector, [*FLAT, *IMPULSE, *PULLBACK_CANDLES, RESUMPTION])[-1]
    # La hausse reprend depuis le creux du repli (4000.60) : nouvelle impulsion, nouveau repli, nouvelle reprise.
    more = [
        candle(12, 4001.30, high=4001.35, low=4000.95),
        candle(13, 4001.70, high=4001.75, low=4001.30),
        candle(14, 4001.40, high=4001.45, low=4001.20),
        candle(15, 4001.25, high=4001.35, low=4001.15),
        candle(16, 4001.50, high=4001.55, low=4001.25),
    ]
    results = _run(detector, more)
    second = results[-1]
    assert sum(r is not None for r in results) == 1 and second is not None
    assert second.key != first.key and second.level == pytest.approx(4001.75)


def test_make_detectors_follows_the_config_or_the_request():
    assert [d.code for d in make_detectors(CONFIG)] == [BREAKOUT, PULLBACK]
    assert [d.code for d in make_detectors(CONFIG, (PULLBACK,))] == [PULLBACK]
    only_breakout = CONFIG.model_copy(update={"pullback": CONFIG.pullback.model_copy(update={"enabled": False})})
    assert [d.code for d in make_detectors(only_breakout)] == [BREAKOUT]


# --- plan ------------------------------------------------------------------------------------------


def _setup(closes=(4000.0,) * 12 + (4000.30, 4000.40)):
    return _feed(BreakoutDetector(CONFIG), list(closes))[-1]


def test_plan_puts_the_stop_behind_the_signal_structure_and_targets_1_2_times_it():
    plan = plan_trade(_setup(), 4000.32, 4000.48, CONFIG, **KWARGS)
    # Plus bas médian des 30 dernières secondes : 3999.95 ; stop = 3999.95 - 0.08 (demi-spread) - 0.05 = 3999.82.
    assert plan.sl == pytest.approx(3999.82)
    assert plan.entry == 4000.48 and plan.stop_distance == pytest.approx(0.66)
    assert plan.tp == pytest.approx(4000.48 + 0.79)  # 1,2 x 0,66 arrondi
    assert plan.target == pytest.approx(0.79)


def _scaled(candles, factor):
    """Même scénario, écarts de prix multipliés (impulsion de 2,40 $ au lieu de 1,20 $ pour factor = 2)."""

    def price(value):
        return round(4000.0 + (value - 4000.0) * factor, 2)

    return [Candle(c.start_ms, price(c.open), price(c.high), price(c.low), price(c.close), c.bid, c.ask,
                   c.max_spread, c.ticks) for c in candles]  # fmt: skip


def test_plan_of_a_pullback_puts_the_stop_under_the_pullback_low():
    candles = _scaled([*FLAT, *IMPULSE, *PULLBACK_CANDLES, RESUMPTION], 2)
    setup = _run(PullbackDetector(CONFIG), candles, atr=2.0)[-1]
    assert setup.structure == pytest.approx(4001.20)
    plan = plan_trade(setup, 4001.82, 4001.98, CONFIG, **KWARGS)
    assert plan.sl == pytest.approx(4001.20 - 0.08 - 0.05)
    assert plan.stop_distance == pytest.approx(4001.98 - 4001.07)
    # Même signal à moitié échelle : stop de 0,56 $, objectif de 0,67 $ < 3 x 0,26 $ de coût : refus.
    small = _run(PullbackDetector(CONFIG), [*FLAT, *IMPULSE, *PULLBACK_CANDLES, RESUMPTION])[-1]
    assert "trop faible" in plan_trade(small, 4000.87, 4001.03, CONFIG, **KWARGS)


def test_plan_refuses_a_target_too_small_for_the_costs():
    # Spread de 0,40 : coût estimé 0,40 + 0,10 = 0,50 ; objectif 1,2 x 0,93 = 1,12 < 3 x 0,50.
    refusal = plan_trade(_setup(), 4000.20, 4000.60, CONFIG, **KWARGS)
    assert isinstance(refusal, str) and "trop faible" in refusal


def test_plan_refuses_stops_too_far():
    # Creux à 3990 dans les 30 dernières secondes : le stop derrière la structure serait à plus de 10 $.
    setup = _setup((4000.0,) * 9 + (3990.0,) + (4000.0,) * 2 + (4000.30, 4000.40))
    refusal = plan_trade(setup, 4000.32, 4000.48, CONFIG, **KWARGS)
    assert isinstance(refusal, str) and "trop loin" in refusal


def test_commission_counts_in_the_cost_filter():
    plan = plan_trade(_setup(), 4000.32, 4000.48, CONFIG, **KWARGS)
    assert plan.cost == pytest.approx(0.26)  # spread 0,16 + 2 x 0,05 de glissement
    # 0,07 $ par once aller-retour (7 $ par lot) : coût 0,33, objectif 0,79 < 3 x 0,33.
    refusal = plan_trade(_setup(), 4000.32, 4000.48, CONFIG, commission_per_oz=0.07, **KWARGS)
    assert isinstance(refusal, str) and "trop faible" in refusal


# --- règles d'entrée et fractionnement -------------------------------------------------------------


def _policy_check(policy, now_ms, *, key="k", open_trades=(), risk=5.0, day_result=0.0):
    return policy.refusal(now_ms, key=key, risk=risk, equity=5000.0, day_result=day_result,
                          day_start_equity=5000.0, open_trades=list(open_trades))  # fmt: skip


def test_up_to_five_entries_per_rolling_minute_with_a_candle_between_them():
    policy = EntryPolicy(CONFIG)
    for k in range(5):
        assert _policy_check(policy, k * 5_000, key=f"k{k}") is None
        policy.accept(k * 5_000)
    assert reason_key(_policy_check(policy, 25_000, key="k5")) == "entrées par minute au maximum"
    assert _policy_check(policy, 60_001, key="k6") is None  # la première entrée est sortie de la fenêtre
    policy = EntryPolicy(CONFIG)
    policy.accept(0)
    assert reason_key(_policy_check(policy, 3_000)) == "délai entre deux entrées"


def test_the_same_occasion_is_never_taken_twice_while_its_trade_is_open():
    policy = EntryPolicy(CONFIG)
    open_trade = Exposure("SC-B-1-L", "B+1@4000.05", LONG, 5.0)
    assert reason_key(_policy_check(policy, 0, key="B+1@4000.05", open_trades=[open_trade])) == (
        "même occasion déjà en position"
    )
    assert _policy_check(policy, 0, key="P+1@0-40000", open_trades=[open_trade]) is None  # autre occasion


def test_positions_cumulative_risk_and_daily_loss_limits():
    policy = EntryPolicy(CONFIG)
    five = [Exposure(f"t{k}", f"k{k}", LONG, 4.0) for k in range(5)]
    assert reason_key(_policy_check(policy, 0, open_trades=five)) == "positions ouvertes au maximum"
    heavy = [Exposure("t", "x", LONG, 22.0)]  # 0,5 % de 5 000 = 25
    assert reason_key(_policy_check(policy, 0, open_trades=heavy, risk=5.0)) == "risque cumulé au maximum"
    assert reason_key(_policy_check(policy, 0, day_result=-50.0)) == "perte du jour atteinte"


def test_split_volume_in_equal_parts_under_the_maximum_per_order():
    assert split_volume(0.11, 20.0, 0.01) == [0.11]
    assert split_volume(45.0, 20.0, 0.01) == [15.0, 15.0, 15.0]
    parts = split_volume(0.11, 0.05, 0.01)
    assert parts == [0.04, 0.04, 0.03] and sum(parts) == pytest.approx(0.11)
    assert split_volume(0.0, 20.0, 0.01) == []


# --- simulation au tick ----------------------------------------------------------------------------


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


# --- rejeu sur l'historique ------------------------------------------------------------------------

INSTRUMENT = Instrument(0.01, 100.0, 0.01, 100.0, 0.01, 0, 0)


def _frames(points, base_ms):
    ticks = pd.DataFrame(
        {
            "time_msc_server": [t for t, _ in points],
            "bid": [round(mid - 0.08, 2) for _, mid in points],
            "ask": [round(mid + 0.08, 2) for _, mid in points],
        }
    )
    minutes = np.arange(base_ms // 1000 - 300 * 60, base_ms // 1000, 60)
    close = np.linspace(3990.0, 4000.0, len(minutes))  # tendance M1 haussière, ATR M1 d'environ 1 $
    bars = pd.DataFrame({"time_server": minutes, "close": close, "high": close + 0.5, "low": close - 0.5})
    return ticks, bars


def _breakout_history(base_ms):
    """Range calme de 2 minutes puis cassure confirmée."""
    points = [(base_ms + k * 1000, 4000.00 + 0.02 * (k % 2)) for k in range(-120, 0)]
    points += [(base_ms + 5_000 + k * 1000, 4000.40) for k in range(5)]
    points += [(base_ms + 10_000 + k * 1000, 4000.50) for k in range(200)]
    return _frames(points, base_ms)


def _pullback_history(base_ms):
    """Range calme, impulsion de 2,40 $ en 15 s, repli de 46 %, puis reprise."""
    points = [(base_ms + k * 1000, 4000.00 + 0.02 * (k % 2)) for k in range(-120, 0)]
    path = np.concatenate(
        [np.linspace(4000.20, 4002.40, 15), np.linspace(4002.20, 4001.30, 10), np.linspace(4001.80, 4002.20, 10)]
    )
    points += [(base_ms + k * 1000, round(float(mid), 2)) for k, mid in enumerate(path)]
    points += [(base_ms + (35 + k) * 1000, 4002.20) for k in range(200)]
    return _frames(points, base_ms)


def _replay(history, base_ms, strategies=None):
    ticks, bars = history(base_ms)
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    return run_backtest(ticks, bars, CONFIG, instrument=INSTRUMENT, schedule=schedule, initial_equity=5700.0,
                        slippage_points=0.0, strategies=strategies)  # fmt: skip


def test_backtest_trades_the_confirmed_breakout_and_exits_at_max_duration():
    result = _replay(_breakout_history, server_epoch_of("2026-01-06 12:00") * 1000, (BREAKOUT,))
    assert result.signals == {BREAKOUT: 1} and len(result.trades) == 1
    (trade,) = result.trades.to_dict("records")
    assert trade["strategy"] == BREAKOUT and trade["parts"] == 1
    assert trade["reason"] == "durée max" and trade["exit_ms"] - trade["open_ms"] >= 120_000


def test_backtest_trades_the_pullback_resumption():
    result = _replay(_pullback_history, server_epoch_of("2026-01-06 12:00") * 1000, (PULLBACK,))
    assert result.signals[PULLBACK] == 1 and len(result.trades) == 1
    assert result.trades["strategy"].tolist() == [PULLBACK]


def test_backtest_refuses_an_entry_when_the_market_closes_before_the_max_duration():
    result = _replay(_breakout_history, server_epoch_of("2026-01-06 23:57") * 1000 + 30_000, (BREAKOUT,))
    assert result.signals == {BREAKOUT: 1} and result.trades.empty  # pause quotidienne à 23:59
    assert result.refusals == {(BREAKOUT, "le marché ferme avant la durée max"): 1}


# --- cadence liée au bénéfice -------------------------------------------------------------------------------


def _cadence(**changes):
    return Cadence(CONFIG.model_copy(update={"cadence": CONFIG.cadence.model_copy(update=changes)}))


def test_cadence_starts_at_the_base_limits():
    cadence = _cadence()
    assert cadence.limits() == Limits(5, 5.0, 5, 0.5)
    off = _cadence(enabled=False)
    for _ in range(100):
        off.on_close(1.0)
    assert off.level == 0 and off.limits() == Limits(5, 5.0, 5, 0.5)


def test_cadence_climbs_one_level_per_winning_window_within_the_ceilings():
    cadence = _cadence(window_trades=10)
    messages = [cadence.on_close(1.0) for _ in range(10)]
    assert cadence.level == 1 and messages[-1].startswith("cadence : palier 1 (x2)")
    assert cadence.limits() == Limits(10, 2.5, 10, 1.0)  # risque ouvert plafonné à la perte journalière (1 %)
    for _ in range(9):
        cadence.on_close(1.0)
    assert cadence.level == 1  # au plus un palier par série de 10 trades
    cadence.on_close(1.0)
    assert cadence.level == 2 and cadence.limits() == Limits(20, 1.25, 20, 1.0)
    for _ in range(20):
        cadence.on_close(1.0)
    assert cadence.level == 3 and cadence.limits() == Limits(30, 5.0 / 6, 30, 1.0)  # plafond de 30 entrées/min
    for _ in range(30):
        cadence.on_close(1.0)
    assert cadence.level == 3  # dernier palier


def test_cadence_falls_back_to_the_base_as_soon_as_the_recent_trades_lose():
    cadence = _cadence(window_trades=10)
    for _ in range(20):
        cadence.on_close(1.0)
    assert cadence.level == 2
    message = None
    for pnl in [-3.0, -3.0, -3.0, -3.0]:  # 10 derniers : six gains de 1, quatre pertes de 3 -> -6
        message = cadence.on_close(pnl) or message
    assert cadence.level == 0 and "retour à la base" in message
    assert cadence.limits() == Limits(5, 5.0, 5, 0.5)


def test_cadence_never_rises_after_losses_and_needs_a_full_window():
    cadence = _cadence(window_trades=10)
    for _ in range(9):
        cadence.on_close(5.0)
    assert cadence.level == 0  # pas encore 10 trades
    for _ in range(50):
        cadence.on_close(-1.0)
    assert cadence.level == 0


def test_cadence_state_is_rebuilt_from_the_closed_trades():
    results = [1.0] * 25 + [-0.5] * 3
    live = _cadence(window_trades=10)
    for pnl in results:
        live.on_close(pnl)
    restarted = _cadence(window_trades=10)
    restarted.rebuild(results)
    assert (restarted.level, restarted.since_change, list(restarted.results)) == (
        live.level,
        live.since_change,
        list(live.results),
    )


def test_policy_limits_follow_the_cadence():
    policy = EntryPolicy(CONFIG)
    five = [Exposure(f"t{k}", f"k{k}", LONG, 4.0) for k in range(5)]
    assert reason_key(_policy_check(policy, 0, open_trades=five, risk=4.0)) == "positions ouvertes au maximum"
    for _ in range(CONFIG.cadence.window_trades):
        policy.cadence.on_close(2.0)
    assert policy.cadence.level == 1
    assert _policy_check(policy, 0, open_trades=five, risk=4.0) is None  # 6e position : 24 $ <= 1 % de 5 000
    ten = [Exposure(f"t{k}", f"k{k}", LONG, 5.0) for k in range(10)]
    assert reason_key(_policy_check(policy, 0, open_trades=ten[:9], risk=6.0)) == "risque cumulé au maximum"


def test_cadence_config_is_checked():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="commence à 1"):
        CONFIG.cadence.model_validate({**CONFIG.cadence.model_dump(), "multipliers": [2, 4]})
    with pytest.raises(ValidationError, match="perte journalière"):
        CONFIG.model_validate({**CONFIG.model_dump(), "cadence": {**CONFIG.cadence.model_dump(),
                                                                  "ceiling_total_risk_pct": 2.0}})  # fmt: skip
    with pytest.raises(ValidationError, match="less than or equal to 5"):  # la base reste à 5 (accord de l'utilisateur)
        CONFIG.model_validate({**CONFIG.model_dump(), "max_entries_per_minute": 6})


# --- corrections tirées des trades démo -----------------------------------------------------------------------


def _policy_with(**changes):
    return EntryPolicy(CONFIG.model_copy(update=changes))


def test_same_side_cap_counts_only_trades_in_that_direction():
    policy = _policy_with(max_same_side_positions=2)
    two_sells = [Exposure(f"s{k}", f"s{k}", SHORT, 4.0) for k in range(2)]
    refusal = policy.refusal(0, key="new", risk=4.0, equity=5000.0, day_result=0.0, day_start_equity=5000.0,
                             open_trades=two_sells, side=SHORT)  # fmt: skip
    assert reason_key(refusal) == "trades ouverts dans ce sens au maximum"
    assert policy.refusal(0, key="new", risk=4.0, equity=5000.0, day_result=0.0, day_start_equity=5000.0,
                          open_trades=two_sells, side=LONG) is None  # fmt: skip


def _check_side(policy, now_ms, side):
    return policy.refusal(now_ms, key=f"k{now_ms}", risk=4.0, equity=5000.0, day_result=0.0,
                          day_start_equity=5000.0, open_trades=[], side=side)  # fmt: skip


def test_a_stop_hit_within_seconds_pauses_that_direction_only():
    policy = _policy_with(quick_stop_s=20, quick_stop_pause_s=60)
    policy.on_exit(SHORT, "stop", 100_000, 108_000, -4.5)  # stop touché en 8 s
    assert "stop touché en moins de 20 s" in _check_side(policy, 150_000, SHORT)
    assert _check_side(policy, 150_000, LONG) is None
    assert _check_side(policy, 168_001, SHORT) is None  # 60 s après la sortie
    policy.on_exit(SHORT, "stop", 200_000, 290_000, -4.5)  # stop après 90 s : pas une entrée dans le bruit
    assert _check_side(policy, 291_000, SHORT) is None


def test_a_loss_streak_in_one_direction_pauses_it():
    policy = _policy_with(loss_streak=3, loss_streak_pause_s=900)
    policy.on_exit(SHORT, "stop", 0, 60_000, -4.5)
    policy.on_exit(SHORT, "objectif", 0, 70_000, 5.0)  # un gain remet le compteur à zéro
    for k in range(2):
        policy.on_exit(SHORT, "stop", 0, 80_000 + k, -4.5)
    assert _check_side(policy, 90_000, SHORT) is None
    policy.on_exit(LONG, "stop", 0, 90_000, -4.5)  # les achats ont leur propre compteur
    assert _check_side(policy, 95_000, SHORT) is None
    policy.on_exit(SHORT, "durée max", 0, 100_000, -0.4)  # 3e perte d'affilée dans ce sens
    assert "3 pertes d'affilée" in _check_side(policy, 101_000, SHORT)
    assert _check_side(policy, 101_000, LONG) is None
    assert _check_side(policy, 1_000_001, SHORT) is None  # 15 min après


def test_new_policy_options_are_off_by_default():
    policy = EntryPolicy(CONFIG)
    for k in range(10):
        policy.on_exit(SHORT, "stop", k * 1000, k * 1000 + 2_000, -4.5)
    sells = [Exposure(f"s{k}", f"s{k}", SHORT, 4.0) for k in range(4)]
    assert policy.refusal(20_000, key="n", risk=4.0, equity=5000.0, day_result=0.0, day_start_equity=5000.0,
                          open_trades=sells, side=SHORT) is None  # fmt: skip


def test_a_losing_close_never_raises_the_cadence_and_losses_need_a_new_full_window():
    cadence = _cadence(window_trades=10)
    for pnl in [-6.0] + [0.6] * 8 + [-0.5]:
        cadence.on_close(pnl)
    assert cadence.recent_result() < 0
    cadence.on_close(-1.0)  # la perte de -6 sort de la fenêtre : la somme repasse positive sur un trade perdant
    assert cadence.recent_result() > 0 and cadence.level == 0
    for _ in range(8):
        cadence.on_close(1.0)
    assert cadence.level == 0  # pas encore 10 trades depuis le dernier passage en perte
    cadence.on_close(1.0)
    assert cadence.level == 1  # série complète, en bénéfice, close sur un trade gagnant


def test_open_risk_never_exceeds_what_is_left_of_the_daily_loss_budget():
    policy = EntryPolicy(CONFIG)
    for _ in range(CONFIG.cadence.window_trades * 3):
        policy.cadence.on_close(2.0)
    assert policy.cadence.level == 3  # risque ouvert permis : 1 % de 5 000 = 50
    open_trades = [Exposure(f"t{k}", f"k{k}", LONG, 5.0) for k in range(4)]  # 20 en jeu
    # Perte du jour -30 (0,6 %) : il reste 20 de budget, déjà en jeu : pas de nouveau trade.
    refusal = policy.refusal(0, key="n", risk=5.0, equity=5000.0, day_result=-30.0, day_start_equity=5000.0,
                             open_trades=open_trades)  # fmt: skip
    assert reason_key(refusal) == "budget de perte du jour"
    assert policy.refusal(0, key="n", risk=5.0, equity=5000.0, day_result=-20.0, day_start_equity=5000.0,
                          open_trades=open_trades) is None  # fmt: skip
