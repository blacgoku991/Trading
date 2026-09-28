"""Bout en bout du script check_connection.py, avec le broker factice."""

from dataclasses import replace
from pathlib import Path

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import BrokerError
from goldbot.broker.fake_broker import FakeBroker, make_account, make_symbol
from goldbot.diagnostics import EXIT_CONFIG, EXIT_NO_CONNECTION, EXIT_OK, EXIT_PROBLEMS, EXIT_REFUSED, main
from tests.conftest import CONFIG_PATH, OPEN_NOW, tick_at

SECRETS = ("87654321", "s3cret-pass", "Axi-Demo-Server")


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(
        f"MT5_LOGIN={SECRETS[0]}\nMT5_PASSWORD={SECRETS[1]}\nMT5_SERVER={SECRETS[2]}\n", encoding="utf-8"
    )
    return path


def _fake(rule, **account_changes):
    return FakeBroker(
        account=make_account(login=int(SECRETS[0]), **account_changes),
        symbols=[make_symbol("XAUUSD"), make_symbol("XAUEUR", currency_profit="EUR")],
        ticks={"XAUUSD": tick_at(rule, OPEN_NOW)},
        commission_per_lot_side=5.0,
    )


def _run(tmp_path, env_file, broker, *args):
    lines = []
    code = main(
        ["--config", str(CONFIG_PATH), "--env", str(env_file), *args],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=lambda: OPEN_NOW,
        sleep=lambda s: None,
        echo=lines.append,
    )
    return code, "\n".join(lines)


def _written_files_text(project: Path) -> str:
    """Contenu de tout ce que le script a écrit (logs, rapports, dump JSON)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in project.rglob("*") if p.is_file())


def test_healthy_check_passes_and_never_prints_secrets(tmp_path, env_file, rule):
    code, output = _run(tmp_path, env_file, _fake(rule))
    assert code == EXIT_OK
    assert "0 erreur(s)" in output
    assert "XAUUSD" in output and "GMT+3" in output
    reports = tmp_path / "project" / "reports"
    assert list(reports.glob("symbol_info_XAUUSD_*.json"))
    assert list(reports.glob("check_connection_*.txt"))
    everything = output + _written_files_text(tmp_path / "project")
    for secret in SECRETS:
        assert secret not in everything


def test_test_order_reports_the_commission(tmp_path, env_file, rule):
    broker = _fake(rule)
    code, output = _run(tmp_path, env_file, broker, "--test-order")
    assert code == EXIT_OK
    assert "commission par lot aller-retour ... 10.00 USD" in output
    broker.connect()  # le script s'est déconnecté en fin d'exécution
    assert broker.positions() == []


def test_test_order_is_refused_on_a_real_account(tmp_path, env_file, rule):
    broker = _fake(rule, trade_mode=C.ACCOUNT_TRADE_MODE_REAL)
    code, output = _run(tmp_path, env_file, broker, "--test-order")
    assert code == EXIT_REFUSED
    assert "REFUSÉ" in output
    assert broker.sent == [] and broker.checked == []


def test_test_order_is_refused_when_checks_fail(tmp_path, env_file, rule):
    broker = _fake(rule)
    broker.terminal_state = replace(broker.terminal_state, trade_allowed=False)
    code, output = _run(tmp_path, env_file, broker, "--test-order")
    assert code == EXIT_REFUSED
    assert "Algo Trading" in output
    assert broker.sent == []


def test_first_tick_may_arrive_late_after_symbol_selection(tmp_path, env_file, rule, monkeypatch):
    broker = _fake(rule)
    real_tick, misses = broker.tick, iter([True, True])

    def late_tick(name):
        if next(misses, False):
            raise BrokerError(f"symbol_info_tick({name})", C.RES_E_NOT_FOUND, "no tick yet")
        return real_tick(name)

    monkeypatch.setattr(broker, "tick", late_tick)
    code, _ = _run(tmp_path, env_file, broker)
    assert code == EXIT_OK


def test_problems_give_a_non_zero_exit_code(tmp_path, env_file, rule):
    broker = _fake(rule, trade_expert=False)
    code, output = _run(tmp_path, env_file, broker)
    assert code == EXIT_PROBLEMS
    assert "1 erreur(s)" in output


def test_connection_failure_explains_the_fix(tmp_path, env_file, rule):
    broker = _fake(rule)
    broker.connect_errors = [BrokerError("initialize", C.RES_E_AUTH_FAILED, "Terminal: Authorization failed")]
    code, output = _run(tmp_path, env_file, broker)
    assert code == EXIT_NO_CONNECTION
    assert "MT5_SERVER" in output


def test_unreachable_terminal_fails_fast_with_progress_messages(tmp_path, env_file, rule):
    broker = _fake(rule)
    broker.connect_errors = [
        BrokerError("initialize", C.RES_E_INTERNAL_FAIL_TIMEOUT, "IPC timeout") for _ in range(10)
    ]
    code, output = _run(tmp_path, env_file, broker)
    assert code == EXIT_NO_CONNECTION
    assert broker.connect_calls == 3  # la config en autorise 8, le diagnostic s'arrête à 3
    assert "Connexion au terminal MT5 en cours" in output
    assert "MT5_PATH vide" in output
    assert "(Get-Process terminal64).Path" in output


def test_invalid_configuration(tmp_path, env_file, rule):
    lines = []
    code = main(["--config", str(tmp_path / "absent.yaml"), "--env", str(env_file)], root=tmp_path, echo=lines.append)
    assert code == EXIT_CONFIG
    assert "introuvable" in lines[0]
