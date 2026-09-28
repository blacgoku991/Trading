from pathlib import Path

import pytest
import yaml

from goldbot.config import ConfigError, load_secrets, load_settings
from tests.conftest import CONFIG_PATH


def _write_settings(tmp_path: Path, **changes) -> Path:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    for dotted, value in changes.items():
        section, key = dotted.split("__")
        raw[section][key] = value
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_repo_settings_are_valid(settings):
    assert settings.risk.risk_per_trade_pct == 0.5
    assert settings.risk.max_daily_loss_pct == 2.0
    assert settings.risk.max_drawdown_pct == 10.0
    assert settings.risk.max_open_positions == 2
    assert settings.risk.max_trades_per_day == 4
    assert settings.risk.max_consecutive_losses == 3
    assert settings.symbol.is_auto
    assert settings.test_order.volume == 0.01


def test_risk_per_trade_hard_cap_is_enforced(tmp_path):
    with pytest.raises(ConfigError, match="risk_per_trade_pct"):
        load_settings(_write_settings(tmp_path, risk__risk_per_trade_pct=1.5))


def test_risk_per_trade_at_cap_is_accepted(tmp_path):
    assert load_settings(_write_settings(tmp_path, risk__risk_per_trade_pct=1.0)).risk.risk_per_trade_pct == 1.0


def test_unknown_timezone_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="fuseau horaire inconnu"):
        load_settings(_write_settings(tmp_path, server_time__reference_timezone="Mars/Olympus"))


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="risk.typo"):
        load_settings(_write_settings(tmp_path, risk__typo=1))


def test_test_order_volume_is_capped(tmp_path):
    with pytest.raises(ConfigError, match="test_order.volume"):
        load_settings(_write_settings(tmp_path, test_order__volume=0.1))


def test_unquoted_times_are_rejected(tmp_path):
    text = CONFIG_PATH.read_text(encoding="utf-8").replace('week_close: "23:58"', "week_close: 23:58")
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="entre guillemets"):
        load_settings(path)


def test_daily_break_must_span_midnight(tmp_path):
    with pytest.raises(ConfigError, match="chevaucher minuit"):
        load_settings(_write_settings(tmp_path, market_hours__daily_break_start="00:30"))


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_settings(tmp_path / "absent.yaml")


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)


def _env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_secrets_are_loaded_and_never_in_repr(tmp_path, clean_env):
    secrets = load_secrets(_env(tmp_path, "MT5_LOGIN=87654321\nMT5_PASSWORD='p@ss #word'\nMT5_SERVER=Axi-Demo\n"))
    assert secrets.login == 87654321
    assert secrets.password == "p@ss #word"
    assert secrets.live_trading is False
    text = repr(secrets)
    for value in ("87654321", "p@ss", "Axi-Demo"):
        assert value not in text


def test_missing_secrets_are_listed_without_values(tmp_path, clean_env):
    with pytest.raises(ConfigError, match="MT5_PASSWORD, MT5_SERVER"):
        load_secrets(_env(tmp_path, "MT5_LOGIN=1\n"))


def test_login_must_be_numeric(tmp_path, clean_env):
    with pytest.raises(ConfigError, match="nombre entier") as info:
        load_secrets(_env(tmp_path, "MT5_LOGIN=abc123\nMT5_PASSWORD=x\nMT5_SERVER=s\n"))
    assert "abc123" not in str(info.value)


@pytest.mark.parametrize(("text", "expected"), [("true", True), ("TRUE", True), ("false", False), ("", False)])
def test_live_trading_flag(tmp_path, clean_env, text, expected):
    secrets = load_secrets(_env(tmp_path, f"MT5_LOGIN=1\nMT5_PASSWORD=x\nMT5_SERVER=s\nLIVE_TRADING={text}\n"))
    assert secrets.live_trading is expected


def test_live_trading_flag_rejects_garbage(tmp_path, clean_env):
    with pytest.raises(ConfigError, match="LIVE_TRADING"):
        load_secrets(_env(tmp_path, "MT5_LOGIN=1\nMT5_PASSWORD=x\nMT5_SERVER=s\nLIVE_TRADING=peut-etre\n"))


def test_windows_path_in_double_quotes_is_rejected(tmp_path, clean_env):
    env = 'MT5_LOGIN=1\nMT5_PASSWORD=x\nMT5_SERVER=s\nMT5_PATH="C:\\temp\\new\\terminal64.exe"\n'
    with pytest.raises(ConfigError, match="MT5_PATH"):
        load_secrets(_env(tmp_path, env))


def test_unquoted_windows_path_is_kept(tmp_path, clean_env):
    env = "MT5_LOGIN=1\nMT5_PASSWORD=x\nMT5_SERVER=s\nMT5_PATH=C:\\temp\\new\\terminal64.exe\n"
    assert load_secrets(_env(tmp_path, env)).terminal_path == "C:\\temp\\new\\terminal64.exe"


def test_environment_variables_take_precedence(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("MT5_SERVER", "From-Env")
    secrets = load_secrets(_env(tmp_path, "MT5_LOGIN=1\nMT5_PASSWORD=x\nMT5_SERVER=From-File\n"))
    assert secrets.server == "From-Env"
