import types

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.mt5_broker import verify_constants


def test_mirror_contains_the_official_constants():
    assert C.PACKAGE_VERSION == "5.0.6231"
    assert len(C.OFFICIAL_NAMES) == 223
    assert C.TRADE_RETCODE_DONE == 10009
    assert C.ORDER_FILLING_RETURN == 2
    assert C.TIMEFRAME_H1 == 16385
    assert C.ACCOUNT_TRADE_MODE_REAL == 2


def test_verify_constants_detects_a_changed_value():
    module = types.SimpleNamespace(**{name: getattr(C, name) for name in C.OFFICIAL_NAMES})
    assert verify_constants(module) == []
    module.TRADE_RETCODE_DONE = 1
    del module.ORDER_FILLING_FOK
    problems = verify_constants(module)
    assert any("TRADE_RETCODE_DONE" in p for p in problems)
    assert any("ORDER_FILLING_FOK absent" in p for p in problems)


def test_mirror_matches_installed_package():
    """Sous Windows uniquement : compare le miroir au vrai package MetaTrader5."""
    mt5 = pytest.importorskip("MetaTrader5")
    assert verify_constants(mt5) == []
