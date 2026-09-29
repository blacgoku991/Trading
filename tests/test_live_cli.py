"""Garde-fous du lancement du bot en direct."""

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.fake_broker import FakeBroker, make_account, make_terminal
from goldbot.live.cli import EXIT_LOCKED, EXIT_OK, EXIT_REFUSED, main
from goldbot.live.lock import InstanceLock
from tests.conftest import CONFIG_PATH, OPEN_NOW, tick_at

SECRETS = ("87654321", "s3cret-pass", "Axi-Demo-Server")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(f"MT5_LOGIN={SECRETS[0]}\nMT5_PASSWORD={SECRETS[1]}\nMT5_SERVER={SECRETS[2]}\n", encoding="utf-8")
    return path


def _run(tmp_path, env_file, broker, *args):
    lines = []
    code = main(
        ["--config", str(CONFIG_PATH), "--env", str(env_file), *args],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=lambda: OPEN_NOW,
        sleep=lambda s: None,
        echo=lines.append,
        max_steps=2,
    )
    return code, "\n".join(lines)


def _fake(rule, **account):
    return FakeBroker(account=make_account(**account), ticks={"XAUUSD": tick_at(rule, OPEN_NOW)})


def test_demo_account_runs_and_prints_the_schedule(tmp_path, env_file, rule):
    code, output = _run(tmp_path, env_file, _fake(rule))
    assert code == EXIT_OK, output
    assert "mode DÉMO" in output and "londres : signal vers 11:00" in output
    for secret in SECRETS:
        assert secret not in output


def test_real_account_is_refused_without_both_safeguards(tmp_path, env_file, rule):
    broker = _fake(rule, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _run(tmp_path, env_file, broker)
    assert code == EXIT_REFUSED and "REFUS" in output
    assert broker.sent == []
    code, _ = _run(tmp_path, env_file, broker, "--i-understand-real-money")  # LIVE_TRADING absent de .env
    assert code == EXIT_REFUSED


def test_netting_account_is_refused(tmp_path, env_file, rule):
    code, output = _run(tmp_path, env_file, _fake(rule, margin_mode=C.ACCOUNT_MARGIN_MODE_RETAIL_NETTING))
    assert code == EXIT_REFUSED and "netting" in output


def test_algo_trading_button_off_is_refused(tmp_path, env_file, rule):
    broker = _fake(rule)
    broker.terminal_state = make_terminal(trade_allowed=False)
    code, output = _run(tmp_path, env_file, broker)
    assert code == EXIT_REFUSED and "Algo Trading" in output


def test_simulation_needs_no_trading_permission_and_sends_nothing(tmp_path, env_file, rule):
    broker = _fake(rule, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _run(tmp_path, env_file, broker, "--simulation")
    assert code == EXIT_OK and "SIMULATION" in output
    assert broker.sent == []


def test_only_one_bot_at_a_time(tmp_path, env_file, rule):
    lock = InstanceLock(tmp_path / "project" / "data" / "live.lock")
    assert lock.acquire()
    try:
        code, output = _run(tmp_path, env_file, _fake(rule))
        assert code == EXIT_LOCKED and "un autre bot" in output
    finally:
        lock.release()


def test_a_halted_bot_stays_halted_until_a_manual_restart(tmp_path, env_file, rule):
    from goldbot.config import load_settings
    from goldbot.risk.limits import RiskLimits
    from goldbot.state.store import StateStore

    store = StateStore(tmp_path / "project" / "data" / "live.sqlite")
    limits = RiskLimits.start(load_settings(CONFIG_PATH).risk, 10_000.0)
    limits.halted = True
    store.save_risk(limits)
    store.close()
    _, output = _run(tmp_path, env_file, _fake(rule))
    assert "arrêt total" in output.lower() and "--reprendre-apres-arret" in output
    _, output = _run(tmp_path, env_file, _fake(rule), "--reprendre-apres-arret")
    assert "Arrêt total levé" in output
    reloaded = RiskLimits.start(load_settings(CONFIG_PATH).risk, 10_000.0)
    StateStore(tmp_path / "project" / "data" / "live.sqlite").load_risk(reloaded)
    assert reloaded.halted is False
