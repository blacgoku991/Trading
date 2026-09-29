"""Moteur de backtest : exécutions au bid/ask, SL/TP, gaps, sorties horaires, taille, limites, swaps."""

import pandas as pd
import pytest

from goldbot.backtest.engine import Backtest, Costs, Instrument
from goldbot.risk.limits import DAILY_LOSS
from goldbot.strategies.base import LONG, MARKET, SHORT, STOP, OrderIntent

INSTRUMENT = Instrument(point=0.01, contract_size=100.0, volume_min=0.01, volume_max=20.0, volume_step=0.01)
NO_COSTS = Costs()
START = "2026-01-06 10:00"  # mardi, hiver : heure serveur = UTC + 2 h


def frame(rows, start=START, spread=15):
    """Barres M1 consécutives (open, high, low, close) en prix bid ; spread en points."""
    times = pd.date_range(start, periods=len(rows), freq="min", tz="UTC")
    server = (times.tz_convert(None) - pd.Timestamp(0)) // pd.Timedelta(seconds=1) + 7200
    opens, highs, lows, closes = zip(*rows, strict=True)
    return pd.DataFrame(
        {
            "time": times.as_unit("ms"),
            "time_server": server,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "spread": spread,
        }
    )


def intent(bars, *, side=LONG, kind=STOP, price=2001.0, sl=1999.0, tp=2004.0, at=0, expires=None, exit_at=None):
    times = bars["time"]
    return OrderIntent(
        time=times.iloc[at],
        side=side,
        kind=kind,
        price=price if kind == STOP else None,
        sl=sl,
        tp=tp,
        expires_at=expires if expires is not None else times.iloc[-1] + pd.Timedelta(hours=1),
        exit_at=exit_at,
        tag=f"test-{at}-{side}",
        reason="test",
    )


def run(bars, intents, *, settings, equity=10_000.0, costs=NO_COSTS, halt=True):
    backtest = Backtest(
        bars, instrument=INSTRUMENT, costs=costs, risk=settings.risk, initial_equity=equity, halt_on_drawdown=halt
    )
    return backtest.run(intents)


QUIET = (2000.00, 2000.50, 1999.80, 2000.20)
BELOW_TRIGGER = (2000.20, 2000.80, 2000.10, 2000.70)  # ask haut = 2000.95 < 2001
TRIGGER = (2000.80, 2001.50, 2000.70, 2001.40)  # ask haut = 2001.65 >= 2001


def test_long_stop_fills_on_ask_and_takes_profit(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (2001.40, 2004.20, 2001.30, 2004.00)])
    result = run(bars, [intent(bars)], settings=settings)
    (trade,) = result.trades.itertuples()
    assert trade.entry_time == bars["time"].iloc[2]
    assert trade.entry_price == pytest.approx(2001.00)  # max(niveau, ask à l'ouverture 2000.95)
    assert trade.lots == pytest.approx(0.25)  # 50 $ de risque / (2 $ x 100 oz)
    assert trade.exit_reason == "TP" and trade.exit_price == pytest.approx(2004.00)
    assert trade.pnl == pytest.approx(75.0) and trade.r_multiple == pytest.approx(1.5)


def test_sl_wins_when_sl_and_tp_are_in_the_same_bar(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (2001.40, 2004.20, 1998.90, 2002.00)])
    (trade,) = run(bars, [intent(bars)], settings=settings).trades.itertuples()
    assert trade.exit_reason == "SL" and trade.r_multiple == pytest.approx(-1.0)


def test_gap_through_the_sl_exits_at_the_open(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (1998.50, 1998.80, 1998.00, 1998.60)])
    (trade,) = run(bars, [intent(bars)], settings=settings).trades.itertuples()
    assert trade.exit_reason == "SL (gap)" and trade.exit_price == pytest.approx(1998.50)
    assert trade.r_multiple == pytest.approx(-1.25)


def test_short_sl_is_triggered_by_the_ask(settings):
    bars = frame(
        [
            QUIET,
            (1999.50, 1999.60, 1998.90, 1999.00),  # bid bas 1998.90 <= 1999 : vente déclenchée
            (1999.00, 2000.90, 1998.95, 2000.50),  # bid haut 2000.90 < SL, mais ask haut 2001.05 >= SL
        ]
    )
    short = intent(bars, side=SHORT, price=1999.0, sl=2001.0, tp=1996.0)
    (trade,) = run(bars, [short], settings=settings).trades.itertuples()
    assert trade.entry_price == pytest.approx(1999.00)
    assert trade.exit_reason == "SL" and trade.exit_price == pytest.approx(2001.00)


def test_time_exit_at_the_open_of_the_exit_bar(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (2001.60, 2001.90, 2001.50, 2001.80), QUIET])
    order = intent(bars, exit_at=bars["time"].iloc[3])
    (trade,) = run(bars, [order], settings=settings).trades.itertuples()
    assert trade.exit_reason == "sortie horaire" and trade.exit_price == pytest.approx(2001.60)


