"""Moteur de scalping : bougies de 5 s, détecteurs (cassure, impulsion-repli), règles d'entrée, plan, simulation."""

import numpy as np
import pandas as pd
import pytest

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
    Setup,
    SimTrade,
    day_direction,
    ema_trend,
    make_detectors,
    plan_trade,
)
from goldbot.scalping.policy import (
    Cadence,
    EntryPolicy,
    Exposure,
    Limits,
    drawdown_pct,
    reason_key,
    split_volume,
    trade_volume,
)
from tests.conftest import server_epoch_of, v1_exit_settings

SETTINGS = v1_exit_settings()
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
    assert cadence.level == 2 and cadence.limits() == Limits(12, 1.25, 20, 1.0)  # une entrée par bougie de 5 s
    for _ in range(20):
        cadence.on_close(1.0)
    assert cadence.level == 3 and cadence.limits() == Limits(12, 5.0 / 6, 30, 1.0)
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
    with pytest.raises(ValidationError, match="less than or equal to 12"):  # une par bougie de 5 s au plus
        CONFIG.model_validate({**CONFIG.model_dump(), "max_entries_per_minute": 13})


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
    message = policy.on_exit(SHORT, "durée max", 0, 100_000, -0.4)  # 3e perte d'affilée dans ce sens
    assert message == "pause des ventes pendant 15 min (3 pertes d'affilée) : le marché ne va pas dans ce sens"
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


def test_the_window_turning_positive_on_a_losing_close_does_not_raise_the_cadence():
    cadence = _cadence(window_trades=10)
    for _ in range(9):
        cadence.on_close(5.0)
    cadence.on_close(-1.0)  # 10e trade : fenêtre pleine et en bénéfice, mais ce trade perd
    assert cadence.recent_result() > 0 and cadence.level == 0
    cadence.on_close(1.0)
    assert cadence.level == 1


def test_open_trades_in_floating_profit_do_not_enlarge_the_daily_budget():
    policy = EntryPolicy(CONFIG)
    for _ in range(CONFIG.cadence.window_trades):
        policy.cadence.on_close(2.0)
    five = [Exposure(f"t{k}", f"k{k}", LONG, 5.0) for k in range(5)]  # 25 en jeu
    # Réalisé -40, latent +25 (résultat du jour -15) : si tous les stops sont touchés, -65 > 50 de limite.
    refusal = policy.refusal(0, key="n", risk=5.0, equity=5000.0, day_result=-15.0, day_start_equity=5000.0,
                             open_trades=five, day_realized=-40.0)  # fmt: skip
    assert reason_key(refusal) == "budget de perte du jour"


# --- lot fixe et arrêt total au drawdown maximal ---------------------------------------------------------------


def test_fixed_lot_is_used_while_its_risk_stays_under_the_per_trade_cap():
    fixed = CONFIG.model_copy(update={"fixed_volume": 0.1, "risk_per_trade_pct": 1.0})
    # 0,1 lot, stop à 2,55 $ (+5 points) : 256 $ par lot -> 25,60 $ <= 1 % de 5 700 $ (57 $).
    assert trade_volume(fixed, 5700.0, 256.0, volume_min=0.01, volume_step=0.01) == (0.1, None)
    volume, why = trade_volume(fixed, 5700.0, 605.0, volume_min=0.01, volume_step=0.01)  # stop de 6 $ : 60,50 $
    assert volume == 0.0 and why.startswith("lot fixe au-dessus du risque maximal : 0.1 lot perd 60.50")
    tiny = fixed.model_copy(update={"fixed_volume": 0.005})
    assert trade_volume(tiny, 5700.0, 256.0, volume_min=0.01, volume_step=0.01)[0] == 0.0


