"""MT5Broker.connect avec un faux module MetaTrader5 (le vrai n'existe que sous Windows)."""

from types import SimpleNamespace

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import BrokerError
from goldbot.broker.mt5_broker import MT5Broker


class StubMT5(SimpleNamespace):
    """Imite l'API du package : initialize, login, account_info, last_error, shutdown."""

    def __init__(self, *, logged_in_as=None, initialize_ok=True, login_ok=True):
        super().__init__(**{name: getattr(C, name) for name in C.OFFICIAL_NAMES})
        self.logged_in_as = logged_in_as
        self.initialize_ok = initialize_ok
        self.login_ok = login_ok
        self.calls = []
        self.error = (C.RES_S_OK, "Success")

    def initialize(self, *args, **kwargs):
        self.calls.append(("initialize", args, kwargs))
        if not self.initialize_ok:
            self.error = (C.RES_E_INTERNAL_FAIL_TIMEOUT, "IPC timeout")
        return self.initialize_ok

    def account_info(self):
        return None if self.logged_in_as is None else SimpleNamespace(login=self.logged_in_as)

    def login(self, login, **kwargs):
        self.calls.append(("login", (login,), kwargs))
        if self.login_ok:
            self.logged_in_as = login
        else:
            self.error = (C.RES_E_AUTH_FAILED, "Terminal: Authorization failed")
        return self.login_ok

    def last_error(self):
        return self.error

    def shutdown(self):
        self.calls.append(("shutdown", (), {}))


def _broker(stub, path=r"C:\MT5\terminal64.exe"):
    broker = MT5Broker(login=123, password="pw", server="Srv-Demo", path=path, timeout_ms=60_000)
    broker._mt5 = stub
    return broker


def _names(stub):
    return [call[0] for call in stub.calls]


def test_attaches_without_logging_in_again_when_already_on_the_account():
    stub = StubMT5(logged_in_as=123)
    _broker(stub).connect()
    assert _names(stub) == ["initialize"]
    _, args, kwargs = stub.calls[0]
    assert args == (r"C:\MT5\terminal64.exe",)
    assert kwargs == {"timeout": 60_000}  # aucun identifiant envoyé pour s'attacher


def test_logs_in_when_the_terminal_is_on_another_account():
    stub = StubMT5(logged_in_as=999)
    _broker(stub).connect()
    assert _names(stub) == ["initialize", "login"]
    assert stub.calls[1][1:] == ((123,), {"password": "pw", "server": "Srv-Demo"})


def test_logs_in_when_no_account_is_connected():
    stub = StubMT5(logged_in_as=None)
    _broker(stub).connect()
    assert "login" in _names(stub)


def test_portable_mode_is_passed_to_initialize():
    stub = StubMT5(logged_in_as=123)
    broker = MT5Broker(login=123, password="pw", server="s", path=r"C:\MT5\terminal64.exe", timeout_ms=1, portable=True)
    broker._mt5 = stub
    broker.connect()
    assert stub.calls[0][2] == {"timeout": 1, "portable": True}


def test_initialize_without_path():
    stub = StubMT5(logged_in_as=123)
    _broker(stub, path=None).connect()
    assert stub.calls[0][1:] == ((), {"timeout": 60_000})


def test_initialize_failure_reports_last_error_and_shuts_down():
    stub = StubMT5(initialize_ok=False)
    with pytest.raises(BrokerError, match="-10005") as info:
        _broker(stub).connect()
    assert info.value.retryable
    assert _names(stub)[-1] == "shutdown"


def test_login_failure_is_not_retryable():
    stub = StubMT5(logged_in_as=999, login_ok=False)
    with pytest.raises(BrokerError, match="MT5_SERVER") as info:
        _broker(stub).connect()
    assert not info.value.retryable
    assert _names(stub)[-1] == "shutdown"


def test_constant_mismatch_refuses_to_run():
    stub = StubMT5(logged_in_as=123)
    stub.TRADE_RETCODE_DONE = 1
    with pytest.raises(BrokerError, match="TRADE_RETCODE_DONE") as info:
        _broker(stub).connect()
    assert not info.value.retryable
