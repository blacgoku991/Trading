from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from goldbot.data.timezones import InvalidServerTime, format_offset, measured_offset

NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("moment", "hours"),
    [
        (datetime(2026, 1, 15, 12, tzinfo=UTC), 2),  # hiver
        (datetime(2026, 3, 20, 12, tzinfo=UTC), 3),  # New York déjà à l'heure d'été, pas l'Europe
        (datetime(2026, 7, 15, 12, tzinfo=UTC), 3),  # été
        (datetime(2026, 10, 28, 12, tzinfo=UTC), 3),  # Europe revenue à l'heure d'hiver, pas New York
        (datetime(2026, 11, 2, 12, tzinfo=UTC), 2),  # hiver
    ],
)
def test_server_offset_follows_new_york_dst(rule, moment, hours):
    assert rule.utc_offset_at(moment) == timedelta(hours=hours)


def test_us_releases_are_always_1530_server_time(rule):
    """08:30 à New York (NFP, CPI) = 15:30 serveur tous les jours ouvrés de 2026, semaines décalées comprises."""
    day = date(2026, 1, 1)
    while day.year == 2026:
        if day.weekday() < 5:
            release = datetime(day.year, day.month, day.day, 8, 30, tzinfo=NEW_YORK)
            assert rule.to_server(release.astimezone(UTC)) == datetime(day.year, day.month, day.day, 15, 30)
        day += timedelta(days=1)


@pytest.mark.parametrize(
    ("server", "utc"),
    [
        (datetime(2026, 1, 15, 15, 30), datetime(2026, 1, 15, 13, 30, tzinfo=UTC)),
        (datetime(2026, 3, 20, 15, 30), datetime(2026, 3, 20, 12, 30, tzinfo=UTC)),
        (datetime(2026, 7, 15, 15, 30), datetime(2026, 7, 15, 12, 30, tzinfo=UTC)),
    ],
)
def test_server_to_utc(rule, server, utc):
    assert rule.server_to_utc(server) == utc


def test_server_epoch_round_trip(rule):
    moment = datetime(2026, 9, 29, 10, 0, 0, tzinfo=UTC)
    assert rule.server_epoch_to_utc(rule.server_epoch(moment)) == moment


def test_ambiguous_server_time_is_rejected(rule):
    # 01:30 le 01/11/2026 existe deux fois à New York -> 08:30 serveur ambiguë (marché fermé à cette heure).
    with pytest.raises(InvalidServerTime, match="ambiguë"):
        rule.server_to_utc(datetime(2026, 11, 1, 8, 30))


def test_nonexistent_server_time_is_rejected(rule):
    # 02:30 le 08/03/2026 n'existe pas à New York -> 09:30 serveur inexistante.
    with pytest.raises(InvalidServerTime, match="inexistante"):
        rule.server_to_utc(datetime(2026, 3, 8, 9, 30))


def test_measured_offset_is_offset_minus_tick_age(rule):
    now = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    tick_epoch = rule.server_epoch(now) - 2.0
    assert measured_offset(tick_epoch, now) == pytest.approx(3 * 3600 - 2.0)


@pytest.mark.parametrize(
    ("offset", "text"),
    [(10800, "GMT+3"), (timedelta(hours=-4), "GMT-4"), (19800, "GMT+5:30"), (0, "GMT+0"), (10798, "GMT+3")],
)
def test_format_offset(offset, text):
    assert format_offset(offset) == text