def test_without_a_fixed_lot_the_volume_follows_the_risk_and_rounds_down():
    assert trade_volume(CONFIG, 5700.0, 256.0, volume_min=0.01, volume_step=0.01) == (0.02, None)  # 5,70 / 256
    volume, why = trade_volume(CONFIG, 5700.0, 800.0, volume_min=0.01, volume_step=0.01)  # 0,0071 < 0,01
    assert volume == 0.0 and why == "lot minimum au-dessus du budget de risque"


def _two_breakouts_history(base_ms):
    """Cassure, trade fermé à la durée maximale (perte du spread), puis une deuxième cassure plus tard."""
    ticks, bars = _breakout_history(base_ms)
    later = base_ms + 215_000
    points = [(later + k * 1000, 4000.10) for k in range(70)]
    points += [(later + 70_000 + k * 1000, 4000.95) for k in range(5)]
    points += [(later + 75_000 + k * 1000, 4001.05) for k in range(200)]
    more, _ = _frames(points, base_ms)
    return pd.concat([ticks, more], ignore_index=True), bars


def test_replay_stops_everything_at_the_maximum_drawdown():
    base_ms = server_epoch_of("2026-01-06 12:00") * 1000
    ticks, bars = _two_breakouts_history(base_ms)
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    run = lambda config: run_backtest(ticks, bars, config, instrument=INSTRUMENT, schedule=schedule,  # noqa: E731
                                      initial_equity=5700.0, slippage_points=0.0, strategies=(BREAKOUT,))  # fmt: skip
    normal = run(CONFIG)
    assert len(normal.trades) == 2 and normal.halted_ms is None
    strict = run(CONFIG.model_copy(update={"max_drawdown_pct": 0.001}))  # la première perte suffit
    assert len(strict.trades) == 1 and strict.halted_ms == int(strict.trades["exit_ms"].iloc[0])
    # Mesuré sur le latent comme en démo : le trade ouvert est fermé au marché à l'arrêt (close_all en démo).
    assert strict.trades["reason"].tolist() == ["arrêt total"]
    assert strict.refusals == {(BREAKOUT, "arrêt total : drawdown maximal atteint"): 1}


def test_drawdown_is_measured_from_the_peak():
    assert drawdown_pct(9000.0, 10_000.0) == pytest.approx(10.0) and drawdown_pct(10_500.0, 10_000.0) < 0


# --- pips et stop fixe en pips ----------------------------------------------------------------------------------


def test_fixed_pip_stop_is_placed_from_the_entry_like_the_user_example():
    config = CONFIG.model_copy(update={"fixed_stop_pips": 30.0, "fixed_target_pips": 90.0, "min_stop_points": 200})
    candle = Candle(0, 4000.40, 4000.50, 4000.40, 4000.50, 4000.42, 4000.58, 0.16, 5)
    sell = Setup(BREAKOUT, SHORT, 4000.60, 4000.95, candle, key="B-1", reason="test")  # structure à 0,35 $
    plan = plan_trade(sell, 4000.00, 4000.16, config, point=0.01, stops_level_points=0, freeze_level_points=0)
    assert plan.entry == 4000.00 and plan.sl == pytest.approx(4003.00) and plan.tp == pytest.approx(3991.00)
    assert plan.stop_distance == pytest.approx(3.00) and plan.target == pytest.approx(9.00)


def test_refusals_are_written_in_pips():
    candle = Candle(0, 4000.40, 4000.50, 4000.40, 4000.50, 4000.42, 4000.58, 0.16, 5)
    buy = Setup(BREAKOUT, LONG, 4000.20, 4000.10, candle, key="B+1", reason="test")
    config = CONFIG.model_copy(update={"min_stop_points": 200})
    refusal = plan_trade(buy, 4000.42, 4000.58, config, point=0.01, stops_level_points=0, freeze_level_points=0)
    assert refusal == "stop trop proche : 6.1 pips < 20.0 pips"  # 4000.58 - (4000.10 - 0.08 - 0.05) = 0,61 $


