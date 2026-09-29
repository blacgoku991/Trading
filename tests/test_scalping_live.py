"""Expérience de scalping en direct, avec le broker factice."""

import json
import sqlite3
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import TICKS_DTYPE
from goldbot.broker.fake_broker import FakeBroker, make_account, make_bars, make_symbol, make_tick
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.scalping.cli import EXIT_OK, EXIT_REFUSED, live_main
from goldbot.scalping.engine import BREAKOUT, LONG, PULLBACK, Candle, Setup
from goldbot.scalping.live import ScalpRunner, strategy_versions
from goldbot.scalping.store import CLOSED, FAILED, NOT_SENT, OPEN, REFUSED, SENDING, SENT, ScalpStore
from tests.conftest import CONFIG_PATH, server_epoch_of

MAGIC = 20260929
BASE_MS = server_epoch_of("2026-01-06 12:00") * 1000  # mardi, 12:00 heure serveur (10:00 UTC)


class Clock:
    def __init__(self, server_ms):
        self.server_ms = server_ms

    def __call__(self):
        return pd.Timestamp(self.server_ms - 7_200_000, unit="ms", tz="UTC").to_pydatetime()  # hiver : UTC + 2 h

    def advance(self, seconds):
        self.server_ms += int(seconds * 1000)


def ticks(points, spread=0.16):
    """[(ms, prix médian), ...] -> tableau de ticks MT5."""
    array = np.zeros(len(points), TICKS_DTYPE)
    array["time_msc"] = [t for t, _ in points]
    array["time"] = array["time_msc"] // 1000
    array["bid"] = [round(mid - spread / 2, 2) for _, mid in points]
    array["ask"] = [round(mid + spread / 2, 2) for _, mid in points]
    return array


def range_then_breakout(start_ms, *, breakout=True):
    points = [(start_ms + k * 1000, 4000.00 + 0.02 * (k % 2)) for k in range(-120, 0)]
    if breakout:
        points += [(start_ms + 5_000 + k * 1000, 4000.40) for k in range(5)]
        points += [(start_ms + 10_000 + k * 1000, 4000.50) for k in range(6)]
    return ticks(points)


def make_world(tmp_path, settings, *, symbol=None, account=None):
    clock = Clock(BASE_MS + 1_500)
    broker = FakeBroker(
        account=account or make_account(balance=10_000.0, equity=10_000.0, margin_free=10_000.0),
        symbols=[symbol or make_symbol()],
    )
    broker.connect()
    # Barres M1 en hausse régulière (EMA20 au-dessus de l'EMA50), d'environ 1 $ d'amplitude (ATR M1).
    minutes = np.arange(BASE_MS // 1000 - 300 * 60, BASE_MS // 1000, 60)
    bars = make_bars(minutes, start_price=3990.0)
    bars["close"] = np.linspace(3990.0, 4000.0, len(minutes))
    bars["open"], bars["high"], bars["low"] = bars["close"], bars["close"] + 0.5, bars["close"] - 0.5
    broker.history_bars = bars
    broker.history_ticks = range_then_breakout(BASE_MS, breakout=False)

    def quote(mid, spread=0.16):
        broker.ticks["XAUUSD"] = make_tick(round(mid - spread / 2, 2), round(mid + spread / 2, 2), clock.server_ms)

    quote(4000.0)
    lines = []

    def runner(*, local_only=False, store=None):
        return ScalpRunner(
            broker,
            ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=lambda s: None),
            store or ScalpStore(tmp_path / "scalp.sqlite"),
            settings=settings,
            spec=broker.symbol("XAUUSD"),
            local_only=local_only,
            now_utc=clock,
            say=lines.append,
            sleep=lambda s: None,
        )

    return broker, clock, quote, runner, lines


@pytest.fixture
def world(tmp_path, settings):
    return make_world(tmp_path, settings)


def _breakout(broker, clock, quote, live):
    live.step()  # préchauffage : le range des 60 s est lu, sans trader
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()


def _second_breakout(broker, clock, quote, live):
    """Retour dans le range (réarmement) puis nouvelle cassure confirmée, 37 s après la première."""
    later = [(BASE_MS + 30_000 + k * 1000, 4000.10) for k in range(12)]
    later += [(BASE_MS + 42_000 + k * 1000, 4000.95) for k in range(5)]
    later += [(BASE_MS + 47_000 + k * 1000, 4001.05) for k in range(6)]
    broker.history_ticks = np.concatenate([broker.history_ticks, ticks(later)])
    clock.server_ms = BASE_MS + 53_500
    quote(4001.05)
    live.step()


def _deals(broker):
    return [r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL and "position" not in r]


