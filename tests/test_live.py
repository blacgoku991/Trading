"""Boucle live avec le broker factice : ordres, idempotence, sorties, réconciliation, limites."""

from dataclasses import replace

import pandas as pd
import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.fake_broker import FakeBroker, make_account, make_bars
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.live.runner import LiveOptions, LiveRunner
from goldbot.state.store import CLOSED, OPEN, SIMULATED, SKIPPED, StateStore
from goldbot.strategies.base import LONG, MARKET, OrderIntent, StopUpdate, Strategy
from tests.conftest import open_minutes, tick_at

MAGIC = 20260928


class Scripted(Strategy):
    """Stratégie de test : renvoie des intentions prévues, seulement une fois leur barre clôturée."""

    name = "test"
    description = "test"

    def __init__(self, intents, updates=()):
        self.planned = intents
        self.updates = list(updates)

    def intents(self, bars):
        last = bars["time"].iloc[-1] if len(bars) else None
        return [i for i in self.planned if last is not None and i.time <= last]

    def stop_updates(self, bars):
        last = bars["time"].iloc[-1] if len(bars) else None
        return [u for u in self.updates if last is not None and u.time <= last]


class Clock:
    def __init__(self, start):
        self.now = pd.Timestamp(start)

    def __call__(self):
        return self.now.to_pydatetime()


def _intent(time, *, tag="T-1", sl=3990.0, exit_minutes=30, side=LONG):
    return OrderIntent(
        time=pd.Timestamp(time),
        side=side,
        kind=MARKET,
        price=None,
        sl=sl,
        tp=None,
        expires_at=pd.Timestamp(time) + pd.Timedelta(minutes=2),
        exit_at=pd.Timestamp(time) + pd.Timedelta(minutes=exit_minutes),
        tag=tag,
        reason="test",
    )


@pytest.fixture
def world(tmp_path, settings, rule, schedule):
    """Marché ouvert le mardi 06/01/2026 à 10:00 UTC (12:00 heure serveur)."""
    clock = Clock("2026-01-06 10:00:30+00:00")
    broker = FakeBroker(account=make_account(balance=10_000.0, equity=10_000.0, margin_free=10_000.0))
    broker.connect()

    def set_time(moment):
        clock.now = pd.Timestamp(moment)
        # Barres jusqu'à la minute en cours (la dernière est encore ouverte), prix vers 4 000.
        server_now = rule.to_server(clock.now.to_pydatetime())
        minutes = open_minutes(schedule, "2026-01-05", pd.Timestamp(server_now).floor("min") + pd.Timedelta(minutes=1))
        broker.history_bars = make_bars(minutes, start_price=4000.0, seed=1)
        broker.ticks["XAUUSD"] = tick_at(rule, clock.now.to_pydatetime(), bid=4000.00, ask=4000.15)

    set_time(clock.now)

    def runner(strategy, *, simulate=False, store=None):
        return LiveRunner(
            broker,
            ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=lambda s: None),
            strategy,
            store or StateStore(tmp_path / "live.sqlite"),
            settings=settings,
            spec=broker.symbol("XAUUSD"),
            simulate=simulate,
            options=LiveOptions(),
            now_utc=clock,
            sleep=lambda s: None,
        )

    return broker, clock, set_time, runner


def _signal_time(clock):
    """Ouverture de la dernière barre clôturée."""
    return clock.now.floor("min") - pd.Timedelta(minutes=1)


def test_new_signal_opens_one_position_with_its_stop_loss(world):
    broker, clock, _, runner = world
    live = runner(Scripted([_intent(_signal_time(clock))]))
    live.step()
    (sent,) = broker.sent
    assert sent["action"] == C.TRADE_ACTION_DEAL and sent["type"] == C.ORDER_TYPE_BUY
    assert sent["sl"] == 3990.0 and sent["magic"] == MAGIC and sent["comment"] == "T-1"
    # 0,5 % de 10 000 = 50 $ ; perte pour 1 lot au SL (10,15 $ + 5 points) x 100 = 1 020 $ : 0,04 lot.
    assert sent["volume"] == pytest.approx(0.04)
    (row,) = live.store.with_status(OPEN)
    assert row.tag == "T-1" and row.position == broker.positions()[0].ticket
    assert live.limits.trades_today == 1


def test_a_signal_is_never_sent_twice_even_after_a_restart(world, tmp_path):
    broker, clock, _, runner = world
    strategy = Scripted([_intent(_signal_time(clock))])
    live = runner(strategy)
    live.step()
    live.step()
    runner(strategy, store=StateStore(tmp_path / "live.sqlite")).step()  # redémarrage du bot
    assert len([r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL]) == 1


def test_time_exit_closes_the_position_and_records_the_result(world):
    broker, clock, set_time, runner = world
    live = runner(Scripted([_intent(_signal_time(clock), exit_minutes=5)]))
    live.step()
    set_time(clock.now + pd.Timedelta(minutes=6))
    live.step()  # sortie horaire
    assert broker.positions() == []
    live.step()  # résultat relevé dans les deals
    (row,) = live.store.with_status(CLOSED)
    assert live.store.detail(row.tag)["pnl"] is not None


def test_stop_loss_hit_on_the_server_is_recorded(world):
    broker, clock, set_time, runner = world
    live = runner(Scripted([_intent(_signal_time(clock))]))
    live.step()
    broker.hit_stop(broker.positions()[0].ticket, 3990.0)
    set_time(clock.now + pd.Timedelta(seconds=10))
    live.step()
    detail = live.store.detail("T-1")
    assert detail["status"] == CLOSED and detail["detail"] == "SL" and detail["pnl"] < 0
    assert live.limits.consecutive_losses == 1