def test_a_fixed_pip_stop_outside_the_allowed_range_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="fixed_stop_pips"):
        CONFIG.model_validate({**CONFIG.model_dump(), "fixed_stop_pips": 100.0})  # 10 $ > 6 $ maximum


# --- filtre de sens : mouvement du jour ------------------------------------------------------------------------


def _days_of_bars(days, *, range_per_day=10.0, last_move=0.0):
    """Barres M1 de plusieurs jours de cotation (heure serveur) : chaque jour oscille de range_per_day autour de
    4000, le dernier jour finit à ouverture + last_move."""
    rows = []
    for d in range(days):
        start = 1_767_657_600 + d * 86_400 + 3_600  # 01:00 serveur
        for k in range(120):
            price = 4000.0 + (range_per_day / 2 if k % 2 else -range_per_day / 2) * (k in (10, 11))
            rows.append((start + k * 60, 4000.0, price + 0.5 if k in (10, 11) else 4000.5, price - 0.5, 4000.0))
    frame = pd.DataFrame(rows, columns=["time_server", "open", "high", "low", "close"])
    frame.loc[frame.index[-1], "close"] = 4000.0 + last_move
    return frame


def test_day_direction_follows_a_strong_move_since_the_open_and_ignores_a_weak_one():
    config = CONFIG.model_copy(update={"direction_filter": True, "direction_min_move_atr": 0.5})
    bars = _days_of_bars(20, last_move=-6.0)
    atr_known = day_direction(bars, config)  # ATR des 14 jours précédents : 11 $ environ (range 10 $ + 1 $)
    assert atr_known[-1] == -1  # baisse de 6 $ >= 0,5 x 11 $
    assert day_direction(_days_of_bars(20, last_move=+3.0), config)[-1] == 0  # 3 $ < 5,5 $ : pas de sens
    assert (day_direction(_days_of_bars(10), config) == 0).all()  # moins de 15 jours : pas d'ATR, pas de sens


def test_day_direction_is_the_same_with_a_short_or_a_long_history():
    config = CONFIG.model_copy(update={"direction_filter": True})
    bars = _days_of_bars(40, last_move=-6.0)
    tail = bars[bars["time_server"] >= bars["time_server"].iloc[-1] - 16 * 86_400].reset_index(drop=True)
    assert day_direction(tail, config)[-1] == day_direction(bars, config)[-1] == -1


def test_direction_filter_refuses_trades_against_or_without_the_day_move():
    policy = EntryPolicy(CONFIG.model_copy(update={"direction_filter": True}))
    check = lambda side, direction: policy.refusal(0, key="k", risk=5.0, equity=5000.0, day_result=0.0,  # noqa: E731
                                                   day_start_equity=5000.0, open_trades=[], side=side,
                                                   direction=direction)  # fmt: skip
    assert check(SHORT, -1) is None
    assert reason_key(check(LONG, -1)) == "sens"
    assert "contre le mouvement du jour (baisse)" in check(LONG, -1)
    assert "pas de mouvement net du jour" in check(SHORT, 0)
    off = EntryPolicy(CONFIG)
    assert off.refusal(0, key="k", risk=5.0, equity=5000.0, day_result=0.0, day_start_equity=5000.0,
                       open_trades=[], side=LONG, direction=-1) is None  # fmt: skip


def test_replay_applies_the_direction_filter():
    base_ms = server_epoch_of("2026-01-06 12:00") * 1000
    ticks, bars = _breakout_history(base_ms)  # un seul jour de barres : pas d'ATR journalier, donc pas de sens
    bars = bars.assign(open=bars["close"])
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    config = CONFIG.model_copy(update={"direction_filter": True})
    result = run_backtest(ticks, bars, config, instrument=INSTRUMENT, schedule=schedule, initial_equity=5700.0,
                          slippage_points=0.0, strategies=(BREAKOUT,))  # fmt: skip
    assert result.trades.empty and result.refusals == {(BREAKOUT, "sens"): 1}