def test_unfilled_stop_order_expires(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, TRIGGER])
    order = intent(bars, expires=bars["time"].iloc[2])  # valable pour la barre 1 seulement
    result = run(bars, [order], settings=settings)
    assert result.trades.empty and result.skipped.empty


def test_market_order_fills_at_the_next_open_on_the_ask(settings):
    bars = frame([QUIET, BELOW_TRIGGER, QUIET])
    order = intent(bars, kind=MARKET, sl=1998.0, tp=2010.0)
    (trade,) = run(bars, [order], settings=settings).trades.itertuples()
    assert trade.entry_time == bars["time"].iloc[1]
    assert trade.entry_price == pytest.approx(2000.35)  # ouverture 2000.20 + spread 0.15
    assert trade.exit_reason == "fin des données"


def test_slippage_and_spread_floor_are_charged(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (2001.40, 2004.20, 2001.30, 2004.00)], spread=0)
    costs = Costs(min_spread_points=15, slippage_points=5)
    (trade,) = run(bars, [intent(bars)], settings=settings, costs=costs).trades.itertuples()
    assert trade.entry_price == pytest.approx(2001.05)  # niveau + 5 points de glissement
    assert trade.spread_points == pytest.approx(15.0)  # plancher appliqué au spread nul


def test_trade_is_skipped_when_the_minimum_volume_would_risk_too_much(settings):
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER])
    result = run(bars, [intent(bars)], settings=settings, equity=100.0)  # 0,50 $ de risque
    assert result.trades.empty
    assert result.skipped["reason"].tolist() == ["volume sous le minimum du broker pour ce risque"]


def test_intent_dated_on_a_missing_bar_is_rejected(settings):
    bars = frame([QUIET, QUIET])
    bad = intent(bars)
    bad = OrderIntent(**{**bad.__dict__, "time": bad.time + pd.Timedelta(seconds=30)})
    with pytest.raises(ValueError, match="barre absente"):
        run(bars, [bad], settings=settings)


def _big_loss_bars():
    # Achat déclenché barre 2, puis gap à 1960 : perte de 41 $ x 25 oz = 1 025 $ (> 2 % et > 10 %).
    return frame([QUIET, BELOW_TRIGGER, TRIGGER, (1960.0, 1961.0, 1959.0, 1960.5), QUIET, QUIET, TRIGGER])


def test_daily_loss_blocks_new_entries_for_the_day(settings):
    bars = _big_loss_bars()
    later = intent(bars, at=5, price=2001.0, sl=1999.0, tp=2004.0)
    result = run(bars, [intent(bars), later], settings=settings, halt=False)
    assert len(result.trades) == 1
    assert result.skipped["reason"].tolist() == [DAILY_LOSS]


def test_account_mode_halts_on_max_drawdown_research_mode_only_notes_it(settings):
    bars = _big_loss_bars()
    account = run(bars, [intent(bars)], settings=settings, halt=True)
    assert account.halted and account.halt_time is None
    research = run(bars, [intent(bars)], settings=settings, halt=False)
    assert not research.halted and research.halt_time == bars["time"].iloc[3]


def test_swap_is_charged_three_times_on_wednesday_night(settings):
    # Mercredi 23:50 -> jeudi 01:05 heure serveur (UTC + 2 h en hiver).
    wednesday = frame([QUIET] * 9, start="2026-01-07 21:50")
    thursday = frame([QUIET] * 6, start="2026-01-07 23:00")
    bars = pd.concat([wednesday, thursday], ignore_index=True)
    order = intent(bars, kind=MARKET, sl=1990.0, tp=2010.0, exit_at=bars["time"].iloc[-1])
    costs = Costs(swap_long_points=-61.6, swap_short_points=40.5, triple_swap_weekday=2)
    (trade,) = run(bars, [order], settings=settings, costs=costs).trades.itertuples()
    assert trade.swap == pytest.approx(-61.6 * 0.01 * 100 * trade.lots * 3)


def test_a_limit_crossed_while_closing_several_positions_closes_each_once(settings):
    # Deux achats ouverts ; le gap suivant les sort tous les deux au SL et franchit la perte journalière
    # pendant la boucle des sorties : chaque position ne doit être fermée qu'une fois.
    bars = frame([QUIET, BELOW_TRIGGER, TRIGGER, (2001.40, 2001.50, 2001.30, 2001.40), (1960.0, 1961.0, 1959.0, 1960.5)])
    first = intent(bars, price=2001.0, sl=1999.0, tp=2010.0)
    second = OrderIntent(**{**first.__dict__, "tag": "second", "price": 2001.2, "sl": 1999.2})
    result = run(bars, [first, second], settings=settings, halt=False)
    assert len(result.trades) == 2
    assert sorted(result.trades["strategy_tag"]) == ["second", "test-0-1"]