def test_breakout_opens_a_demo_trade_with_its_server_stop_then_closes_at_max_duration(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    (position,) = broker.positions()
    assert position.magic == MAGIC and position.sl > 0 and position.tp > position.price_open
    (sent,) = _deals(broker)
    assert sent["sl"] == pytest.approx(3999.87) and sent["comment"] == f"SC-B-{BASE_MS + 10_000}-L#1"
    assert any("signal ACHAT [cassure v1 + apprentissage v1 + cadence v1 · " in line for line in lines)
    assert any("entrée démo [cassure v1" in line for line in lines)
    # 121 s plus tard : sortie forcée à la durée maximale, même si la position perd.
    clock.advance(121)
    quote(4000.30)
    live.step()
    assert broker.positions() == []
    live.step()  # la sortie est relevée dans les deals au passage suivant
    tag = sent["comment"].split("#")[0]
    assert live.store.signal(tag)["status"] == SENT
    order = live.store.order(tag, 1)
    assert order["status"] == CLOSED and order["exit_reason"] == "durée max" and order["real_pnl"] < 0
    assert order["est_pnl"] == pytest.approx(order["real_pnl"])  # spread compté une seule fois
    assert order["exit_ms"] - order["open_ms"] == 121_000
    assert any("sortie" in line and "après 121 s" in line and "frais" in line for line in lines)
    assert any("essai démo : réalisé" in line and "total" in line for line in lines)


def test_each_signal_keeps_its_strategy_version_and_parameters(world, settings):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    (sent,) = _deals(broker)
    row = live.store.signal(sent["comment"].split("#")[0])
    version, digest, params = strategy_versions(settings.scalping)[BREAKOUT]
    assert (row["strategy"], row["version"], row["params_hash"]) == (BREAKOUT, version, digest)
    assert row["key"] == "B+1@4000.02" and "cassure de 4000.02" in row["reason"]
    assert params["rules"]["lookback_s"] == 60 and params["common"]["target_ratio"] == 1.2
    # Une autre valeur d'un réglage donne une autre empreinte.
    changed = settings.scalping.model_copy(update={"target_ratio": 1.5})
    assert strategy_versions(changed)[BREAKOUT][1] != digest


def test_two_distinct_signals_in_the_same_minute_are_both_sent(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    _second_breakout(broker, clock, quote, live)  # autre niveau cassé, 37 s plus tard
    assert len(_deals(broker)) == 2 and len(broker.positions()) == 2
    assert live.store.status_counts() == {SENT: 2}


def test_position_limit_also_counts_the_simulated_trades(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    live.cfg = live.cfg.model_copy(update={"max_open_positions": 1})
    live.policy.cfg = live.cfg
    _breakout(broker, clock, quote, live)
    _second_breakout(broker, clock, quote, live)
    assert live.store.status_counts() == {NOT_SENT: 1, REFUSED: 1}
    assert live.set_aside == {"positions ouvertes au maximum": 1}


def _setup_at(candle_end_ms, *, key="B+1@4000.20"):
    candle = Candle(candle_end_ms - 5_000, 4000.40, 4000.50, 4000.40, 4000.50, 4000.42, 4000.58, 0.16, 5)
    return Setup(BREAKOUT, LONG, 4000.20, 3999.40, candle, key=key, reason="cassure de 4000.20 confirmée (test)")


def test_no_entry_when_the_market_closes_before_the_max_duration(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    near_break = server_epoch_of("2026-01-06 23:57") * 1000 + 30_000  # pause quotidienne à 23:59
    live.consider(_setup_at(near_break - 1_000), near_break)
    assert broker.sent == [] and live.store.status_counts() == {REFUSED: 1}
    assert live.set_aside == {"le marché ferme avant la durée max": 1}


def test_a_stale_signal_is_refused(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    live.consider(_setup_at(BASE_MS + 30_000), BASE_MS + 60_000)  # après un trou de connexion
    assert broker.sent == [] and live.store.status_counts() == {REFUSED: 1}
    assert live.set_aside == {"signal périmé": 1}


def test_the_same_signal_seen_twice_is_a_counted_duplicate_never_resent(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    clock.server_ms = BASE_MS + 6_000
    setup = _setup_at(BASE_MS + 5_000)
    live.consider(setup, clock.server_ms)
    live.consider(setup, clock.server_ms)  # même signal revu (relecture, relance…)
    assert len(_deals(broker)) == 1
    assert live.store.meta("doublons_evites") == "1"


def test_a_duplicate_position_on_the_broker_is_closed(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    (original,) = broker.positions()
    broker._positions[99_999] = replace(original, ticket=99_999, identifier=99_999)  # exécution en double
    live.step()
    assert [p.ticket for p in broker.positions()] == [original.ticket]
    assert any("doublon accidentel, fermeture" in line for line in lines)
    assert live.store.meta("doublons_evites") == "1"


def test_split_order_when_the_volume_exceeds_the_maximum_per_order(tmp_path, settings):
    broker, clock, quote, runner, lines = make_world(tmp_path, settings, symbol=make_symbol(volume_max=0.05))
    live = runner()
    _breakout(broker, clock, quote, live)
    sent = _deals(broker)
    tag = sent[0]["comment"].split("#")[0]
    assert [r["comment"] for r in sent] == [f"{tag}#1", f"{tag}#2", f"{tag}#3"]
    assert [r["volume"] for r in sent] == [0.05, 0.04, 0.04]  # 0,13 lot en trois ordres, un seul budget de risque
    assert live.store.signal(tag)["parts"] == 3 and live.store.status_counts() == {SENT: 1}
    assert any("en 3 ordres (fractionnement)" in line for line in lines)
    clock.advance(121)
    quote(4000.30)
    live.step()
    live.step()
    assert broker.positions() == []
    text = "\n".join(live.reports())
    assert "trades fermés 1 " in text and "trades fractionnés : 1" in text


def test_uncertain_reply_is_reconciled_with_the_positions_not_sent_twice(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    broker.lose_next_reply = True  # ordre exécuté, mais réponse perdue (TIMEOUT)
    _breakout(broker, clock, quote, live)
    assert any("réponse incertaine" in line for line in lines)
    live.step()
    (order,) = live.store.orders(OPEN)
    assert order.position == broker.positions()[0].ticket
    assert len(_deals(broker)) == 1  # ni renvoyé, ni fermé


def test_lost_reply_then_stop_hit_before_the_bot_sees_it_is_recovered_from_the_deals(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    broker.lose_next_reply = True  # ordre exécuté, réponse perdue (TIMEOUT)...
    opened = []
    send = broker.order_send

    def send_then_stop(request):
        result = send(request)
        for position in broker.positions():
            if position.comment == request.get("comment") and "position" not in request:
                opened.append(position)
                broker.hit_stop(position.ticket, position.sl)  # ...et stop touché avant que le bot la voie
        return result

    broker.order_send = send_then_stop
    _breakout(broker, clock, quote, live)
    (position,) = opened
    assert broker.positions() == [] and any("réponse incertaine" in line for line in lines)
    order = live.store.order(broker.sent[0]["comment"].split("#")[0], 1)
    assert order["status"] == CLOSED and order["exit_reason"] == "stop" and order["real_pnl"] < 0
    assert order["position"] == position.ticket and order["open_price"] == position.price_open
    assert any("retrouvé dans l'historique des deals" in line for line in lines)
    assert live.store.realized_since(0) == pytest.approx(order["real_pnl"])  # perte du jour et bilans
    assert list(live.policy.cadence.results) == [pytest.approx(order["real_pnl"])]  # compté une seule fois
    live.step()
    assert len(live.policy.cadence.results) == 1 and len(_deals(broker)) == 1  # jamais renvoyé


def test_an_uncertain_order_that_never_executed_fails_after_two_minutes(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    broker.send_retcodes = [C.TRADE_RETCODE_TIMEOUT]  # réponse incertaine, rien d'exécuté
    _breakout(broker, clock, quote, live)
    assert broker.positions() == [] and any("réponse incertaine" in line for line in lines)
    live.step()
    (order,) = live.store.orders(SENDING)  # pas encore jugé : ni position ni deal, moins de 2 minutes
    clock.advance(121)
    live.step()
    assert live.store.orders(SENDING) == [] and live.store.closed_orders() == []
    assert live.store.order(order.tag, 1)["detail"] == "envoi incertain, aucune position ni aucun deal trouvés"


def test_too_many_requests_pauses_sending(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    broker.send_retcodes = [C.TRADE_RETCODE_TOO_MANY_REQUESTS]
    _breakout(broker, clock, quote, live)
    assert broker.positions() == [] and live.store.status_counts() == {FAILED: 1}
    assert any("trop de requêtes" in line for line in lines)
    _second_breakout(broker, clock, quote, live)  # 37 s plus tard : encore dans la pause de 60 s
    assert any("broker saturé" in line for line in lines) and broker.positions() == []


def test_low_free_margin_refuses_the_entry(tmp_path, settings):
    account = make_account(balance=10_000.0, equity=10_000.0, margin_free=4_000.0)  # < 50 % de l'equity
    broker, clock, quote, runner, lines = make_world(tmp_path, settings, account=account)
    live = runner()
    _breakout(broker, clock, quote, live)
    assert broker.positions() == [] and live.store.status_counts() == {REFUSED: 1}
    assert any("marge libre insuffisante" in line for line in lines)


def test_daily_loss_stops_new_demo_entries_for_the_day(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    store = live.store
    store.record_signal("SC-B-1-L", strategy=BREAKOUT, time_ms=BASE_MS - 60_000, side=1, status=SENT)
    store.record_order("SC-B-1-L", 1, comment="SC-B-1-L#1", side=1, volume=0.1, sl=1.0, tp=2.0, risk=10.0,
                       spread=0.16, time_ms=BASE_MS - 60_000)  # fmt: skip
    store.update_order("SC-B-1-L", 1, status=CLOSED, real_pnl=-150.0, closed_ms=BASE_MS - 30_000)  # -1,5 %
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()
    assert _deals(broker) == [] and store.status_counts() == {SENT: 1, REFUSED: 1}
    assert any("perte du jour atteinte" in line for line in lines)


def test_in_simulation_mode_the_daily_loss_uses_the_simulated_results(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    live.step()
    live.store.record_sim("SC-B-1-L", BREAKOUT, BASE_MS - 60_000, 1, 4000.0, 3999.0, 4001.2, 1.0, False)
    live.store.close_sim("SC-B-1-L", 3998.5, "stop", -150.0, 0.0, BASE_MS - 30_000)
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()
    assert live.store.status_counts() == {REFUSED: 1}
    assert any("perte du jour atteinte" in line for line in lines)


def test_local_simulation_mode_sends_nothing(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    _breakout(broker, clock, quote, live)
    assert broker.sent == []
    assert live.store.status_counts() == {NOT_SENT: 1}
    assert any("mode --simulation" in line for line in lines)


def test_wide_spread_is_refused_with_its_reason(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50, spread=0.60)  # objectif trop petit face à ce spread
    live.step()
    assert broker.sent == []
    assert live.store.status_counts() == {REFUSED: 1}
    assert live.set_aside == {"objectif trop faible face au coût": 1}
    assert not any("refus" in line or "signal" in line for line in lines)  # écarté sans affichage


def test_pullback_signal_is_traded_live(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    path = np.concatenate(
        [np.linspace(4000.20, 4002.40, 15), np.linspace(4002.20, 4001.30, 10), np.linspace(4001.80, 4002.20, 10)]
    )
    points = [(BASE_MS + k * 1000, round(float(mid), 2)) for k, mid in enumerate(path)] + [(BASE_MS + 35_000, 4002.2)]
    broker.history_ticks = np.concatenate([broker.history_ticks, ticks(points)])
    for second in range(1, 36):  # un passage par seconde, comme en direct
        clock.server_ms = BASE_MS + second * 1000 + 500
        quote(float(path[min(second, len(path) - 1)]))
        live.step()
    # La même hausse a aussi déclenché la cassure : deux occasions distinctes, deux trades.
    (sent,) = [r for r in _deals(broker) if r["comment"].startswith("SC-P-")]
    assert sent["sl"] == pytest.approx(4001.30 - 0.08 - 0.05)
    assert any(
        "signal ACHAT [impulsion-repli v1 + apprentissage v1 + cadence v1 · " in line and "repli de 46 %" in line
        for line in lines
    )
    assert {live.store.signal(r["comment"].split("#")[0])["strategy"] for r in _deals(broker)} == {BREAKOUT, PULLBACK}


def test_restart_resumes_the_position_without_sending_again(world, tmp_path):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    live.store.close()
    restarted = runner(store=ScalpStore(tmp_path / "scalp.sqlite"))
    restarted.step()
    assert len(_deals(broker)) == 1
    assert len(restarted.store.orders(OPEN)) == 1


def test_missing_stop_is_put_back(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    ticket = broker.positions()[0].ticket
    broker._positions[ticket] = replace(broker._positions[ticket], sl=0.0)
    live.step()
    assert broker.positions()[0].sl == pytest.approx(3999.87)


def test_server_stop_hit_is_recorded_from_the_deals(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    broker.hit_stop(broker.positions()[0].ticket, 3999.87)
    live.step()
    (sent,) = _deals(broker)
    order = live.store.order(sent["comment"].split("#")[0], 1)
    assert order["status"] == CLOSED and order["exit_reason"] == "stop" and order["real_pnl"] < 0


def test_two_separate_reports(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    demo, simulation = "\n".join(live.reports()).split("=== Bilan 2")
    assert "Bilan 1 : exécutions démo" in demo and "simulation avec glissement supplémentaire" in simulation
    for block in (demo, simulation):
        assert (
            "[cassure v1 + apprentissage v1 + cadence v1 · " in block
            and "[impulsion-repli v1 + apprentissage v1 + cadence v1 · " in block
            and "[total]" in block
        )
        for text in ("gains réalisés", "pertes réalisées", "dont frais", "résultat total", "durée", "sorties"):
            assert text in block
    assert "positions ouvertes 1 (latent" in demo and "valeur du compte (equity" in demo
    assert "doublons accidentels évités : 0" in demo
    # Trade simulé ouvert à l'ask + 0,10 $ de glissement, valorisé au bid - 0,10 $ : latent négatif.
    assert "positions ouvertes 1 (latent -" in simulation and "valeur simulée du compte" in simulation
    status = live.status_line()
    assert "en marche (marché ouvert)" in status and "tendance M1 haussière" in status and "ATR M1" in status
    assert "1 envoyés" in status and "essai démo : réalisé" in status and "1 position(s) ouverte(s)" in status


def test_a_state_database_of_the_previous_version_is_archived_not_rewritten(tmp_path):
    path = tmp_path / "scalp.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE signals (tag TEXT PRIMARY KEY, time_ms INTEGER, side INTEGER, status TEXT)")
    old.execute("INSERT INTO signals VALUES ('SC-1-L', 1, 1, 'fermé')")
    old.commit()
    old.close()
    store = ScalpStore(path)
    assert store.archived is not None and store.archived.exists() and not store.has_activity()


def _teach(live, key, best_name, *, best=0.5, others=-0.5, count=20):
    """État d'apprentissage imposé : chaque variante jugée sur `count` trades, une seule gagnante (ou aucune)."""
    live.book.learner.state = {
        key: {
            name: [(best if name == best_name else others) * count, float(count), count]
            for name in live.book.learner.variants
        }
    }


def test_learning_can_play_a_buy_signal_as_a_sell(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _teach(live, "B+", "stop x1 / objectif 1.2 R / inversé")  # en ce moment, vendre les cassures haussières gagne
    _breakout(broker, clock, quote, live)
    (sent,) = _deals(broker)
    assert sent["type"] == C.ORDER_TYPE_SELL
    assert sent["sl"] == pytest.approx(4001.13) and sent["tp"] == pytest.approx(3999.57)  # stop au-dessus
    assert broker.positions()[0].type == C.POSITION_TYPE_SELL
    row = live.store.signal(sent["comment"].split("#")[0])
    assert row["side"] == LONG and row["variante"] == "stop x1 / objectif 1.2 R / inversé"
    assert any("VENTE (signal joué à l'envers)" in line for line in lines)
    assert any(
        "apprentissage : meilleure variante récente stop x1 / objectif 1.2 R / inversé" in line for line in lines
    )


def test_learning_skips_the_signal_when_every_variant_loses(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _teach(live, "B+", None)
    _breakout(broker, clock, quote, live)
    assert _deals(broker) == [] and live.store.status_counts() == {REFUSED: 1}
    assert live.set_aside == {"apprentissage": 1}
    (row,) = live.store._rows("SELECT detail FROM signals")
    assert row["detail"].startswith("apprentissage : toutes les variantes perdent en ce moment")


def test_learning_state_is_saved_after_each_simulated_trade_and_reloaded_after_a_restart(world, tmp_path):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    assert len(live.book.sims) == 16  # le signal est simulé avec toutes les variantes possibles
    clock.advance(121)
    quote(4000.30)
    live.step()  # durée maximale atteinte pour toutes les variantes : chaque résultat met à jour son score
    saved = json.loads(live.store.meta("apprentissage"))
    counts = {name: score[2] for name, score in saved["state"]["B+"].items()}
    assert len(counts) == 16 and set(counts.values()) == {1} and not live.book.sims
    live.store.close()
    restarted = runner(store=ScalpStore(tmp_path / "scalp.sqlite"))
    assert restarted.book.learner.state == live.book.learner.state


def test_reports_show_what_the_bot_learned(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _teach(live, "B+", "stop x2 / objectif 2 R")
    text = "\n".join(live.reports())
    assert "=== Apprentissage : score récent des variantes" in text
    assert "[cassure, achats] meilleure : stop x2 / objectif 2 R (+0.50 R) | règles de départ -0.50 R" in text


# --- lancement -----------------------------------------------------------------------------------

SECRETS = ("87654321", "s3cret-pass", "Axi-Demo-Server")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        f"MT5_LOGIN={SECRETS[0]}\nMT5_PASSWORD={SECRETS[1]}\nMT5_SERVER={SECRETS[2]}\nLIVE_TRADING=true\n",
        encoding="utf-8",
    )
    return path


# Réglages du dépôt avec les sorties de la v1 : ces tests sont écrits avec ces valeurs (voir tests/conftest.py).
_V1_EXITS = [
    ("min_stop_points: 200", "min_stop_points: 50"),
    ("target_ratio: 3.0", "target_ratio: 1.2"),
    ("max_hold_s: 600", "max_hold_s: 120"),
    ("version: 2                    # v1", "version: 1                    # v1"),
    ("target_ratios: [2.0, 3.0, 4.0]", "target_ratios: [0.8, 1.2, 2.0]"),
    ("    enabled: false\n    version: 1\n    impulse_atr", "    enabled: true\n    version: 1\n    impulse_atr"),
    ("  max_total_risk_pct: 2.0 ", "  max_total_risk_pct: 0.5 "),
    ("  daily_loss_pct: 2.0 ", "  daily_loss_pct: 1.0 "),
    ("    ceiling_total_risk_pct: 2.0 ", "    ceiling_total_risk_pct: 1.0 "),
    ("  max_open_positions: 20 ", "  max_open_positions: 5 "),
    ("  max_entries_per_minute: 12 ", "  max_entries_per_minute: 5 "),
    ("  risk_per_trade_pct: 1.0\n", "  risk_per_trade_pct: 0.1\n"),
    ("  lot_choices: [0.4, 0.3]\n", ""),
    ("résultat (PF 0,90 contre 0,91).\n    enabled: false\n", "résultat (PF 0,90 contre 0,91).\n    enabled: true\n"),
    ("la moins perdante des trois sur 7 ans de M1 (PF 0,88).\n    enabled: true\n",
     "la moins perdante des trois sur 7 ans de M1 (PF 0,88).\n    enabled: false\n"),
    ("à la demande de l'utilisateur.\n    enabled: false\n", "à la demande de l'utilisateur.\n    enabled: true\n"),
]


def _v1_config(tmp_path, text=None):
    text = text or CONFIG_PATH.read_text(encoding="utf-8")
    for old, new in _V1_EXITS:
        assert old in text, old
        text = text.replace(old, new)
    path = tmp_path / "settings_v1.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _launch(tmp_path, env_file, broker, clock, *args, config=None, sleep=None, max_steps=2):
    lines = []
    code = live_main(
        ["--config", str(config or _v1_config(tmp_path)), "--env", str(env_file), *args],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=clock,
        sleep=sleep or (lambda s: clock.advance(s)),
        echo=lines.append,
        max_steps=max_steps,
    )
    return code, "\n".join(lines)


def test_a_real_account_is_always_refused(tmp_path, env_file, world):
    broker, clock, *_ = world
    broker.account_state = replace(broker.account_state, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _launch(tmp_path, env_file, broker, clock)  # même avec LIVE_TRADING=true
    assert code == EXIT_REFUSED and "démo" in output
    assert broker.sent == []


def test_launch_registers_the_strategy_versions_and_shows_them(tmp_path, env_file, world, settings):
    broker, clock, *_ = world
    code, output = _launch(tmp_path, env_file, broker, clock)
    assert (
        code == EXIT_OK
        and "Stratégies (version · empreinte des réglages) : cassure v1 + apprentissage v1 + cadence v1 · " in output
    )
    assert "5 entrées par minute au plus" in output
    for secret in SECRETS:
        assert secret not in output
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    rows = store.versions()
    expected = strategy_versions(settings.scalping)
    assert {(r["strategy"], r["version"], r["params_hash"]) for r in rows} == {
        (code, version, digest) for code, (version, digest, _) in expected.items()
    }
    assert {r["strategy"] for r in rows} == {BREAKOUT, PULLBACK} and '"impulse_atr": 1.0' in rows[1]["params"]


def test_changed_settings_are_refused_during_an_experiment(tmp_path, env_file, world):
    broker, clock, quote, *_ = world
    code, _ = _launch(tmp_path, env_file, broker, clock)
    assert code == EXIT_OK
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    store.record_signal(
        "SC-B-1-L", strategy=BREAKOUT, time_ms=BASE_MS, side=1, status=REFUSED
    )  # l'expérience a commencé
    store.close()
    changed = tmp_path / "changed.yaml"
    changed.write_text(_v1_config(tmp_path).read_text(encoding="utf-8").replace("target_ratio: 1.2", "target_ratio: 1.5"),
                       encoding="utf-8")  # fmt: skip
    code, output = _launch(tmp_path, env_file, broker, clock, config=changed)
    assert code == EXIT_REFUSED and "réglages" in output
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience", config=changed)
    assert code == EXIT_OK and "archivée" in output


def test_verification_checks_open_stop_restart_and_max_duration(tmp_path, env_file, world):
    broker, clock, quote, *_ = world
    code, output = _launch(tmp_path, env_file, broker, clock, "--verification")
    assert code == EXIT_OK, output
    assert output.count("OK   ") == 7 and "ÉCHEC" not in output
    assert "ordre retrouvable par magic et commentaire" in output
    assert "durée max" in output
    broker.connect()  # le lanceur ferme la connexion en sortant
    assert broker.positions() == []
    (modify,) = [r for r in broker.sent if r["action"] == C.TRADE_ACTION_SLTP]
    opening = _deals(broker)[0]
    assert opening["comment"].startswith("SC-VERIF-") and opening["comment"].endswith("#1")
    assert modify["sl"] > opening["sl"]  # stop resserré, jamais éloigné
    # Le trade de test n'entre pas dans les résultats de l'expérience.
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    assert store.closed_orders() == [] and not store.has_activity() and store.realized_since(0) == 0.0


def test_verification_refuses_a_real_account(tmp_path, env_file, world):
    broker, clock, *_ = world
    broker.account_state = replace(broker.account_state, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _launch(tmp_path, env_file, broker, clock, "--verification")
    assert code == EXIT_REFUSED and broker.sent == []


def test_ctrl_c_closes_the_experiment_positions_then_prints_the_reports(tmp_path, env_file, world):
    broker, clock, quote, *_ = world
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if len(calls) == 1:  # après le préchauffage, la cassure arrive
            broker.history_ticks = range_then_breakout(BASE_MS)
            clock.server_ms = BASE_MS + 16_500
            quote(4000.50)
        elif len(calls) == 2:  # une position est ouverte : Ctrl+C
            raise KeyboardInterrupt
        else:
            clock.advance(seconds)

    code, output = _launch(tmp_path, env_file, broker, clock, sleep=sleep, max_steps=None)
    assert code == EXIT_OK, output
    assert "entrée démo" in output and "(arrêt du bot)" in output and "Bilan 1" in output
    broker.connect()
    assert broker.positions() == []
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    (trade,) = _deals(broker)
    tag, part = trade["comment"].split("#")
    assert store.order(tag, int(part))["exit_reason"] == "arrêt du bot"


def test_cadence_is_rebuilt_from_the_closed_trades_after_a_restart(world, tmp_path):
    broker, clock, quote, runner, lines = world
    store = ScalpStore(tmp_path / "scalp.sqlite")
    for k in range(30):  # 30 trades simulés fermés, tous gagnants
        tag = f"SC-B-{k}-L"
        store.record_sim(tag, "B", k * 1000, 1, 4000.0, 3999.0, 4001.2, 0.05, False)
        store.close_sim(tag, 4001.2, "objectif", 6.0, 0.0, k * 1000 + 500)
    live = runner(local_only=True, store=store)
    assert live.policy.cadence.level == 1 and len(live._cadence_fed) == 30
    assert "cadence palier 1 : 10 entrées/min, 10 positions, risque ouvert 1 %" in live.status_line()
    tag = "SC-B-99-L"  # une grosse perte : retour immédiat à la base, annoncé
    store.record_sim(tag, "B", 99_000, 1, 4000.0, 3999.0, 4001.2, 0.05, False)
    store.close_sim(tag, 3990.0, "stop", -500.0, 0.0, 99_500)
    live._update_cadence(live.server_now_ms())  # appelé après chaque trade fermé
    assert live.policy.cadence.level == 0
    assert any("cadence : retour à la base" in line for line in lines)


def test_a_split_trade_reaches_the_cadence_once_with_its_summed_result(tmp_path, settings):
    broker, clock, quote, runner, lines = make_world(tmp_path, settings, symbol=make_symbol(volume_max=0.05))
    live = runner()
    _breakout(broker, clock, quote, live)
    assert len(broker.positions()) == 3  # un trade en trois ordres
    tickets = sorted(p.ticket for p in broker.positions())
    broker.hit_stop(tickets[0], broker.positions()[0].sl)  # une seule part fermée
    clock.advance(2)
    live.step()
    assert len(live.policy.cadence.results) == 0  # trade pas fini : rien donné à la cadence
    clock.advance(121)
    quote(4000.30)
    live.step()
    live.step()
    assert broker.positions() == []
    (result,) = live.policy.cadence.results
    closed = [row for row in live.store.closed_orders() if row["tag"] == broker.sent[0]["comment"].split("#")[0]]
    assert len(closed) == 3 and result == pytest.approx(sum(row["real_pnl"] for row in closed))


def test_three_losses_in_a_row_pause_that_direction_and_say_so(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.cfg = live.cfg.model_copy(update={"loss_streak": 1, "loss_streak_pause_s": 600})
    live.policy.cfg = live.cfg
    _breakout(broker, clock, quote, live)
    (position,) = broker.positions()
    broker.hit_stop(position.ticket, position.sl)
    clock.advance(2)
    live.step()
    assert any("pause des achats pendant 10 min (1 pertes d'affilée)" in line for line in lines)
    assert "pause dans ce sens" in live.policy.refusal(live.server_now_ms(), key="x", risk=1.0, equity=10_000.0,
                                                       day_result=0.0, day_start_equity=10_000.0, open_trades=[],
                                                       side=1)  # fmt: skip


def test_in_simulation_mode_each_closed_simulated_trade_reaches_the_cadence(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    _breakout(broker, clock, quote, live)
    assert broker.sent == [] and len(live.shadow) == 1
    clock.advance(121)  # durée maximale : le trade simulé ferme au passage suivant
    quote(4000.30)
    live.step()
    assert live.shadow == {} and len(live.policy.cadence.results) == 1
    assert live.policy.cadence.results[0] == pytest.approx(live.store.closed_sims()[0]["pnl"])


def test_maximum_drawdown_stops_everything_until_a_manual_restart(world, tmp_path):
    broker, clock, quote, runner, lines = world
    store = ScalpStore(tmp_path / "scalp.sqlite")
    store.set_meta("start_equity", "10000.0")
    live = runner(local_only=True, store=store)
    store.record_sim("SC-B-1-L", "B", 1_000, 1, 4000.0, 3990.0, 4030.0, 1.0, False)
    store.close_sim("SC-B-1-L", 3990.0, "stop", -1_050.0, 0.0, 2_000)  # -10,5 % depuis le plus haut
    live.step()
    assert live.halted and "baisse de 10.5 %" in live.halted
    assert any("ARRÊT TOTAL" in line for line in lines) and "ARRÊT TOTAL" in live.status_line()
    _breakout(broker, clock, quote, live)
    assert live.shadow == {} and any("refus : arrêt total : drawdown maximal atteint" in line for line in lines)
    restarted = runner(local_only=True, store=ScalpStore(tmp_path / "scalp.sqlite"))
    assert restarted.halted == live.halted  # l'arrêt survit au redémarrage


def test_a_halted_experiment_refuses_to_start_without_a_new_experiment(tmp_path, env_file, world):
    broker, clock, *_ = world
    store_path = tmp_path / "project" / "data" / "scalp.sqlite"
    store_path.parent.mkdir(parents=True)
    store = ScalpStore(store_path)
    store.record_signal("SC-B-1-L", strategy=BREAKOUT, time_ms=BASE_MS - 60_000, side=1, status=SENT)
    store.set_meta("arret_total", "baisse de 10.5 % depuis le plus haut")
    store.close()
    code, output = _launch(tmp_path, env_file, broker, clock)
    assert code == EXIT_REFUSED and "arrêt total de l'expérience" in output and broker.sent == []
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience")
    assert code == EXIT_OK and "Ancienne expérience archivée" in output


def test_no_drawdown_measure_while_a_closed_position_is_not_recorded_yet(world, tmp_path):
    broker, clock, quote, runner, lines = world
    store = ScalpStore(tmp_path / "scalp.sqlite")
    store.set_meta("start_equity", "10000.0")
    live = runner(store=store)
    _breakout(broker, clock, quote, live)
    (position,) = broker.positions()
    broker._positions[position.ticket] = replace(position, profit=500.0)  # gain latent de 500
    store.set_meta("plus_haut_experience", "11400.0")
    live._check_drawdown(live.server_now_ms())
    assert not live.halted  # 10 500 contre 11 400 : -7,9 %
    broker._positions.pop(position.ticket)  # fermée au TP, sortie pas encore dans l'historique
    live._check_drawdown(live.server_now_ms())
    assert not live.halted  # sans la garde : 10 000 contre 11 400, -12,3 % et faux arrêt total


def test_after_a_halt_positions_are_closed_again_until_they_are_gone(world, tmp_path):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    live.halted = "test"
    broker.send_retcodes = [C.TRADE_RETCODE_MARKET_CLOSED]  # première fermeture refusée
    live._check_drawdown(live.server_now_ms())
    assert len(broker.positions()) == 1
    clock.advance(6)  # nouvel essai après 5 s
    quote(4000.40)
    live.step()
    assert broker.positions() == []


def test_halt_is_checked_before_the_entries_of_the_same_pass(world, tmp_path):
    broker, clock, quote, runner, lines = world
    store = ScalpStore(tmp_path / "scalp.sqlite")
    store.set_meta("start_equity", "10000.0")
    live = runner(local_only=True, store=store)
    live.step()  # préchauffage
    store.record_sim("SC-B-1-L", "B", 1_000, 1, 4000.0, 3990.0, 4030.0, 1.0, False)
    store.close_sim("SC-B-1-L", 3990.0, "stop", -1_050.0, 0.0, 2_000)  # -10,5 % avant ce passage
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()  # la cassure arrive dans le même passage que la mesure
    assert live.halted and live.shadow == {}
    assert any("refus : arrêt total" in line for line in lines)


def test_a_new_experiment_is_refused_while_orders_are_still_open(tmp_path, env_file, world):
    broker, clock, quote, runner, lines = world
    store_path = tmp_path / "project" / "data" / "scalp.sqlite"
    store_path.parent.mkdir(parents=True)
    store = ScalpStore(store_path)
    store.record_signal("SC-B-1-L", strategy=BREAKOUT, time_ms=BASE_MS - 60_000, side=1, status=SENT)
    store.record_order("SC-B-1-L", 1, comment="SC-B-1-L#1", side=1, volume=0.02, sl=3990.0, tp=4010.0, risk=5.0,
                       spread=0.16, time_ms=BASE_MS - 60_000)  # fmt: skip
    store.update_order("SC-B-1-L", 1, status=OPEN, position=999)
    store.close()
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience")
    # Position introuvable et aucun deal : résultat inconnu, noté comme tel, puis archive.
    assert code == EXIT_OK and "résultat inconnu" in output and "Ancienne expérience archivée" in output


def test_a_new_experiment_first_closes_and_records_the_open_trades(tmp_path, env_file, world):
    broker, clock, quote, runner, lines = world
    store_path = tmp_path / "project" / "data" / "scalp.sqlite"
    store_path.parent.mkdir(parents=True)
    live = runner(store=ScalpStore(store_path))
    _breakout(broker, clock, quote, live)
    (position,) = broker.positions()
    live.store.close()
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience")
    assert code == EXIT_OK and "fermeture des ordres restants" in output and "archivée" in output
    assert position.ticket not in broker._positions  # fermée (le broker factice est déconnecté après le lancement)
    (archive,) = store_path.parent.glob("scalp_*.sqlite")
    (row,) = ScalpStore(archive).closed_orders()
    assert row["real_pnl"] is not None and row["exit_reason"] == "fin de l'expérience"  # résultat gardé


def test_a_new_experiment_is_refused_while_positions_cannot_be_closed(tmp_path, env_file, world):
    broker, clock, quote, runner, lines = world
    store_path = tmp_path / "project" / "data" / "scalp.sqlite"
    store_path.parent.mkdir(parents=True)
    live = runner(store=ScalpStore(store_path))
    _breakout(broker, clock, quote, live)
    live.store.close()
    broker.send_retcodes = [C.TRADE_RETCODE_MARKET_CLOSED] * 1000
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience")
    assert code == EXIT_REFUSED and "encore ouverte" in output and len(broker._positions) == 1
    assert list(store_path.parent.glob("scalp_*.sqlite")) == []  # rien d'archivé


def test_live_direction_filter_refuses_when_the_day_has_no_clear_move(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.cfg = live.cfg.model_copy(update={"direction_filter": True})
    live.policy.cfg = live.cfg
    _breakout(broker, clock, quote, live)  # 5 heures de barres seulement : pas d'ATR journalier, pas de sens
    assert broker.positions() == [] and live.direction == 0
    assert live.set_aside == {"sens": 1} and not any("refus" in line for line in lines)
    assert "sens du jour : aucun trade" in live.status_line()


def test_live_market_read_decides_the_side_and_announces_its_changes(world, monkeypatch):
    from goldbot.config import ScalpMarketReadConfig
    from goldbot.scalping import market_read

    broker, clock, quote, runner, lines = world
    read = {"side": -1}
    monkeypatch.setitem(market_read.MODELS, "test", lambda b, **p: np.full(len(b), read["side"]))
    live = runner()
    live.cfg = live.cfg.model_copy(update={"market_read": ScalpMarketReadConfig(enabled=True, model="test")})
    live.policy.cfg = live.cfg
    _breakout(broker, clock, quote, live)  # la cassure est un achat ; la lecture dit « vendeur »
    assert broker.positions() == [] and live.direction == -1
    assert live.set_aside == {"lecture du marché": 1} and not any("refus" in line for line in lines)
    assert "lecture du marché : VENDEUR (ventes)" in live.status_line()
    read["side"] = 1  # la minute suivante, les bougies réagissent à la hausse
    clock.server_ms += 60_000
    live._refresh_context(clock.server_ms)
    assert live.direction == 1
    assert any(line.endswith("lecture du marché : ACHETEUR (achats)") for line in lines)


def test_strategy_label_names_the_market_read():
    from goldbot.config import ScalpMarketReadConfig
    from goldbot.scalping.live import strategy_label

    from tests.conftest import v1_exit_settings

    cfg = v1_exit_settings().scalping
    cfg = cfg.model_copy(update={"market_read": ScalpMarketReadConfig(enabled=True, model="reaction", version=2)})
    label = strategy_label("B", cfg, 1, "abcd1234")
    assert label.startswith("cassure v1") and "+ lecture reaction v2 · abcd1234" in label


def test_account_limit_refusals_are_shown_once_per_15_minutes_and_rule_refusals_never(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    candle = Candle(BASE_MS, 4000.0, 4000.1, 3999.9, 4000.0, 3999.9, 4000.1, 0.2, 5)
    setup = Setup(BREAKOUT, LONG, 4000.0, 3999.0, candle, key="B+1@4000.00", reason="test")
    live._signal_line = "signal ACHAT test"
    live._refuse(setup, 0.2, "stop trop proche : 9.5 pips < 20.0 pips")
    assert lines == [] and live.set_aside == {"stop trop proche": 1}
    for k in range(3):  # trois signaux en 10 minutes, tous refusés par la limite du jour
        later = replace(candle, start_ms=BASE_MS + k * 300_000)
        live._signal_line = f"signal ACHAT {k}"
        live._refuse(replace(setup, candle=later), 0.2, "perte du jour atteinte : -101.00, limite -2 %")
    shown = [line for line in lines if "refus" in line]
    assert len(shown) == 1 and "perte du jour atteinte" in shown[0] and lines[0] == "signal ACHAT 0"
    live._signal_line = "signal ACHAT 4"
    live._refuse(replace(setup, candle=replace(candle, start_ms=BASE_MS + 900_000)), 0.2, "perte du jour atteinte : x")
    assert len([line for line in lines if "refus" in line]) == 2  # 15 minutes plus tard : rappel


def test_two_candles_send_one_position_per_target_with_the_same_server_stop(tmp_path, settings):
    from goldbot.config import ScalpTwoCandleConfig

    scalping = settings.scalping.model_copy(update={
        "breakout": settings.scalping.breakout.model_copy(update={"enabled": False}),
        "pullback": settings.scalping.pullback.model_copy(update={"enabled": False}),
        "two_candles": ScalpTwoCandleConfig(enabled=True, min_stop_pips=10.0, target_pips=[20.0, 25.0, 30.0]),
    })  # fmt: skip
    broker, clock, quote, runner, lines = make_world(tmp_path, settings.model_copy(update={"scalping": scalping}))
    live = runner()
    live.step()  # préchauffage
    points = [(BASE_MS + k * 1000, 4001.0 - k / 60) for k in range(60)]  # minute baissière : 4001 -> 4000
    points += [(BASE_MS + 60_000 + k * 1000, 3999.9 + k / 100) for k in range(60)]  # haussière : 3999.9 -> 4000.49
    broker.history_ticks = np.concatenate([broker.history_ticks, ticks(points)])
    clock.server_ms = BASE_MS + 121_500
    quote(4000.50)
    live.step()
    sent = _deals(broker)
    assert len(sent) == 3 and {r["type"] for r in sent} == {C.ORDER_TYPE_SELL}
    assert [r["sl"] for r in sent] == pytest.approx([4001.42] * 3)  # stop commun, élargi à 10 pips
    assert [r["tp"] for r in sent] == pytest.approx([3998.42, 3997.92, 3997.42])  # 20 / 25 / 30 pips
    assert [r["comment"][-2:] for r in sent] == ["#1", "#2", "#3"] and len(broker.positions()) == 3
    assert any("en 3 positions" in line and "objectifs 3998.42 / 3997.92 / 3997.42" in line for line in lines)
    assert live.labels["R"].startswith("deux bougies v1")