def test_the_stop_can_sit_behind_a_structure_older_than_the_breakout_window():
    # Stop derrière les 5 dernières minutes : le creux d'il y a 3 minutes compte, même hors de la fenêtre de 60 s.
    config = CONFIG.model_copy(update={"breakout": CONFIG.breakout.model_copy(update={"stop_lookback_s": 300})})
    detector = BreakoutDetector(config)
    closes = [3998.0] + [4000.0] * 40  # creux à 3998 il y a 200 s, puis 200 s de range plat
    _feed(detector, closes)
    start = len(closes)
    assert detector.on_candle(candle(start, 4000.30), LONG, 0.0) is None
    setup = detector.on_candle(candle(start + 1, 4000.40), LONG, 0.0)
    assert setup is not None and setup.level == pytest.approx(4000.05)
    assert setup.structure == pytest.approx(3997.95)  # plus bas des 300 dernières secondes


# --- lecture du marché ----------------------------------------------------------------------------------------


def _read_config(model="test", **update):
    from goldbot.config import ScalpMarketReadConfig

    return CONFIG.model_copy(update={"market_read": ScalpMarketReadConfig(enabled=True, model=model), **update})


def test_market_read_config_needs_a_model_and_excludes_the_day_filter():
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="nom du modèle"):
        CONFIG.model_validate({**CONFIG.model_dump(), "market_read": {"enabled": True}})
    with pytest.raises(ValidationError, match="choisir un seul sens"):
        CONFIG.model_validate({**CONFIG.model_dump(), "direction_filter": True,
                               "market_read": {"enabled": True, "model": "x"}})  # fmt: skip


def test_read_direction_uses_the_model_the_day_move_or_nothing(monkeypatch):
    from goldbot.scalping import market_read

    bars = _days_of_bars(20, last_move=-6.0)
    monkeypatch.setitem(market_read.MODELS, "test", lambda b, **p: np.where(np.arange(len(b)) % 2, 1, -1))
    values = market_read.read_direction(bars, _read_config())
    assert values.dtype.kind == "i" and values[0] == -1 and values[1] == 1
    assert market_read.read_direction(bars, CONFIG.model_copy(update={"direction_filter": True}))[-1] == -1
    assert (market_read.read_direction(bars, CONFIG) == 0).all()
    with pytest.raises(ValueError, match="modèle inconnu"):
        market_read.read_direction(bars, _read_config("inconnu"))


def test_market_read_refuses_trades_against_or_without_a_clear_read():
    policy = EntryPolicy(_read_config())
    check = lambda side, direction: policy.refusal(0, key="k", risk=5.0, equity=5000.0, day_result=0.0,  # noqa: E731
                                                   day_start_equity=5000.0, open_trades=[], side=side,
                                                   direction=direction)  # fmt: skip
    assert check(LONG, 1) is None and check(SHORT, -1) is None
    assert check(LONG, -1) == "lecture du marché : marché vendeur en ce moment"
    assert check(SHORT, 1) == "lecture du marché : marché acheteur en ce moment"
    assert check(SHORT, 0) == "lecture du marché : pas de sens clair en ce moment"
    assert reason_key(check(SHORT, 0)) == "lecture du marché"


def test_replay_follows_the_market_read(monkeypatch):
    from goldbot.scalping import market_read

    base_ms = server_epoch_of("2026-01-06 12:00") * 1000
    ticks, bars = _breakout_history(base_ms)
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    for read, traded in ((1, True), (-1, False)):  # la cassure du test est un achat
        monkeypatch.setitem(market_read.MODELS, "test", lambda b, **p: np.full(len(b), read))
        result = run_backtest(ticks, bars, _read_config(), instrument=INSTRUMENT, schedule=schedule,
                              initial_equity=5700.0, slippage_points=0.0, strategies=(BREAKOUT,))  # fmt: skip
        assert (not result.trades.empty) == traded
        if not traded:
            assert result.refusals == {(BREAKOUT, "lecture du marché"): 1}


