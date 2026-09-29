from datetime import date, datetime, time

import pandas as pd
import pytest

from goldbot.config import load_settings
from goldbot.data.market_hours import MarketSchedule
from tests.conftest import CONFIG_PATH


@pytest.mark.parametrize(
    ("server_time", "is_open"),
    [
        (datetime(2026, 9, 28, 1, 0), False),  # lundi avant l'ouverture
        (datetime(2026, 9, 28, 1, 1), True),  # lundi à l'ouverture
        (datetime(2026, 9, 29, 0, 30), False),  # pause quotidienne
        (datetime(2026, 9, 29, 1, 1), True),  # fin de la pause
        (datetime(2026, 9, 29, 23, 58), True),
        (datetime(2026, 9, 29, 23, 59), False),  # début de la pause
        (datetime(2026, 10, 2, 23, 57), True),  # vendredi
        (datetime(2026, 10, 2, 23, 58), False),  # vendredi, fermeture
        (datetime(2026, 10, 3, 12, 0), False),  # samedi
        (datetime(2026, 10, 4, 23, 0), False),  # dimanche
    ],
)
def test_is_open(schedule, server_time, is_open):
    assert schedule.is_open(server_time) is is_open


def test_closed_dates():
    schedule = MarketSchedule(
        week_open=time(1, 1),
        week_close=time(23, 58),
        break_start=time(23, 59),
        break_end=time(1, 1),
        closed_dates=frozenset({date(2026, 12, 25)}),
    )
    assert not schedule.is_open(datetime(2026, 12, 25, 12, 0))
    assert schedule.is_open(datetime(2026, 12, 24, 12, 0))


def test_seconds_since_open(schedule):
    assert schedule.seconds_since_open(datetime(2026, 9, 29, 1, 6)) == 300
    assert schedule.seconds_since_open(datetime(2026, 9, 29, 0, 30)) is None


def test_open_mask_matches_is_open(schedule):
    instants = pd.date_range("2026-09-26", "2026-10-06", freq="30s")  # samedi à mardi suivant
    mask = schedule.open_mask(instants)
    assert list(mask) == [schedule.is_open(t.to_pydatetime()) for t in instants]


def test_open_mask_respects_closed_dates():
    config = load_settings(CONFIG_PATH).market_hours.model_copy(update={"closed_dates": (date(2026, 12, 25),)})
    schedule = MarketSchedule.from_config(config)
    instants = pd.DatetimeIndex(["2026-12-25 12:00", "2026-12-24 12:00"])
    assert list(schedule.open_mask(instants)) == [False, True]
