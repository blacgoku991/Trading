from goldbot.broker import mt5_constants as C
from goldbot.broker.checks import Level, check_account, check_server_time, check_symbol, check_terminal
from goldbot.broker.fake_broker import make_account, make_symbol, make_terminal
from tests.conftest import OPEN_NOW, WEEKEND_NOW, tick_at


def _levels(findings):
    return [f.level for f in findings]


def _messages(findings, level):
    return " | ".join(f.message for f in findings if f.level is level)


def test_healthy_terminal_has_no_problem():
    assert set(_levels(check_terminal(make_terminal()))) == {Level.OK}


def test_algo_trading_button_off_is_an_error():
    findings = check_terminal(make_terminal(trade_allowed=False))
    assert "10027" in _messages(findings, Level.ERROR)


def test_python_api_disabled_is_an_error():
    findings = check_terminal(make_terminal(tradeapi_disabled=True))
    assert "Disable algorithmic trading via external Python API" in _messages(findings, Level.ERROR)


def test_disconnected_terminal_is_an_error():
    assert Level.ERROR in _levels(check_terminal(make_terminal(connected=False)))


def test_low_maxbars_and_high_ping_are_warnings():
    findings = check_terminal(make_terminal(maxbars=100_000, ping_last=450_000))
    warnings = _messages(findings, Level.WARN)
    assert "Unlimited" in warnings
    assert "450 ms" in warnings


def test_demo_hedging_account_is_ok():
    assert set(_levels(check_account(make_account(), live_trading=False))) == {Level.OK}


def test_real_account_is_flagged():
    findings = check_account(make_account(trade_mode=C.ACCOUNT_TRADE_MODE_REAL), live_trading=False)
    assert "--i-understand-real-money" in _messages(findings, Level.WARN)


def test_netting_warns_about_manual_trades():
    findings = check_account(make_account(margin_mode=C.ACCOUNT_MARGIN_MODE_RETAIL_NETTING), live_trading=False)
    assert "magic" in _messages(findings, Level.WARN)


def test_server_side_algo_ban_is_an_error():
    findings = check_account(make_account(trade_expert=False), live_trading=False)
    assert "trading automatique" in _messages(findings, Level.ERROR)


def test_other_login_than_env_is_an_error_without_showing_numbers():
    findings = check_account(make_account(login=111), live_trading=False, expected_login=222)
    errors = _messages(findings, Level.ERROR)
    assert "autre compte" in errors
    assert "111" not in errors and "222" not in errors


def test_non_usd_account_is_informed():
    findings = check_account(make_account(currency="EUR"), live_trading=False)
    assert "EUR" in _messages(findings, Level.INFO)


def test_symbol_without_fok_ioc_warns():
    findings = check_symbol(make_symbol(filling_mode=0), test_volume=0.01)
    assert "RETURN" in _messages(findings, Level.WARN)


def test_server_time_matches_rule(rule, schedule):
    result = check_server_time(tick_at(rule, OPEN_NOW, age_s=2), OPEN_NOW, rule, schedule)
    assert result.market_open
    assert _levels(result.findings) == [Level.OK]
    assert result.residual_s == -2


def test_wrong_server_offset_is_detected(rule, schedule):
    # Tick horodaté en GMT+2 alors que la règle donne GMT+3 : la règle ou l'horloge est fausse.
    tick = tick_at(rule, OPEN_NOW, age_s=3600)
    result = check_server_time(tick, OPEN_NOW, rule, schedule)
    assert "GMT+2" in _messages(result.findings, Level.ERROR)


def test_server_time_not_measurable_when_market_closed(rule, schedule):
    result = check_server_time(tick_at(rule, WEEKEND_NOW, age_s=100_000), WEEKEND_NOW, rule, schedule)
    assert not result.market_open
    assert _levels(result.findings) == [Level.INFO]