# --- deux bougies (idée de l'utilisateur) ---------------------------------------------------------------------


def _two_config(**update):
    from goldbot.config import ScalpTwoCandleConfig

    return CONFIG.model_copy(update={"two_candles": ScalpTwoCandleConfig(enabled=True, **update)})


def _minute(detector, minute, first, last, *, high=None, low=None):
    """12 bougies de 5 s pour la minute `minute` : de `first` à `last` (prix médians)."""
    prices = np.linspace(first, last, 12)
    out = []
    for k, price in enumerate(prices):
        top = high if (high is not None and k == 6) else price + 0.05
        bottom = low if (low is not None and k == 6) else price - 0.05
        c = Candle(minute * 60_000 + k * 5000, price, top, bottom, price, price - 0.08, price + 0.08, 0.16, 10)
        out.append(detector.on_candle(c, 0, 0.0))
    return out


def test_two_candles_sell_after_a_bearish_then_bullish_minute_with_the_stop_above_both():
    from goldbot.scalping.engine import TWO_CANDLES, TwoCandleDetector

    detector = TwoCandleDetector(_two_config())
    assert all(s is None for s in _minute(detector, 100, 4002.0, 4000.0, high=4003.0))  # baissière, plus haut 4003
    *before, setup = _minute(detector, 101, 4000.2, 4001.0)  # haussière
    assert all(s is None for s in before)  # décidé à la clôture de la minute seulement
    assert setup is not None and setup.strategy == TWO_CANDLES and setup.side == SHORT
    assert setup.structure == pytest.approx(4003.0) and setup.key == f"R-1@{101 * 60_000}"
    assert "baissière puis haussière" in setup.reason
    *_, buy = _minute(detector, 102, 4001.0, 4000.4, low=3999.0)  # haussière puis baissière : achat
    assert buy is not None and buy.side == LONG and buy.structure == pytest.approx(3999.0)


def test_two_candles_need_consecutive_minutes_and_a_colour_change():
    from goldbot.scalping.engine import TwoCandleDetector

    detector = TwoCandleDetector(_two_config())
    _minute(detector, 100, 4002.0, 4000.0)
    assert _minute(detector, 102, 4000.2, 4001.0)[-1] is None  # une minute manque (fermeture) : rien
    assert _minute(detector, 103, 4001.0, 4002.0)[-1] is None  # deux haussières : rien


def test_two_candles_minute_without_its_last_slot_is_closed_by_the_next_one():
    from goldbot.scalping.engine import TwoCandleDetector

    detector = TwoCandleDetector(_two_config())
    _minute(detector, 100, 4002.0, 4000.0)
    for k in range(6):  # minute 101 haussière, sans tick après 30 s
        detector.on_candle(Candle(101 * 60_000 + k * 5000, 4000 + k * 0.2, 4000 + k * 0.2 + 0.05,
                                  4000 + k * 0.2 - 0.05, 4000 + k * 0.2, 0, 0, 0.16, 5), 0, 0.0)  # fmt: skip
    first = Candle(102 * 60_000, 4001.0, 4001.05, 4000.95, 4001.0, 4000.92, 4001.08, 0.16, 5)
    setup = detector.on_candle(first, 0, 0.0)
    assert setup is not None and setup.side == SHORT and setup.candle is first


