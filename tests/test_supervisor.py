from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import BrokerError
from goldbot.broker.fake_broker import FakeBroker
from goldbot.broker.supervisor import ConnectionSupervisor, FeedState, backoff_delays, evaluate_feed
from goldbot.config import ReconnectConfig
from tests.conftest import OPEN_NOW, WEEKEND_NOW, tick_at

CONFIG = ReconnectConfig(base_delay_s=1, max_delay_s=60, max_attempts=8)


def _ipc_timeout():
    return BrokerError("initialize", C.RES_E_INTERNAL_FAIL_TIMEOUT, "IPC timeout")


def test_backoff_doubles_and_is_capped():
    assert list(backoff_delays(CONFIG)) == [1, 2, 4, 8, 16, 32, 60]


def test_connect_retries_transient_errors():
    broker = FakeBroker()
    broker.connect_errors = [_ipc_timeout(), _ipc_timeout()]
    sleeps = []
    ConnectionSupervisor(broker, CONFIG, sleep=sleeps.append).connect()
    assert broker.connected
    assert broker.connect_calls == 3
    assert sleeps == [1, 2]


def test_connect_does_not_retry_authentication_failure():
    broker = FakeBroker()
    broker.connect_errors = [BrokerError("initialize", C.RES_E_AUTH_FAILED, "Authorization failed")]
    sleeps = []
    with pytest.raises(BrokerError, match="MT5_SERVER"):
        ConnectionSupervisor(broker, CONFIG, sleep=sleeps.append).connect()
    assert broker.connect_calls == 1
    assert sleeps == []


def test_connect_gives_up_after_max_attempts():
    broker = FakeBroker()
    broker.connect_errors = [_ipc_timeout() for _ in range(10)]
    config = ReconnectConfig(base_delay_s=1, max_delay_s=4, max_attempts=3)
    with pytest.raises(BrokerError):
        ConnectionSupervisor(broker, config, sleep=lambda s: None).connect()
    assert broker.connect_calls == 3


def test_ensure_connected_reconnects_after_ipc_loss():
    broker = FakeBroker()
    broker.connect()
    broker.terminal_errors = [BrokerError("terminal_info", C.RES_E_INTERNAL_FAIL_CONNECT, "No IPC connection")]
    state = ConnectionSupervisor(broker, CONFIG, sleep=lambda s: None).ensure_connected()
    assert state.connected
    assert broker.shutdown_calls == 1
    assert broker.connect_calls == 2


def test_ensure_connected_waits_for_the_terminal_to_reconnect():
    broker = FakeBroker()
    broker.connect()
    connected = broker.terminal_state
    broker.terminal_state = replace(connected, connected=False)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            broker.terminal_state = connected

    assert ConnectionSupervisor(broker, CONFIG, sleep=sleep).ensure_connected().connected
    assert sleeps == [1, 2]


def test_ensure_connected_raises_when_terminal_stays_disconnected():
    broker = FakeBroker()
    broker.connect()
    broker.terminal_state = replace(broker.terminal_state, connected=False)
    with pytest.raises(BrokerError, match="toujours déconnecté"):
        ConnectionSupervisor(broker, CONFIG, sleep=lambda s: None).ensure_connected()


def test_fresh_feed(rule, schedule, settings):
    status = evaluate_feed(tick_at(rule, OPEN_NOW, age_s=2), OPEN_NOW, rule, schedule, settings.feed)
    assert status.state is FeedState.FRESH


def test_stale_feed_while_market_quotes(rule, schedule, settings):
    status = evaluate_feed(tick_at(rule, OPEN_NOW, age_s=120), OPEN_NOW, rule, schedule, settings.feed)
    assert status.state is FeedState.STALE
    assert status.tick_age_s == pytest.approx(120)


def test_old_tick_is_normal_when_market_is_closed(rule, schedule, settings):
    status = evaluate_feed(tick_at(rule, WEEKEND_NOW, age_s=90_000), WEEKEND_NOW, rule, schedule, settings.feed)
    assert status.state is FeedState.CLOSED


def test_grace_period_after_daily_reopening(rule, schedule, settings):
    # 01:02 heure serveur, juste après la fin de la pause quotidienne.
    now = rule.server_to_utc(datetime(2026, 9, 29, 1, 2))
    status = evaluate_feed(tick_at(rule, now, age_s=90), now, rule, schedule, settings.feed)
    assert status.state is FeedState.GRACE
    later = now + timedelta(minutes=10)
    assert evaluate_feed(tick_at(rule, later, age_s=90), later, rule, schedule, settings.feed).state is FeedState.STALE
