"""Export et relecture de l'historique (tranches, relecture stable, fichiers parquet)."""

from datetime import UTC, date, datetime
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import RATES_DTYPE, BrokerError
from goldbot.broker.fake_broker import FakeBroker, make_bars, make_ticks
from goldbot.data.history import (
    day_chunks,
    export_bars,
    export_ticks,
    fetch_stable,
    load_bars,
    load_ticks,
    month_chunks,
    server_epoch,
    ticks_frame,
    verify_files,
)
from tests.conftest import open_instants_ms, open_minutes, server_epoch_of


@pytest.fixture
def history_broker(schedule):
    broker = FakeBroker()
    broker.connect()
    broker.history_bars = make_bars(open_minutes(schedule, "2025-11-17", "2026-02-07"))
    broker.history_ticks = make_ticks(open_instants_ms(schedule, "2026-01-26", "2026-02-07"))
    return broker


def _export_bars(broker, rule, out_dir, **kwargs):
    return export_bars(
        broker,
        "XAUUSD",
        since=kwargs.pop("since", date(2000, 1, 1)),
        until=kwargs.pop("until", date(2026, 2, 9)),
        out_dir=out_dir,
        rule=rule,
        sleep=lambda s: None,
        progress=lambda text: None,
        **kwargs,
    )


def _export_ticks(broker, rule, out_dir, since=date(2026, 1, 26), until=date(2026, 2, 9)):
    return export_ticks(
        broker,
        "XAUUSD",
        since=since,
        until=until,
        out_dir=out_dir,
        rule=rule,
        sleep=lambda s: None,
        progress=lambda text: None,
    )


# --- Tranches ---------------------------------------------------------------------------


def test_month_chunks_go_back_in_time_within_bounds():
    chunks = month_chunks(date(2025, 11, 15), date(2026, 2, 10))
    assert [c.label for c in chunks] == ["2026-02", "2026-01", "2025-12", "2025-11"]
    assert (chunks[0].start, chunks[0].end) == (server_epoch(date(2026, 2, 1)), server_epoch(date(2026, 2, 10)))
    assert chunks[-1].start == server_epoch(date(2025, 11, 15))
    # Contiguës, sans recouvrement.
    assert all(newer.start == older.end for newer, older in pairwise(chunks))


def test_month_chunks_skip_a_month_without_complete_day():
    assert month_chunks(date(2025, 12, 1), date(2026, 2, 1))[0].label == "2026-01"


def test_day_chunks_skip_weekends():
    labels = [c.label for c in day_chunks(date(2026, 2, 5), date(2026, 2, 11))]
    assert labels == ["2026-02-05", "2026-02-06", "2026-02-09", "2026-02-10"]


def test_server_epoch_is_midnight_server_time():
    assert server_epoch(date(2026, 1, 6)) == server_epoch_of("2026-01-06 00:00")


# --- Relecture stable -----------------------------------------------------------------------


def _array(times):
    data = np.zeros(len(times), RATES_DTYPE)
    data["time"] = times
    return data


def test_fetch_stable_rereads_until_two_identical_reads():
    reads = iter([_array([1]), _array([1, 2]), _array([1, 2])])
    data, stable = fetch_stable(lambda: next(reads), "time", sleep=lambda s: None)
    assert stable and list(data["time"]) == [1, 2]


def test_fetch_stable_reports_data_that_keeps_changing():
    counter = iter(range(1, 100))
    data, stable = fetch_stable(lambda: _array(range(next(counter))), "time", attempts=3, sleep=lambda s: None)
    assert not stable and len(data) == 3  # la dernière lecture


def test_empty_read_is_confirmed_after_a_longer_pause():
    pauses = []
    reads = iter([_array([]), _array([5]), _array([5])])
    data, stable = fetch_stable(lambda: next(reads), "time", sleep=pauses.append)
    assert stable and list(data["time"]) == [5]
    assert pauses == [3.0, 0.5]


# --- Barres ------------------------------------------------------------------------------------


def test_bars_are_exported_one_file_per_year_in_utc(history_broker, rule, tmp_path):
    result = _export_bars(history_broker, rule, tmp_path)
    assert [f.name for f in result.files] == ["XAUUSD_M1_2026.parquet", "XAUUSD_M1_2025.parquet"]
    bars = load_bars(tmp_path, "XAUUSD")
    source = history_broker.history_bars
    assert len(bars) == len(source) == result.rows
    assert (bars["time_server"].to_numpy() == source["time"]).all()
    assert np.allclose(bars["close"], source["close"])
    # 6 janvier 2026, 13:00 heure serveur = 06:00 à New York (hiver) = 11:00 UTC.
    row = bars.loc[bars["time_server"] == server_epoch_of("2026-01-06 13:00")].iloc[0]
    assert row["time"] == pd.Timestamp("2026-01-06 11:00", tz="UTC")
    assert str(bars["time"].dtype) == "datetime64[ms, UTC]"


def test_bars_export_stops_where_the_broker_history_starts(history_broker, rule, tmp_path):
    result = _export_bars(history_broker, rule, tmp_path)
    months = [call for call in history_broker.history_calls if call[0] == "rates"]
    # 2026-02 à 2025-11 avec données, puis 6 mois vides (relus 2 fois chacun) avant l'arrêt.
    first_months = {call[3] for call in months}
    assert len(first_months) == 4 + 6
    assert result.empty == [] and result.failed == []


def test_empty_month_inside_the_history_is_reported(history_broker, rule, tmp_path):
    bars = history_broker.history_bars
    december = (bars["time"] >= server_epoch_of("2025-12-01")) & (bars["time"] < server_epoch_of("2026-01-01"))
    history_broker.history_bars = bars[~december]
    result = _export_bars(history_broker, rule, tmp_path)
    assert result.empty == ["2025-12"]