def test_two_candles_plan_widens_a_close_stop_and_sets_one_target_per_position():
    from goldbot.scalping.engine import TWO_CANDLES

    config = _two_config(min_stop_pips=10.0, target_pips=[20.0, 25.0, 30.0])
    c = candle(0, 4000.0)
    close = Setup(TWO_CANDLES, SHORT, 4000.0, 4000.30, c, key="R", reason="test")  # structure à 0,30 $
    plan = plan_trade(close, 3999.92, 4000.08, config, **KWARGS)
    assert plan.sl == pytest.approx(4000.92) and plan.stop_distance == pytest.approx(1.0)  # élargi à 10 pips
    assert plan.tps == pytest.approx((3997.92, 3997.42, 3996.92)) and plan.tp == pytest.approx(3996.92)
    wide = Setup(TWO_CANDLES, LONG, 4000.0, 3997.0, c, key="R", reason="test")
    plan = plan_trade(wide, 3999.92, 4000.08, config, **KWARGS)
    assert plan.sl == pytest.approx(3996.87) and plan.tps[0] == pytest.approx(4002.08)  # stop sous la structure


def test_ladder_volumes_split_the_lot_into_one_position_per_target():
    from goldbot.scalping.policy import ladder_volumes

    assert ladder_volumes(0.04, 3, 0.01, 0.01) == [0.02, 0.01, 0.01]
    assert ladder_volumes(0.02, 3, 0.01, 0.01) == [0.01, 0.01]  # pas assez pour trois positions
    assert ladder_volumes(0.01, 3, 0.01, 0.01) == [0.01]
    assert ladder_volumes(0.0, 3, 0.01, 0.01) == []


def test_a_simulated_ladder_closes_positions_at_their_targets_then_the_rest_at_the_stop():
    from goldbot.scalping.engine import Plan

    plan = Plan(SHORT, 4000.0, 4001.0, 3997.0, 1.0, 3.0, 0.3, 0.16, tps=(3998.0, 3997.5, 3997.0))
    trade = SimTrade.open("t", plan, 4000.0, 4000.16, 0, 600, 0.0, volume=0.04, weights=(0.02, 0.01, 0.01))
    assert trade.tps == plan.tps
    assert not trade.on_tick(1000, 3997.80, 3997.96)  # ask 3997.96 : première position à son objectif (3998)
    assert trade.leg_exits == [3998.0]
    assert trade.on_tick(2000, 4000.90, 4001.06)  # ask au-dessus du stop : les deux autres au stop
    assert trade.reason == "stop"
    assert trade.exit_price == pytest.approx((0.02 * 3998.0 + 0.02 * 4001.06) / 0.04)
    assert trade.move == pytest.approx((4000.0 - trade.exit_price))


def test_replay_trades_two_candles_with_three_positions():
    from goldbot.scalping.engine import TWO_CANDLES

    base_ms = server_epoch_of("2026-01-06 12:00") * 1000
    points = [(base_ms + k * 1000, 4001.0 - 0.02 * k) for k in range(60)]  # minute baissière
    points += [(base_ms + 60_000 + k * 1000, 3999.9 + 0.01 * k) for k in range(60)]  # minute haussière
    points += [(base_ms + 120_000 + k * 1000, 4000.5 - 0.1 * k) for k in range(60)]  # baisse de 6 $ ensuite
    ticks, bars = _frames(points, base_ms)
    schedule = MarketSchedule.from_config(SETTINGS.market_hours)
    config = _two_config(min_stop_pips=10.0, target_pips=[20.0, 25.0, 30.0])
    config = config.model_copy(update={"risk_per_trade_pct": 0.1, "max_total_risk_pct": 0.5})
    for learning in (False, True):  # l'apprentissage ne remplace pas les objectifs choisis par l'utilisateur
        cfg = config.model_copy(update={"learning": config.learning.model_copy(update={"enabled": learning})})
        result = run_backtest(ticks, bars, cfg, instrument=INSTRUMENT, schedule=schedule, initial_equity=5700.0,
                              slippage_points=0.0, strategies=(TWO_CANDLES,))  # fmt: skip
        trades = result.trades
        assert len(trades) >= 1 and trades.iloc[0]["side"] == SHORT and trades.iloc[0]["parts"] == 3
        assert trades.iloc[0]["reason"] == "objectif" and trades.iloc[0]["pnl"] > 0
