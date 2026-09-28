from dataclasses import replace

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.fake_broker import make_account, make_symbol
from goldbot.execution.test_order import TestOrderFailed, TestOrderRefused, run_test_order
from tests.conftest import OPEN_NOW, WEEKEND_NOW, tick_at


def _run(broker, settings, rule, schedule, *, account=None, spec=None, now=OPEN_NOW):
    return run_test_order(
        broker,
        spec=spec or make_symbol(),
        account=account or make_account(),
        magic=settings.bot.magic,
        config=settings.test_order,
        rule=rule,
        schedule=schedule,
        now_utc=now,
        sleep=lambda s: None,
    )


def _sltp_requests(broker):
    return [r for r in broker.sent if r["action"] == C.TRADE_ACTION_SLTP]


def test_full_cycle_on_demo(broker, settings, rule, schedule):
    report = _run(broker, settings, rule, schedule)
    tick = broker.ticks["XAUUSD"]
    # SL et TP envoyés dans la requête d'ouverture (règle 3).
    opening = broker.sent[0]
    assert opening["sl"] < tick.bid and opening["tp"] > tick.ask
    assert opening["magic"] == settings.bot.magic
    # SL resserré, jamais éloigné (règle 4), et le TP renvoyé pour ne pas l'effacer.
    assert report.sl_initial < report.sl_tightened < tick.bid
    (modification,) = _sltp_requests(broker)
    assert modification["tp"] == report.tp
    # Position fermée, deals d'entrée et de sortie lus.
    assert broker.positions() == []
    assert [d.entry for d in report.deals] == [C.DEAL_ENTRY_IN, C.DEAL_ENTRY_OUT]
    # 5 USD par lot et par côté, 0,01 lot : 0,05 USD à l'entrée et à la sortie.
    assert report.commission_in == pytest.approx(-0.05)
    assert report.commission_out == pytest.approx(-0.05)
    assert report.commission_per_lot_round_trip == pytest.approx(10.0)
    assert report.volume == 0.01


def test_sl_distance_respects_broker_stops_level(broker, settings, rule, schedule):
    spec = make_symbol(trade_stops_level=1000, trade_freeze_level=500)  # 10 USD + 5 USD
    _run(broker, settings, rule, schedule, spec=spec)
    tick = broker.ticks["XAUUSD"]
    assert tick.bid - broker.sent[0]["sl"] >= 15.0


def test_sl_is_never_loosened(broker, settings, rule, schedule, monkeypatch):
    # Le prix chute juste après l'entrée : resserrer violerait la distance minimale, on n'y touche pas.
    original_tick = broker.tick
    calls = {"n": 0}

    def tick(name):
        calls["n"] += 1
        current = original_tick(name)
        if calls["n"] >= 3:  # après le tick d'entrée et celui de l'envoi
            return replace(current, bid=current.bid - 4.9, ask=current.ask - 4.9)
        return current

    monkeypatch.setattr(broker, "tick", tick)
    report = _run(broker, settings, rule, schedule)
    assert report.sl_tightened is None
    assert _sltp_requests(broker) == []
    assert any("SL non resserré" in note for note in report.notes)
    assert broker.positions() == []


def test_refused_on_real_account(broker, settings, rule, schedule):
    with pytest.raises(TestOrderRefused, match="DÉMO"):
        _run(broker, settings, rule, schedule, account=make_account(trade_mode=C.ACCOUNT_TRADE_MODE_REAL))
    assert broker.sent == [] and broker.checked == []


def test_refused_on_contest_account(broker, settings, rule, schedule):
    with pytest.raises(TestOrderRefused):
        _run(broker, settings, rule, schedule, account=make_account(trade_mode=C.ACCOUNT_TRADE_MODE_CONTEST))
    assert broker.sent == []


def test_refused_when_market_closed(broker, settings, rule, schedule):
    broker.ticks["XAUUSD"] = tick_at(rule, WEEKEND_NOW, age_s=90_000)
    with pytest.raises(TestOrderRefused, match="marché fermé"):
        _run(broker, settings, rule, schedule, now=WEEKEND_NOW)
    assert broker.sent == []


def test_refused_in_netting_with_an_existing_position(broker, settings, rule, schedule):
    broker.order_send(
        {"action": C.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": 0.05, "type": C.ORDER_TYPE_SELL}
    )  # trade manuel
    netting = make_account(margin_mode=C.ACCOUNT_MARGIN_MODE_RETAIL_NETTING)
    with pytest.raises(TestOrderRefused, match="netting"):
        _run(broker, settings, rule, schedule, account=netting)
    assert len(broker.sent) == 1


def test_manual_positions_are_not_touched_in_hedging(broker, settings, rule, schedule):
    broker.order_send(
        {"action": C.TRADE_ACTION_DEAL, "symbol": "XAUUSD", "volume": 0.05, "type": C.ORDER_TYPE_SELL, "magic": 0}
    )
    manual = broker.positions()[0]
    _run(broker, settings, rule, schedule)
    assert broker.positions() == [manual]


def test_refused_before_any_order_when_opening_is_rejected(broker, settings, rule, schedule):
    broker.send_retcodes = [C.TRADE_RETCODE_NO_MONEY]
    with pytest.raises(TestOrderFailed, match="10019"):
        _run(broker, settings, rule, schedule)
    assert broker.positions() == []


def test_lost_reply_is_reconciled_and_position_closed(broker, settings, rule, schedule):
    broker.lose_next_reply = True  # ouverture exécutée, réponse perdue (TIMEOUT)
    report = _run(broker, settings, rule, schedule)
    assert broker.positions() == []
    assert report.deals and report.deals[-1].entry == C.DEAL_ENTRY_OUT


def test_close_failure_tells_to_close_manually(broker, settings, rule, schedule):
    broker.send_retcodes = [C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_MARKET_CLOSED]
    with pytest.raises(TestOrderFailed, match="à la main"):
        _run(broker, settings, rule, schedule)


def test_missing_position_is_reported(broker, settings, rule, schedule):
    broker.hide_positions = True
    with pytest.raises(TestOrderFailed, match="introuvable"):
        _run(broker, settings, rule, schedule)
