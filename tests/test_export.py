"""Bout en bout de scripts/export_history.py et scripts/data_report.py, avec le broker factice."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import BrokerError
from goldbot.broker.fake_broker import FakeBroker, make_account, make_bars, make_terminal, make_ticks
from goldbot.data_report import EXIT_NO_DATA
from goldbot.data_report import main as report_main
from goldbot.export import EXIT_CONFIG, EXIT_NO_CONNECTION, EXIT_OK, EXIT_PROBLEMS, main
from tests.conftest import CONFIG_PATH, open_instants_ms, open_minutes, tick_at

SECRETS = ("87654321", "s3cret-pass", "Axi-Demo-Server")
# Mardi 10/02/2026 12:00 UTC : marché ouvert, export jusqu'au lundi 09/02 inclus.
NOW = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH", "LIVE_TRADING"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    path.write_text(f"MT5_LOGIN={SECRETS[0]}\nMT5_PASSWORD={SECRETS[1]}\nMT5_SERVER={SECRETS[2]}\n", encoding="utf-8")
    return path


@pytest.fixture
def fake(rule, schedule):
    broker = FakeBroker(
        account=make_account(login=int(SECRETS[0]), server=SECRETS[2]),
        ticks={"XAUUSD": tick_at(rule, NOW)},
    )
    broker.history_bars = make_bars(open_minutes(schedule, "2025-12-15", "2026-02-10"))
    broker.history_ticks = make_ticks(open_instants_ms(schedule, "2026-02-02", "2026-02-10", step="30s"))
    return broker


def _run(tmp_path, env_file, broker, *args):
    lines = []
    code = main(
        ["--config", str(CONFIG_PATH), "--env", str(env_file), *args],
        root=tmp_path / "project",
        broker_factory=lambda settings, secrets: broker,
        now_utc=lambda: NOW,
        sleep=lambda s: None,
        echo=lines.append,
    )
    return code, "\n".join(lines)


def _export_dir(tmp_path) -> Path:
    return tmp_path / "project" / "data" / "export"


def test_export_writes_files_manifest_and_simple_instructions(tmp_path, env_file, fake):
    code, output = _run(tmp_path, env_file, fake, "--tick-days", "8")
    assert code == EXIT_OK, output
    folder = _export_dir(tmp_path)
    names = sorted(path.name for path in folder.iterdir())
    assert names == [
        "XAUUSD_M1_2025.parquet",
        "XAUUSD_M1_2026.parquet",
        "XAUUSD_ticks_2026-W06.parquet",
        "XAUUSD_ticks_2026-W07.parquet",  # lundi 9 février
        "manifest.json",
    ]
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["symbol"]["name"] == "XAUUSD" and manifest["symbol"]["point"] == 0.01
    assert manifest["bars"]["rows"] == len(fake.history_bars)
    assert manifest["bars"]["until_server_date_excluded"] == "2026-02-10"
    assert abs(manifest["server_time"]["check_at_export"]["residual_s"]) <= 1.0  # tick vieux d'une seconde
    assert "explorer data" in output and "Ctrl+A" in output


def test_export_never_writes_or_prints_secrets(tmp_path, env_file, fake):
    _, output = _run(tmp_path, env_file, fake, "--tick-days", "2")
    written = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "project").rglob("*")
        if path.is_file() and path.suffix != ".parquet"
    )
    for secret in SECRETS:
        assert secret not in output
        assert secret not in written
    manifest = json.loads((_export_dir(tmp_path) / "manifest.json").read_text(encoding="utf-8"))
    assert "login" not in json.dumps(manifest) and 'server"' not in json.dumps(manifest["account"])


def test_previous_export_files_are_replaced(tmp_path, env_file, fake):
    folder = _export_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "XAUUSD_M1_1999.parquet").write_bytes(b"ancien")
    (folder / "notes.txt").write_text("à garder", encoding="utf-8")
    _, output = _run(tmp_path, env_file, fake, "--tick-days", "0")
    assert not (folder / "XAUUSD_M1_1999.parquet").exists()
    assert (folder / "notes.txt").exists()  # seuls les fichiers de l'export sont supprimés
    assert "Anciens fichiers" in output


def test_limited_max_bars_is_reported(tmp_path, env_file, fake):
    fake.terminal_state = make_terminal(maxbars=100_000)
    _, output = _run(tmp_path, env_file, fake, "--tick-days", "0")
    assert "Illimité" in output


def test_no_bars_is_a_problem(tmp_path, env_file, fake):
    fake.history_bars = fake.history_bars[:0]
    code, output = _run(tmp_path, env_file, fake, "--tick-days", "0")
    assert code == EXIT_PROBLEMS
    assert "AUCUNE barre" in output
    assert "touche Début" in output  # comment faire télécharger l'historique au terminal


def test_failed_chunks_are_listed(tmp_path, env_file, fake):
    fake.history_errors = [BrokerError("copy_rates_range(XAUUSD)", C.RES_E_INVALID_PARAMS, "Invalid params")]
    code, output = _run(tmp_path, env_file, fake, "--tick-days", "0")
    assert code == EXIT_PROBLEMS
    assert "ÉCHEC 2026-02" in output


def test_connection_failure(tmp_path, env_file, fake):
    fake.connect_errors = [BrokerError("initialize", C.RES_E_AUTH_FAILED, "Authorization failed")] * 10
    code, output = _run(tmp_path, env_file, fake)
    assert code == EXIT_NO_CONNECTION
    assert "connexion au terminal MT5 impossible" in output


def test_negative_tick_days_is_refused(tmp_path, env_file, fake):
    code, _ = _run(tmp_path, env_file, fake, "--tick-days", "-1")
    assert code == EXIT_CONFIG


def test_quality_report_runs_on_the_export(tmp_path, env_file, fake):
    _run(tmp_path, env_file, fake, "--tick-days", "8")
    lines = []
    code = report_main(
        ["--config", str(CONFIG_PATH)],
        root=tmp_path / "project",
        now_utc=lambda: NOW,
        echo=lines.append,
    )
    reports = list((tmp_path / "project" / "reports").glob("qualite_donnees_*.md"))
    assert code == 0, "\n".join(lines)
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert "Tous les fichiers correspondent au manifeste" in text
    assert "COHÉRENTE" in text


def test_quality_report_without_data(tmp_path):
    code = report_main(["--config", str(CONFIG_PATH)], root=tmp_path, echo=lambda text: None)
    assert code == EXIT_NO_DATA
