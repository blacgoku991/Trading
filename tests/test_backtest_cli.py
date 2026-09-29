"""Bout en bout de scripts/run_backtest.py sur un export synthétique."""

import csv
import json
from datetime import UTC, datetime

import pytest

from goldbot.backtest.cli import EXIT_NO_DATA, EXIT_OK, main
from goldbot.broker.fake_broker import make_bars
from goldbot.data.history import bars_frame, write_parquet
from tests.conftest import CONFIG_PATH, open_minutes

SYMBOL = {
    "name": "XAUUSD",
    "point": 0.01,
    "trade_contract_size": 100.0,
    "volume_min": 0.01,
    "volume_max": 20.0,
    "volume_step": 0.01,
    "trade_stops_level": 1,
    "swap_mode": 1,
    "swap_long": -61.6,
    "swap_short": 40.5,
    "swap_rollover3days": 3,
}


@pytest.fixture
def project(tmp_path, schedule, rule):
    folder = tmp_path / "data" / "export"
    bars = bars_frame(make_bars(open_minutes(schedule, "2025-01-06", "2025-03-01"), seed=7), rule)
    write_parquet(bars, folder / "XAUUSD_M1_2025.parquet", "time_server")
    (folder / "manifest.json").write_text(json.dumps({"symbol": SYMBOL}), encoding="utf-8")
    return tmp_path


def _run(project, *args):
    lines = []
    code = main(
        ["--config", str(CONFIG_PATH), *args],
        root=project,
        now_utc=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
        echo=lines.append,
    )
    return code, "\n".join(lines)


def test_backtest_writes_a_report_an_image_and_logs_the_trial(project):
    code, output = _run(project)
    assert code == EXIT_OK, output
    reports = project / "reports"
    report = reports / "backtest_s1_20260929T120000Z.md"
    assert report.exists() and (reports / "backtest_s1_20260929T120000Z.png").exists()
    text = report.read_text(encoding="utf-8")
    assert "## Résultats" in text and "Profit factor" in text
    with (reports / "essais.csv").open(encoding="utf-8") as handle:
        (row,) = list(csv.DictReader(handle, delimiter=";"))
    assert row["strategie"] == "s1" and row["hors_echantillon"] == "False"
    assert "trades" in output


def test_out_of_sample_period_is_excluded_by_default(project):
    # Les données s'arrêtent en 2025-03 : la période hors échantillon (après 2025-10-01) est vide.
    code, output = _run(project, "--hors-echantillon")
    assert code == EXIT_NO_DATA
    assert "hors échantillon" in output


def test_stress_options_are_recorded(project):
    _run(project, "--spread-x", "1.5", "--glissement", "10")
    with (project / "reports" / "essais.csv").open(encoding="utf-8") as handle:
        (row,) = list(csv.DictReader(handle, delimiter=";"))
    assert (row["spread_x"], row["glissement"]) == ("1.5", "10.0")


def test_start_date_uses_earlier_bars_only_to_warm_up_indicators(project):
    code, output = _run(project, "--strategie", "portefeuille", "--debut", "2025-02-10")
    assert code == EXIT_OK, output
    report = next((project / "reports").glob("backtest_portefeuille_*.md")).read_text(encoding="utf-8")
    assert "du 2025-02-10" in report  # l'ATR des jours précédents est connu dès le premier jour tradé
