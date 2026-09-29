import pytest

from goldbot.broker import mt5_constants as C
from goldbot.execution.orders import (
    OrderRejected,
    describe_retcode,
    filling_candidates,
    round_to_tick,
    select_filling,
    send_market_order,
)

BUY_REQUEST = {
    "action": C.TRADE_ACTION_DEAL,
    "symbol": "XAUUSD",
    "volume": 0.01,
    "type": C.ORDER_TYPE_BUY,
    "sl": 4140.0,
    "tp": 4160.0,
    "magic": 1,
    "comment": "t",
}


@pytest.mark.parametrize(
    ("price", "tick", "digits", "mode", "expected"),
    [
        (4150.255, 0.01, 2, "nearest", 4150.26),
        (4150.255, 0.01, 2, "down", 4150.25),
        (4150.251, 0.01, 2, "up", 4150.26),
        (4150.25, 0.01, 2, "down", 4150.25),  # déjà sur la grille : inchangé malgré les flottants
        (0.1 + 0.2, 0.1, 1, "nearest", 0.3),
        (1.03, 0.05, 2, "nearest", 1.05),
        (1.03, 0.05, 2, "down", 1.0),
    ],
)
def test_round_to_tick(price, tick, digits, mode, expected):
    assert round_to_tick(price, tick, digits, mode) == expected


def test_filling_candidates_follow_the_symbol_mask():
    both = C.SYMBOL_FILLING_FOK | C.SYMBOL_FILLING_IOC
    assert filling_candidates(both) == [C.ORDER_FILLING_FOK, C.ORDER_FILLING_IOC, C.ORDER_FILLING_RETURN]
    assert filling_candidates(C.SYMBOL_FILLING_IOC) == [C.ORDER_FILLING_IOC, C.ORDER_FILLING_RETURN]
    assert filling_candidates(0) == [C.ORDER_FILLING_RETURN]


def test_select_filling_skips_unsupported_modes(broker):
    broker.check_retcodes = {C.ORDER_FILLING_FOK: C.TRADE_RETCODE_INVALID_FILL}
    filling, result = select_filling(broker, BUY_REQUEST, C.SYMBOL_FILLING_FOK | C.SYMBOL_FILLING_IOC)
    assert filling == C.ORDER_FILLING_IOC
    assert result.retcode == C.ORDER_CHECK_OK


def test_select_filling_stops_on_other_refusals(broker):
    broker.check_retcodes = {C.ORDER_FILLING_FOK: C.TRADE_RETCODE_NO_MONEY}
    with pytest.raises(OrderRejected, match="10019"):
        select_filling(broker, BUY_REQUEST, C.SYMBOL_FILLING_FOK | C.SYMBOL_FILLING_IOC)
    assert len(broker.checked) == 1


def test_select_filling_fails_when_no_mode_is_accepted(broker):
    broker.check_retcodes = {mode: C.TRADE_RETCODE_INVALID_FILL for mode in range(4)}
    with pytest.raises(OrderRejected, match="aucun mode"):
        select_filling(broker, BUY_REQUEST, C.SYMBOL_FILLING_FOK)


def test_send_uses_current_ask_for_a_buy(broker):
    result = send_market_order(broker, BUY_REQUEST, [C.ORDER_FILLING_FOK], sleep=lambda s: None)
    assert result.retcode == C.TRADE_RETCODE_DONE
    assert broker.sent[0]["price"] == broker.ticks["XAUUSD"].ask


def test_send_falls_back_on_invalid_fill(broker):
    broker.send_retcodes = [C.TRADE_RETCODE_INVALID_FILL]
    fillings = [C.ORDER_FILLING_FOK, C.ORDER_FILLING_IOC]
    result = send_market_order(broker, BUY_REQUEST, fillings, sleep=lambda s: None)
    assert result.retcode == C.TRADE_RETCODE_DONE
    assert [r["type_filling"] for r in broker.sent] == fillings


def test_send_retries_requotes_a_limited_number_of_times(broker):
    broker.send_retcodes = [C.TRADE_RETCODE_REQUOTE, C.TRADE_RETCODE_PRICE_CHANGED]
    assert send_market_order(broker, BUY_REQUEST, [C.ORDER_FILLING_FOK], sleep=lambda s: None).retcode == 10009
    broker.send_retcodes = [C.TRADE_RETCODE_REQUOTE] * 5
    with pytest.raises(OrderRejected, match="10004"):
        send_market_order(broker, BUY_REQUEST, [C.ORDER_FILLING_FOK], max_price_retries=3, sleep=lambda s: None)


def test_send_never_blindly_retries_uncertain_results(broker):
    broker.send_retcodes = [C.TRADE_RETCODE_TIMEOUT]
    with pytest.raises(OrderRejected, match="10012"):
        send_market_order(broker, BUY_REQUEST, [C.ORDER_FILLING_FOK], sleep=lambda s: None)
    assert len(broker.sent) == 1


def test_describe_retcode():
    assert "Algo Trading" in describe_retcode(C.TRADE_RETCODE_CLIENT_DISABLES_AT)
    assert "code inconnu" in describe_retcode(12345)


def test_an_order_filled_in_several_deals_is_recovered_with_its_total_volume_and_average_price():
    from goldbot.broker.base import Deal
    from goldbot.execution.orders import find_entry_deals

    def deal(ticket, volume, price, comment="SC-B-1-L#1", magic=20260929, entry=C.DEAL_ENTRY_IN):
        return Deal(ticket=ticket, order=5, position_id=5, symbol="XAUUSD", type=C.DEAL_TYPE_BUY, entry=entry,
                    reason=C.DEAL_REASON_EXPERT, volume=volume, price=price, commission=-0.1, swap=0.0, fee=0.0,
                    profit=0.0, magic=magic, comment=comment, time_msc=1_000_000)  # fmt: skip

    class History:
        def deals_between(self, start, end):
            return [deal(1, 0.06, 4000.00), deal(2, 0.04, 4000.50), deal(3, 0.1, 3990.0, entry=C.DEAL_ENTRY_OUT),
                    deal(4, 0.1, 4000.0, magic=0), deal(5, 0.1, 4000.0, comment="autre#1")]  # fmt: skip

    found = find_entry_deals(History(), magic=20260929, comments={"SC-B-1-L#1"}, sent_s=1_000, now_s=2_000)
    (recovered,) = found.values()
    assert recovered.volume == pytest.approx(0.10) and recovered.price == pytest.approx(4000.20)
    assert recovered.commission == pytest.approx(-0.2) and recovered.position_id == 5
