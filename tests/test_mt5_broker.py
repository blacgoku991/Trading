"""MT5Broker.connect avec un faux module MetaTrader5 (le vrai n'existe que sous Windows)."""

from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import RATES_DTYPE, TICKS_DTYPE, BrokerError
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

    def copy_rates_range(self, *args):
        self.calls.append(("copy_rates_range", args, {}))
        return self.history

    def copy_ticks_range(self, *args):
        self.calls.append(("copy_ticks_range", args, {}))
        return self.history

    def history_deals_get(self, *args, **kwargs):
        self.calls.append(("history_deals_get", args, kwargs))
        return self.history


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


def test_history_bounds_are_passed_as_utc_datetimes_holding_the_server_epoch():
    stub = StubMT5(logged_in_as=123)
    stub.history = np.zeros(2, RATES_DTYPE)
    result = _broker(stub).rates_range("XAUUSD", C.TIMEFRAME_M1, 1_767_700_800, 1_767_787_199)
    assert len(result) == 2
    _, args, _ = stub.calls[-1]
    assert args[:2] == ("XAUUSD", C.TIMEFRAME_M1)
    assert args[2] == datetime(2026, 1, 6, 12, 0, tzinfo=UTC) and args[2].timestamp() == 1_767_700_800
    assert args[3].tzinfo is UTC


def test_ticks_request_passes_the_flags():
    stub = StubMT5(logged_in_as=123)
    stub.history = np.zeros(1, TICKS_DTYPE)
    _broker(stub).ticks_range("XAUUSD", 1_767_700_800, 1_767_787_200, C.COPY_TICKS_ALL)
    assert stub.calls[-1][1][3] == C.COPY_TICKS_ALL


def test_no_history_with_success_code_is_an_empty_array():
    stub = StubMT5(logged_in_as=123)
    stub.history = None
    result = _broker(stub).rates_range("XAUUSD", C.TIMEFRAME_M1, 0, 60)
    assert len(result) == 0 and result.dtype == RATES_DTYPE


def test_history_failure_raises_with_last_error():
    stub = StubMT5(logged_in_as=123)
    stub.history = None
    stub.error = (C.RES_E_INVALID_PARAMS, "Invalid params")
    with pytest.raises(BrokerError, match="copy_ticks_range"):
        _broker(stub).ticks_range("XAUUSD", 0, 60, C.COPY_TICKS_ALL)


def test_deals_between_passes_positional_utc_dates_and_reads_the_deals():
    from collections import namedtuple

    from goldbot.broker.base import Deal

    fields = [f for f in Deal.__dataclass_fields__] + ["external_id"]  # champ en plus, ignoré
    TradeDeal = namedtuple("TradeDeal", fields)
    values = dict(ticket=7, order=6, position_id=6, symbol="XAUUSD", type=C.DEAL_TYPE_BUY, entry=C.DEAL_ENTRY_IN,
                  reason=C.DEAL_REASON_EXPERT, volume=0.02, price=4150.12, commission=0.0, swap=0.0, fee=0.0,
                  profit=0.0, magic=20260929, comment="SC-B-1-L#1", time_msc=1_767_700_800_123, external_id="")  # fmt: skip
    stub = StubMT5(logged_in_as=123)
    stub.history = (TradeDeal(**values),)
    (deal,) = _broker(stub).deals_between(1_767_700_000, 1_767_787_200)
    assert deal.comment == "SC-B-1-L#1" and deal.position_id == 6 and deal.entry == C.DEAL_ENTRY_IN
    name, args, kwargs = stub.calls[-1]
    assert name == "history_deals_get" and kwargs == {}  # dates en positionnel seulement
    assert args[0] == datetime(2026, 1, 6, 11, 46, 40, tzinfo=UTC) and args[1].timestamp() == 1_767_787_200
    stub.history = None
    assert _broker(stub).deals_between(0, 60) == []  # rien trouvé, code de succès
