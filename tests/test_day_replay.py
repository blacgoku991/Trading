"""Rejeu d'une journée (scripts/rejeu_jour.py) : variantes, fichiers exportés et terminal MT5 (broker factice)."""

import json

import numpy as np
import pytest

from goldbot.broker.base import TICKS_DTYPE
from goldbot.broker.fake_broker import FakeBroker, make_account, make_bars, make_symbol
from goldbot.data.history import bars_frame, ticks_frame, write_parquet
from goldbot.data.timezones import ServerTimeRule
from goldbot.scalping.day_replay import EXIT_OK, day_main, variants
from goldbot.scalping.policy import after_open_refusal
from tests.conftest import CONFIG_PATH, server_epoch_of

START_S = server_epoch_of("2026-01-06 12:00")  # mardi, heure serveur
FIELDS = ("name", "point", "trade_tick_size", "trade_tick_value", "trade_contract_size", "volume_min", "volume_max",
          "volume_step", "trade_stops_level", "trade_freeze_level")  # fmt: skip


def _day_arrays():
    """Minute haussière puis baissière (achat « deux bougies » à 12:02), minute haussière (vente à 12:03), puis montée
    de 5,80 $ en 5 minutes : l'achat touche son objectif de 40 pips, la vente son stop."""
    mids = np.concatenate([np.linspace(4000.0, 4000.5, 60), np.linspace(4000.5, 4000.2, 60),
                           np.linspace(4000.2, 4006.0, 5 * 60), np.full(8 * 60, 4006.0)])  # fmt: skip
    ticks = np.zeros(len(mids), TICKS_DTYPE)
    ticks["time_msc"] = (START_S + np.arange(len(mids))) * 1000
    ticks["time"] = ticks["time_msc"] // 1000
    ticks["bid"], ticks["ask"] = np.round(mids - 0.08, 2), np.round(mids + 0.08, 2)
    bars = make_bars(np.arange(START_S - 300 * 60, START_S + 15 * 60, 60), start_price=4000.0)
    return ticks, bars


def test_each_variant_changes_a_single_setting(settings):
    table = variants(settings.scalping)
    assert next(iter(table)) == "version démo" and table["version démo"] is settings.scalping
    demo = settings.scalping.model_dump()
    for name, cfg in list(table.items())[1:]:
        dump = cfg.model_dump()
        changed = [key for key in dump if dump[key] != demo[key]]
        assert len(changed) == 1, name
        if changed == ["two_candles"]:
            inner = [k for k in dump["two_candles"] if dump["two_candles"][k] != demo["two_candles"][k]]
            assert len(inner) == 1, name


def test_no_entry_right_after_the_daily_reopen_when_asked(settings):
    cfg = settings.scalping
    assert after_open_refusal(10.0, cfg) is None  # défaut : pas de pause
    cfg = cfg.model_copy(update={"no_entry_after_open_min": 60.0})
    assert after_open_refusal(59 * 60.0, cfg) == "réouverture du marché : pas d'entrée pendant 60 min"
    assert after_open_refusal(60 * 60.0, cfg) is None and after_open_refusal(None, cfg) is None


def test_day_replay_from_exported_files(tmp_path, settings):
    rule = ServerTimeRule.from_config(settings.server_time)
    ticks, bars = _day_arrays()
    write_parquet(ticks_frame(ticks, rule), tmp_path / "XAUUSD_ticks_2026-W02.parquet", "time_msc_server")
    write_parquet(bars_frame(bars, rule), tmp_path / "XAUUSD_M1_2026.parquet", "time_server")
    spec = make_symbol()
    manifest = {"symbol": {name: getattr(spec, name) for name in FIELDS}, "account": {"currency": "EUR"}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = []
    code = day_main(["--dir", str(tmp_path), "--config", str(CONFIG_PATH), "--capital", "5000"], root=tmp_path,
                    echo=lines.append)  # fmt: skip
    text = "\n".join(lines)
    assert code == EXIT_OK
    assert lines[0] == "=== Rejeu du 06/01/2026 (toute la journée, heure de Paris), capital de départ 5000.00 EUR ==="
    rows = [line for line in lines if line.startswith("  ") and "EUR" in line and "lot à" not in line]
    assert [row.split()[0] for row in rows][:2] == ["version", "objectif"] and len(rows) == len(variants(settings.scalping))
    assert "Trades de la version démo (heure de Paris) :" in text
    assert "  11:02:00 ACHAT 0.4 lot à 4000.28 -> 4004.28 (objectif, 215 s) +40.0 pips +160.00 EUR" in lines
    assert "  11:03:00 VENTE 0.4 lot à 4001.28 -> 4002.28 (stop, 43 s) -10.0 pips -40.00 EUR" in lines


def test_day_replay_window_without_trades_says_so(tmp_path, settings):
    rule = ServerTimeRule.from_config(settings.server_time)
    ticks, bars = _day_arrays()
    write_parquet(ticks_frame(ticks, rule), tmp_path / "XAUUSD_ticks_2026-W02.parquet", "time_msc_server")
    write_parquet(bars_frame(bars, rule), tmp_path / "XAUUSD_M1_2026.parquet", "time_server")
    manifest = {"symbol": {name: getattr(make_symbol(), name) for name in FIELDS}, "account": {"currency": "EUR"}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = []
    code = day_main(["--dir", str(tmp_path), "--config", str(CONFIG_PATH), "--de", "20:00", "--a", "21:00"],
                    root=tmp_path, echo=lines.append)  # fmt: skip
    assert code == 1 and "Aucun tick le 06/01/2026" in lines[-1]


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text("MT5_LOGIN=87654321\nMT5_PASSWORD=s3cret-pass\nMT5_SERVER=Axi-Demo-Server\n", encoding="utf-8")
    return path


def test_day_replay_reads_the_day_in_the_terminal_and_starts_from_the_account_equity(tmp_path, env_file, settings):
    from datetime import UTC, datetime

    ticks, bars = _day_arrays()
    broker = FakeBroker(account=make_account(balance=5400.0, equity=5399.87, currency="EUR"), symbols=[make_symbol()])
    broker.history_ticks, broker.history_bars = ticks, bars
    lines = []
    code = day_main(["--config", str(CONFIG_PATH), "--env", str(env_file)], root=tmp_path,
                    broker_factory=lambda settings, secrets: broker, now_utc=lambda: datetime(2026, 1, 6, 16, 0, tzinfo=UTC),
                    sleep=lambda s: None, echo=lines.append)  # fmt: skip
    text = "\n".join(lines)
    assert code == EXIT_OK and broker.sent == []  # lecture seulement : aucun ordre
    assert "capital de départ 5399.87 EUR" in text and "ACHAT 0.4 lot à" in text
    assert not any(secret in text for secret in ("87654321", "s3cret-pass", "Axi-Demo-Server"))
