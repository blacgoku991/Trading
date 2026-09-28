import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.fake_broker import make_account
from goldbot.risk.guards import TradingRefused, ensure_demo_account, ensure_trading_permitted

DEMO = make_account(trade_mode=C.ACCOUNT_TRADE_MODE_DEMO)
REAL = make_account(trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
CONTEST = make_account(trade_mode=C.ACCOUNT_TRADE_MODE_CONTEST)


def test_demo_only_guard():
    ensure_demo_account(DEMO)
    for account in (REAL, CONTEST):
        with pytest.raises(TradingRefused):
            ensure_demo_account(account)


def test_demo_account_is_always_permitted():
    ensure_trading_permitted(DEMO, live_trading_env=False, real_money_flag=False)


@pytest.mark.parametrize(("env", "flag"), [(False, False), (True, False), (False, True)])
def test_real_account_needs_both_env_and_flag(env, flag):
    with pytest.raises(TradingRefused, match="--i-understand-real-money"):
        ensure_trading_permitted(REAL, live_trading_env=env, real_money_flag=flag)


def test_real_account_with_both_env_and_flag():
    ensure_trading_permitted(REAL, live_trading_env=True, real_money_flag=True)