def test_requested_bounds_are_inclusive_and_never_overlap(history_broker, rule, tmp_path):
    _export_bars(history_broker, rule, tmp_path, since=date(2026, 1, 1))
    ranges = sorted({(call[3], call[4]) for call in history_broker.history_calls})
    assert ranges == [
        (server_epoch(date(2026, 1, 1)), server_epoch(date(2026, 2, 1)) - 1),
        (server_epoch(date(2026, 2, 1)), server_epoch(date(2026, 2, 9)) - 1),
    ]
    assert all(call[2] == C.TIMEFRAME_M1 for call in history_broker.history_calls)


def test_failed_month_is_recorded_and_export_continues(history_broker, rule, tmp_path):
    history_broker.history_errors = [BrokerError("copy_rates_range(XAUUSD)", C.RES_E_INVALID_PARAMS, "bad")]
    result = _export_bars(history_broker, rule, tmp_path)
    assert len(result.failed) == 1 and result.failed[0].startswith("2026-02 : ")
    assert "2026-02" not in result.empty
    assert result.rows > 0


def test_lost_terminal_link_triggers_a_reconnection(history_broker, rule, tmp_path):
    history_broker.history_errors = [BrokerError("copy_rates_range", C.RES_E_INTERNAL_FAIL_CONNECT, "No IPC")]
    reconnections = []
    result = _export_bars(history_broker, rule, tmp_path, reconnect=lambda: reconnections.append(1))
    assert reconnections == [1]
    assert result.failed == [] and result.rows == len(history_broker.history_bars)


def test_partial_first_read_is_completed(history_broker, rule, tmp_path):
    history_broker.history_sync_calls = 1  # premier appel tronqué : terminal en cours de téléchargement
    result = _export_bars(history_broker, rule, tmp_path)
    assert result.rows == len(history_broker.history_bars)


# --- Ticks -------------------------------------------------------------------------------------


def test_ticks_are_exported_by_week_without_loss_or_duplicate(history_broker, rule, tmp_path):
    ticks = history_broker.history_ticks
    # Un tick pile à minuit serveur appartient au jour qui commence.
    midnight = server_epoch_of("2026-02-03 00:00") * 1000
    extra = make_ticks(np.array([midnight]))
    history_broker.history_ticks = np.sort(np.concatenate([ticks, extra]), order="time_msc")
    result = _export_ticks(history_broker, rule, tmp_path)
    assert [f.name for f in result.files] == [
        "XAUUSD_ticks_2026-W05.parquet",
        "XAUUSD_ticks_2026-W06.parquet",
    ]
    loaded = load_ticks(tmp_path, "XAUUSD")
    assert len(loaded) == len(history_broker.history_ticks) == result.rows
    assert (np.diff(loaded["time_msc_server"].to_numpy()) >= 0).all()
    requests = [call for call in history_broker.history_calls if call[0] == "ticks"]
    assert all(call[4] == C.COPY_TICKS_ALL for call in requests)


def test_ticks_frame_keeps_milliseconds():
    raw = make_ticks(np.array([server_epoch_of("2026-01-06 13:00") * 1000 + 250]))
    from goldbot.config import load_settings
    from goldbot.data.timezones import ServerTimeRule
    from tests.conftest import CONFIG_PATH

    frame = ticks_frame(raw, ServerTimeRule.from_config(load_settings(CONFIG_PATH).server_time))
    assert frame["time"].iloc[0] == pd.Timestamp("2026-01-06 11:00:00.250", tz="UTC")


# --- Relecture et intégrité ---------------------------------------------------------------------


def _manifest(tmp_path, result):
    import json
    from dataclasses import asdict

    (tmp_path / "manifest.json").write_text(
        json.dumps({"bars": {"files": [asdict(f) for f in result.files]}}), encoding="utf-8"
    )


def test_verify_files_detects_missing_and_modified_files(history_broker, rule, tmp_path):
    result = _export_bars(history_broker, rule, tmp_path)
    _manifest(tmp_path, result)
    assert verify_files(tmp_path) == []
    (tmp_path / "XAUUSD_M1_2025.parquet").write_bytes(b"abime")
    (tmp_path / "XAUUSD_M1_2026.parquet").unlink()
    problems = verify_files(tmp_path)
    assert any("2025" in p and "différent" in p for p in problems)
    assert any("2026" in p and "manquant" in p for p in problems)


def test_verify_files_without_manifest(tmp_path):
    assert "absent" in verify_files(tmp_path)[0]


def test_load_bars_removes_duplicates_unless_asked(history_broker, rule, tmp_path):
    _export_bars(history_broker, rule, tmp_path)
    copy = pd.read_parquet(tmp_path / "XAUUSD_M1_2026.parquet").head(3)
    copy.to_parquet(tmp_path / "XAUUSD_M1_2027.parquet", index=False)
    assert len(load_bars(tmp_path, "XAUUSD")) == len(history_broker.history_bars)
    assert len(load_bars(tmp_path, "XAUUSD", deduplicate=False)) == len(history_broker.history_bars) + 3


def test_load_bars_without_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_bars(tmp_path, "XAUUSD")


def test_export_until_today_excludes_the_current_day(history_broker, rule, tmp_path):
    now = datetime(2026, 2, 6, 12, tzinfo=UTC)
    result = _export_bars(history_broker, rule, tmp_path, until=rule.to_server(now).date())
    assert load_bars(tmp_path, "XAUUSD")["time_server"].max() < server_epoch_of("2026-02-06 00:00")
    assert result.rows > 0
