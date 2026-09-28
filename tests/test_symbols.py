import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.fake_broker import FakeBroker, make_symbol, match_group
from goldbot.broker.symbols import SymbolSelectionError, choose_gold_symbol, resolve_gold_symbol
from goldbot.config import SymbolConfig

AUTO = SymbolConfig(name="auto", search_group="*XAU*,*GOLD*")


def test_picks_the_single_tradable_gold_usd_symbol():
    candidates = [
        make_symbol("XAUEUR", currency_profit="EUR"),
        make_symbol("XAUUSD"),
        make_symbol("XAGUSD", currency_base="XAG"),
    ]
    assert choose_gold_symbol(candidates).name == "XAUUSD"


def test_pro_suffix_is_detected_when_plain_symbol_is_disabled():
    candidates = [make_symbol("XAUUSD", trade_mode=C.SYMBOL_TRADE_MODE_DISABLED), make_symbol("XAUUSD.pro")]
    assert choose_gold_symbol(candidates).name == "XAUUSD.pro"


def test_gold_named_symbol_is_detected():
    assert choose_gold_symbol([make_symbol("GOLD", currency_base="XAU")]).name == "GOLD"


def test_several_tradable_candidates_require_an_explicit_choice():
    with pytest.raises(SymbolSelectionError, match="XAUUSD, XAUUSD.pro"):
        choose_gold_symbol([make_symbol("XAUUSD"), make_symbol("XAUUSD.pro")])


def test_no_candidate_is_an_explicit_error():
    with pytest.raises(SymbolSelectionError, match="symbol.name"):
        choose_gold_symbol([make_symbol("XAUEUR", currency_profit="EUR")])


def test_resolve_selects_the_symbol_in_market_watch():
    broker = FakeBroker(symbols=[make_symbol("XAUUSD"), make_symbol("XAUEUR", currency_profit="EUR")])
    broker.connect()
    spec, candidates = resolve_gold_symbol(broker, AUTO)
    assert spec.name == "XAUUSD"
    assert {s.name for s in candidates} == {"XAUUSD", "XAUEUR"}
    assert "XAUUSD" in broker.selected


def test_resolve_explicit_name():
    broker = FakeBroker(symbols=[make_symbol("XAUUSD"), make_symbol("XAUUSD.pro")])
    broker.connect()
    spec, _ = resolve_gold_symbol(broker, SymbolConfig(name="XAUUSD.pro", search_group="*XAU*"))
    assert spec.name == "XAUUSD.pro"


def test_resolve_explicit_name_that_cannot_trade():
    broker = FakeBroker(symbols=[make_symbol("XAUUSD", trade_mode=C.SYMBOL_TRADE_MODE_CLOSEONLY)])
    broker.connect()
    with pytest.raises(SymbolSelectionError, match="pas ouvert au trading"):
        resolve_gold_symbol(broker, SymbolConfig(name="XAUUSD", search_group="*XAU*"))


def test_group_filter_semantics():
    names = ["XAUUSD", "XAUEUR", "GOLDmicro", "EURUSD"]
    assert match_group(names, "*XAU*,*GOLD*") == ["XAUUSD", "XAUEUR", "GOLDmicro"]
    assert match_group(names, "*XAU*,!*EUR*") == ["XAUUSD"]
