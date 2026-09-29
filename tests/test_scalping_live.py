"""Expérience de scalping en direct, avec le broker factice."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import TICKS_DTYPE
from goldbot.broker.fake_broker import FakeBroker, make_account, make_bars, make_tick
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.scalping.cli import EXIT_OK, EXIT_REFUSED, live_main
from goldbot.scalping.engine import LONG, Candle, Setup
from goldbot.scalping.live import ScalpRunner
from goldbot.scalping.store import CLOSED, NOT_SENT, OPEN, REFUSED, ScalpStore
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


@pytest.fixture
def world(tmp_path, settings):
    clock = Clock(BASE_MS + 1_500)
    broker = FakeBroker(account=make_account(balance=10_000.0, equity=10_000.0, margin_free=10_000.0))
    broker.connect()
    # Barres M1 en hausse régulière : EMA20 au-dessus de l'EMA50.
    minutes = np.arange(BASE_MS // 1000 - 300 * 60, BASE_MS // 1000, 60)
    bars = make_bars(minutes, start_price=3990.0)
    bars["close"] = np.linspace(3990.0, 4000.0, len(minutes))
    broker.history_bars = bars
    broker.history_ticks = range_then_breakout(BASE_MS, breakout=False)

    def quote(mid, spread=0.16):
        broker.ticks["XAUUSD"] = make_tick(round(mid - spread / 2, 2), round(mid + spread / 2, 2), clock.server_ms)

    quote(4000.0)

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

    lines = []
    return broker, clock, quote, runner, lines


def _breakout(broker, clock, quote, live):
    live.step()  # préchauffage : le range des 60 s est lu, sans trader
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()


def _second_breakout(broker, clock, quote, live):
    """Retour dans le range (réarmement) puis nouvelle cassure confirmée, 30 s après la première."""
    live.last_entry_ms = -(2**62)  # délai entre deux entrées écoulé
    later = [(BASE_MS + 30_000 + k * 1000, 4000.10) for k in range(12)]
    later += [(BASE_MS + 42_000 + k * 1000, 4000.95) for k in range(5)]
    later += [(BASE_MS + 47_000 + k * 1000, 4001.05) for k in range(6)]
    broker.history_ticks = np.concatenate([broker.history_ticks, ticks(later)])
    clock.server_ms = BASE_MS + 53_500
    quote(4001.05)
    live.step()


def test_breakout_opens_a_demo_trade_with_its_server_stop_then_closes_at_max_duration(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    (position,) = broker.positions()
    assert position.magic == MAGIC and position.sl > 0 and position.tp > position.price_open
    (sent,) = [r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL]
    assert sent["sl"] == pytest.approx(3999.87) and sent["comment"].startswith("SC-")
    assert any("signal ACHAT" in line for line in lines) and any("entrée démo" in line for line in lines)
    # 121 s plus tard : sortie forcée à la durée maximale, même si la position perd.
    clock.advance(121)
    quote(4000.30)
    live.step()
    assert broker.positions() == []
    live.step()  # la sortie est relevée dans les deals au passage suivant
    row = live.store.signal(sent["comment"])
    assert row["status"] == CLOSED and row["exit_reason"] == "durée max" and row["real_pnl"] < 0
    assert row["est_pnl"] == pytest.approx(row["real_pnl"])  # spread compté une seule fois
    assert any("sortie" in line and "après 121 s" in line and "exécuté" in line for line in lines)


def test_second_signal_within_the_unverified_cadence_is_only_simulated(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    _second_breakout(broker, clock, quote, live)  # 37 s après le premier ordre : cadence de 60 s non écoulée
    assert live.store.status_counts() == {OPEN: 1, NOT_SENT: 1}
    assert len(broker.positions()) == 1  # un seul ordre envoyé au broker
    assert any("cadence d'envoi Axi non vérifiée" in line for line in lines)


def test_position_limit_also_counts_the_simulated_trades(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    live.cfg = live.cfg.model_copy(update={"max_open_positions": 1})
    _breakout(broker, clock, quote, live)
    _second_breakout(broker, clock, quote, live)
    assert live.store.status_counts() == {NOT_SENT: 1, REFUSED: 1}
    assert any("1 positions déjà ouvertes" in line for line in lines)


def _signal_at(live, now_ms, *, candle_end_ms):
    candle = Candle(candle_end_ms - 5_000, 4000.40, 4000.50, 4000.40, 4000.50, 4000.42, 4000.58, 0.16, 5)
    live.recent.append(candle)
    live._handle(Setup(LONG, 4000.20, candle, f"SC-{candle.start_ms}-L"), now_ms)


def test_no_entry_when_the_market_closes_before_the_max_duration(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    near_break = server_epoch_of("2026-01-06 23:57") * 1000 + 30_000  # pause quotidienne à 23:59
    _signal_at(live, near_break, candle_end_ms=near_break - 1_000)
    assert broker.sent == [] and live.store.status_counts() == {REFUSED: 1}
    assert any("le marché ferme avant la durée max" in line for line in lines)


def test_a_stale_signal_is_refused(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    _signal_at(live, BASE_MS + 60_000, candle_end_ms=BASE_MS + 30_000)  # après un trou de connexion
    assert broker.sent == [] and live.store.status_counts() == {REFUSED: 1}
    assert any("signal périmé" in line for line in lines)


def test_daily_loss_stops_new_demo_entries_for_the_day(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    live.step()
    live.store.record("SC-old-L", BASE_MS - 60_000, 1, CLOSED, real_pnl=-150.0, closed_ms=BASE_MS - 30_000)
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()
    assert broker.sent == [] and live.store.status_counts() == {CLOSED: 1, REFUSED: 1}  # -1,5 % sur 10 000
    assert any("perte du jour atteinte (1 %) : démo" in line for line in lines)


def test_in_simulation_mode_the_daily_loss_uses_the_simulated_results(world):
    broker, clock, quote, runner, lines = world
    live = runner(local_only=True)
    live.step()
    live.store.record_sim("SC-old-L", BASE_MS - 60_000, 1, 4000.0, 3999.0, 4001.2, 1.0, False)
    live.store.close_sim("SC-old-L", 3998.5, "stop", -150.0, BASE_MS - 30_000)
    broker.history_ticks = range_then_breakout(BASE_MS)
    clock.server_ms = BASE_MS + 16_500
    quote(4000.50)
    live.step()
    assert live.store.status_counts() == {REFUSED: 1}
    assert any("perte du jour atteinte (1 %) : simulation" in line for line in lines)


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
    assert any("refus : objectif trop faible" in line for line in lines)


def test_restart_resumes_the_position_without_sending_again(world, tmp_path):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    live.store.close()
    restarted = runner(store=ScalpStore(tmp_path / "scalp.sqlite"))
    restarted.step()
    assert len([r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL]) == 1
    assert len(restarted.store.demo_trades(OPEN)) == 1


def test_uncertain_reply_is_reconciled_with_the_positions_not_sent_twice(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    broker.lose_next_reply = True  # ordre exécuté, mais réponse perdue (TIMEOUT)
    _breakout(broker, clock, quote, live)
    assert any("réponse incertaine" in line for line in lines)
    live.step()
    (trade,) = live.store.demo_trades(OPEN)
    assert trade.position == broker.positions()[0].ticket
    assert len([r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL]) == 1  # ni renvoyé, ni fermé


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
    (row,) = [live.store.signal(t) for t in [r["comment"] for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL]]
    assert row["status"] == CLOSED and row["exit_reason"] == "stop" and row["real_pnl"] < 0


def test_two_separate_reports(world):
    broker, clock, quote, runner, lines = world
    live = runner()
    _breakout(broker, clock, quote, live)
    demo, simulation = "\n".join(live.reports()).split("=== Bilan 2")
    assert "Bilan 1 : exécutions démo" in demo and "simulation avec glissement supplémentaire" in simulation
    for block in (demo, simulation):
        assert "positions ouvertes : 1" in block and "gains fermés" in block and "pertes fermées" in block
        assert "valeur du compte" in block
    # Trade simulé ouvert à l'ask + 0,10 $ de glissement, valorisé au bid - 0,10 $ : latent négatif.
    assert "latent -" in simulation
    status = live.status_line()
    assert "en marche (marché ouvert)" in status and "tendance M1 haussière" in status
    assert "1 envoyés" in status and "positions démo ouvertes : 1" in status


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


def _launch(tmp_path, env_file, broker, clock, *args, config=CONFIG_PATH):
    lines = []
    code = live_main(
        ["--config", str(config), "--env", str(env_file), *args],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=clock,
        sleep=lambda s: clock.advance(s),
        echo=lines.append,
        max_steps=2,
    )
    return code, "\n".join(lines)


def test_a_real_account_is_always_refused(tmp_path, env_file, world):
    broker, clock, *_ = world
    broker.account_state = replace(broker.account_state, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _launch(tmp_path, env_file, broker, clock)  # même avec LIVE_TRADING=true
    assert code == EXIT_REFUSED and "démo" in output
    assert broker.sent == []


def test_changed_settings_are_refused_during_an_experiment(tmp_path, env_file, world):
    broker, clock, quote, *_ = world
    code, _ = _launch(tmp_path, env_file, broker, clock)
    assert code == EXIT_OK
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    store.record("SC-x", BASE_MS, 1, REFUSED, detail="test")  # l'expérience a commencé
    store.close()
    changed = tmp_path / "changed.yaml"
    changed.write_text(CONFIG_PATH.read_text(encoding="utf-8").replace("target_ratio: 1.2", "target_ratio: 1.5"),
                       encoding="utf-8")  # fmt: skip
    code, output = _launch(tmp_path, env_file, broker, clock, config=changed)
    assert code == EXIT_REFUSED and "réglages" in output
    code, output = _launch(tmp_path, env_file, broker, clock, "--nouvelle-experience", config=changed)
    assert code == EXIT_OK and "archivée" in output


def test_verification_checks_open_stop_restart_and_max_duration(tmp_path, env_file, world):
    broker, clock, quote, *_ = world
    code, output = _launch(tmp_path, env_file, broker, clock, "--verification")
    assert code == EXIT_OK, output
    assert output.count("OK   ") == 6 and "ÉCHEC" not in output
    assert "durée max" in output
    broker.connect()  # le lanceur ferme la connexion en sortant
    assert broker.positions() == []
    (modify,) = [r for r in broker.sent if r["action"] == C.TRADE_ACTION_SLTP]
    opening = next(r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL)
    assert modify["sl"] > opening["sl"]  # stop resserré, jamais éloigné
    # Le trade de test n'entre pas dans les résultats de l'expérience.
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    assert store.closed_demo_results() == [] and not store.has_activity()


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

    lines = []
    code = live_main(
        ["--config", str(CONFIG_PATH), "--env", str(env_file)],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=clock,
        sleep=sleep,
        echo=lines.append,
    )
    output = "\n".join(lines)
    assert code == EXIT_OK, output
    assert "entrée démo" in output and "(arrêt du bot)" in output and "Bilan 1" in output
    broker.connect()
    assert broker.positions() == []
    store = ScalpStore(tmp_path / "project" / "data" / "scalp.sqlite")
    (trade,) = [r for r in broker.sent if r["action"] == C.TRADE_ACTION_DEAL and "position" not in r]
    row = store.signal(trade["comment"])
    assert row["status"] == CLOSED and row["exit_reason"] == "arrêt du bot"