def test_missing_stop_loss_is_put_back(world):
    broker, clock, _, runner = world
    live = runner(Scripted([_intent(_signal_time(clock))]))
    live.step()
    ticket = broker.positions()[0].ticket
    broker._positions[ticket] = replace(broker._positions[ticket], sl=0.0)
    live.step()
    assert broker.positions()[0].sl == 3990.0


def test_unknown_bot_position_is_closed_but_manual_trades_are_untouched(world):
    broker, _, _, runner = world
    base = {"action": C.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": 0.02, "type": C.ORDER_TYPE_BUY}
    broker.order_send({**base, "magic": MAGIC, "comment": "inconnu", "sl": 3900.0})
    broker.order_send({**base, "magic": 0, "comment": "manuel"})
    runner(Scripted([])).step()
    remaining = broker.positions()
    assert [p.comment for p in remaining] == ["manuel"]


def test_stale_signal_after_a_late_restart_is_ignored(world):
    broker, clock, _, runner = world
    old = _intent(_signal_time(clock) - pd.Timedelta(minutes=10))
    live = runner(Scripted([old]))
    live.step()
    assert broker.sent == [] and not live.store.seen("T-1")


def test_simulation_mode_sends_nothing(world):
    broker, clock, _, runner = world
    live = runner(Scripted([_intent(_signal_time(clock))]), simulate=True)
    live.step()
    assert broker.sent == []
    assert live.store.detail("T-1")["status"] == SIMULATED


def test_too_small_account_skips_the_trade(world):
    broker, clock, _, runner = world
    broker.account_state = replace(broker.account_state, equity=100.0, balance=100.0)
    live = runner(Scripted([_intent(_signal_time(clock))]))
    live.step()
    detail = live.store.detail("T-1")
    assert detail["status"] == SKIPPED and "volume" in detail["detail"]
    assert broker.sent == []


def test_daily_loss_closes_everything_and_blocks_new_entries(world):
    broker, clock, set_time, runner = world
    first = _signal_time(clock)
    live = runner(Scripted([_intent(first), _intent(first + pd.Timedelta(minutes=2), tag="T-2")]))
    live.step()
    broker.account_state = replace(broker.account_state, equity=9_750.0)  # -2,5 % dans la journée
    set_time(clock.now + pd.Timedelta(minutes=2))
    live.step()
    assert broker.positions() == []
    assert live.store.detail("T-2")["status"] == SKIPPED


def test_stale_feed_blocks_entries(world, rule):
    broker, clock, _, runner = world
    broker.ticks["XAUUSD"] = tick_at(rule, clock.now.to_pydatetime(), age_s=600)
    live = runner(Scripted([_intent(_signal_time(clock))]))
    live.step()
    assert broker.sent == [] and "flux" in live.store.detail("T-1")["detail"]


def _stop_update(time, sl=None, *, exit=False):
    return StopUpdate(time=pd.Timestamp(time), tag="T-1", sl=sl, exit=exit, reason="stop suiveur")


def test_trailing_stop_is_tightened_once_and_never_moved_away(world):
    broker, clock, set_time, runner = world
    first = _signal_time(clock)
    updates = [_stop_update(first + pd.Timedelta(minutes=1), 3995.004),
               _stop_update(first + pd.Timedelta(minutes=2), 3993.0)]  # fmt: skip
    live = runner(Scripted([_intent(first)], updates))
    live.step()
    set_time(clock.now + pd.Timedelta(minutes=1))
    live.step()
    set_time(clock.now + pd.Timedelta(seconds=20))
    live.step()  # même barre : aucune nouvelle requête
    (modify,) = [r for r in broker.sent if r["action"] == C.TRADE_ACTION_SLTP]
    assert modify["sl"] == 3995.0 and broker.positions()[0].sl == 3995.0  # arrondi vers la position
    assert live.store.with_status(OPEN)[0].sl == 3995.0  # SL reposé à ce niveau si jamais il disparaît
    set_time(clock.now + pd.Timedelta(minutes=1))
    live.step()  # 3993 éloignerait le stop : ignoré
    assert len([r for r in broker.sent if r["action"] == C.TRADE_ACTION_SLTP]) == 1


def test_stop_already_crossed_or_strategy_exit_closes_the_position(world):
    broker, clock, set_time, runner = world
    first = _signal_time(clock)
    live = runner(Scripted([_intent(first)], [_stop_update(first + pd.Timedelta(minutes=1), 4000.50)]))
    live.step()
    set_time(clock.now + pd.Timedelta(minutes=1))
    live.step()  # bid 4000.00 sous le nouveau stop : fermeture au marché
    assert broker.positions() == []

    broker2, clock2, set_time2, runner2 = world
    set_time2(clock2.now + pd.Timedelta(minutes=5))
    second = _signal_time(clock2)
    intent = OrderIntent(**{**_intent(second).__dict__, "tag": "T-2"})
    out = StopUpdate(time=second + pd.Timedelta(minutes=1), tag="T-2", sl=None, exit=True, reason="signal inverse")
    live2 = runner2(Scripted([intent], [out]))
    live2.step()
    assert len(broker2.positions()) == 1
    set_time2(clock2.now + pd.Timedelta(minutes=1))
    live2.step()
    assert broker2.positions() == []
