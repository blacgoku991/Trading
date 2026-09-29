"""Contrôle qualité : trous, barres hors cotation, pics, spreads, validation de la règle d'heure serveur."""

import numpy as np
import pandas as pd
import pytest

from goldbot.broker.fake_broker import make_bars, make_ticks
from goldbot.data.history import bars_frame, ticks_frame
from goldbot.data.quality import (
    bar_spread_vs_ticks,
    daily_close_check,
    find_gaps,
    off_hours,
    ohlc_errors,
    render_report,
    server_index,
    spikes,
)
from tests.conftest import open_instants_ms, open_minutes, server_epoch_of


def _bars(rule, seconds, **kwargs):
    return bars_frame(make_bars(np.asarray(seconds, dtype="int64"), **kwargs), rule)


def test_gap_across_the_daily_break_is_one_gap(schedule):
    minutes = open_minutes(schedule, "2026-01-05", "2026-01-09")
    removed = ((minutes >= server_epoch_of("2026-01-06 23:50")) & (minutes < server_epoch_of("2026-01-07 01:11"))) | (
        minutes == server_epoch_of("2026-01-08 12:00")
    )
    expected, gaps = find_gaps(server_index(minutes[~removed]), schedule)
    assert expected == len(minutes)
    assert [g.minutes for g in gaps] == [20, 1]  # 23:50-23:58 puis 01:00-01:10, pause non comptée
    assert gaps[0].first_missing == pd.Timestamp("2026-01-06 23:50")
    assert gaps[0].last_missing == pd.Timestamp("2026-01-07 01:10")


def test_no_gap_on_complete_data(schedule):
    minutes = open_minutes(schedule, "2026-01-05", "2026-01-10")
    assert find_gaps(server_index(minutes), schedule)[1] == []


def test_off_hours_bars_are_classified(schedule):
    times = server_index(
        [
            server_epoch_of("2026-01-10 12:00"),  # samedi
            server_epoch_of("2026-01-07 00:30"),  # pause quotidienne
            server_epoch_of("2026-01-09 23:58"),  # vendredi après la fermeture
            server_epoch_of("2026-01-07 12:00"),  # ouvert
        ]
    )
    counts = off_hours(times, schedule)
    assert counts["week-end (samedi, dimanche)"] == 1
    assert counts["pause quotidienne"] == 1
    assert counts["vendredi après la fermeture"] == 1
    assert sum(counts.values()) == 3


def test_ohlc_errors(rule, schedule):
    bars = _bars(rule, open_minutes(schedule, "2026-01-06 10:00", "2026-01-06 10:10"))
    assert ohlc_errors(bars) == 0
    bars.loc[3, "high"] = bars.loc[3, "low"] - 1
    bars.loc[5, "close"] = 0.0
    assert ohlc_errors(bars) == 2


def test_reverting_spike_is_flagged(rule, schedule):
    bars = _bars(rule, open_minutes(schedule, "2026-01-05", "2026-01-08"))
    k = 2000
    bars.loc[k, "close"] += 30.0  # pic isolé : la barre suivante revient au niveau normal
    top = spikes(bars, point=0.01)
    assert len(top) == 1  # le retour au niveau normal n'est pas un second pic
    assert top.loc[0, "time"] == bars.loc[k, "time"] and bool(top.loc[0, "reverted"])


# --- Règle d'heure serveur -----------------------------------------------------------------------


def _gold_minutes_utc(start, end):
    """Minutes UTC où l'or cote, d'après les horaires de New York (pause 16:59-18:01, week-end)."""
    utc = pd.date_range(start, end, freq="min", tz="UTC", inclusive="left")
    ny = utc.tz_convert("America/New_York")
    clock = np.asarray(ny.hour * 60 + ny.minute)
    weekday = np.asarray(ny.weekday)
    in_break = (clock >= 16 * 60 + 59) & (clock < 18 * 60 + 1)
    closed = in_break | (weekday == 5) | ((weekday == 4) & (clock >= 16 * 60 + 59))
    closed |= (weekday == 6) & (clock < 18 * 60 + 1)
    return utc[~closed]


def _server_seconds(naive):
    return ((naive - pd.Timestamp(0)) // pd.Timedelta(seconds=1)).to_numpy()


@pytest.fixture(scope="module")
def gold_utc():
    # Un an complet : heure d'été et d'hiver de New York, et les semaines décalées US/UE.
    return _gold_minutes_utc("2025-01-06", "2026-01-05")


def test_rule_is_confirmed_when_the_server_follows_new_york_plus_7(rule, gold_utc):
    server = gold_utc.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    check = daily_close_check(_bars(rule, _server_seconds(server)), rule)
    assert check.consistent, check.explanation()
    assert set(check.table["heure"]) == {"16:58"} and check.odd_weeks == []
    assert set(check.table["période"]) == {"heure d'été", "heure d'hiver"}


def test_rule_is_rejected_when_the_server_ignores_us_daylight_saving(rule, gold_utc):
    server = gold_utc.tz_localize(None) + pd.Timedelta(hours=2)  # GMT+2 fixe toute l'année
    check = daily_close_check(_bars(rule, _server_seconds(server)), rule)
    assert not check.consistent
    summer = check.table[check.table["période"] == "heure d'été"]["heure"]
    assert set(summer) == {"15:58"} and "16:58" in set(check.table["heure"])


def test_rule_is_rejected_when_the_server_follows_european_daylight_saving(rule, gold_utc):
    # GMT+2 / GMT+3 avec les dates européennes : faux seulement pendant les semaines décalées.
    athens = gold_utc.tz_convert("Europe/Athens").tz_localize(None)
    check = daily_close_check(_bars(rule, _server_seconds(athens)), rule)
    assert not check.consistent
    # Mars : l'Europe passe à l'heure d'été 3 semaines après New York ; fin octobre, 1 semaine avant.
    assert check.odd_weeks == ["2025-W11 (15:58)", "2025-W12 (15:58)", "2025-W13 (15:58)", "2025-W44 (15:58)"]


def test_one_early_close_does_not_condemn_the_rule(rule, gold_utc):
    server = gold_utc.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=7)
    bars = _bars(rule, _server_seconds(server))
    # Veille de fête : clôture anticipée à 13:30 à New York (20:30 heure serveur) un mercredi.
    early = (bars["time_server"] > server_epoch_of("2025-12-24 20:30")) & (
        bars["time_server"] < server_epoch_of("2025-12-25 00:00")
    )
    check = daily_close_check(bars[~early], rule)
    assert check.consistent, check.explanation()


# --- Spread ---------------------------------------------------------------------------------------


def test_bar_spread_statistic_is_identified(rule, schedule):
    start = server_epoch_of("2026-01-06 10:00")
    offsets = np.arange(0, 600, 5)  # un tick toutes les 5 s pendant 10 minutes
    ticks = make_ticks((start + offsets) * 1000)
    widths = np.where(offsets % 15 == 0, 0.30, 0.15)  # spread variable dans chaque minute
    ticks["ask"] = np.round(ticks["bid"] + widths, 2)
    tick_frame = ticks_frame(ticks, rule)
    bars = _bars(rule, start + np.arange(0, 600, 60), spread=15)  # MT5 : spread minimal de la minute (hypothèse)
    comparison = bar_spread_vs_ticks(bars, tick_frame, point=0.01)
    assert comparison.loc[0, "statistique"] == "min"
    assert comparison.loc[0, "égalité exacte"] == 1.0
    assert comparison.loc[0, "minutes"] == 10


# --- Rapport ---------------------------------------------------------------------------------------


def test_report_on_clean_data_has_no_alert(rule, schedule):
    bars = _bars(rule, open_minutes(schedule, "2026-01-05", "2026-01-17"))
    ticks = ticks_frame(make_ticks(open_instants_ms(schedule, "2026-01-12", "2026-01-14")), rule)
    text, alerts = render_report(
        bars, ticks, rule=rule, schedule=schedule, point=0.01, zone="Europe/Paris", file_problems=[]
    )
    assert alerts == []
    for title in ("## Trous", "## Règle d'heure serveur", "## Ticks", "## Alertes"):
        assert title in text
    assert "COHÉRENTE" in text


def test_report_raises_alerts_on_bad_data(rule, schedule):
    bars = _bars(rule, open_minutes(schedule, "2026-01-05", "2026-01-17"))
    bars = pd.concat([bars, bars.head(2)], ignore_index=True)  # doublons
    # Une heure entière de barres le samedi : ce qu'une règle d'heure fausse produirait.
    weekend = _bars(rule, [server_epoch_of("2026-01-10 12:00") + 60 * k for k in range(60)])
    bars = pd.concat([bars, weekend], ignore_index=True)
    text, alerts = render_report(
        bars, None, rule=rule, schedule=schedule, point=0.01, zone="Europe/Paris", file_problems=["x : manquant"]
    )
    assert "x : manquant" in alerts
    assert any("doublons" in alert for alert in alerts)
    assert any("hors heures de cotation" in alert for alert in alerts)
    assert "## Ticks" not in text


def test_a_few_off_hours_minutes_are_reported_without_alert(rule, schedule):
    bars = _bars(rule, open_minutes(schedule, "2026-01-05", "2026-01-17"))
    friday = _bars(rule, [server_epoch_of("2026-01-09 23:58")])  # ancien horaire de fermeture du vendredi
    text, alerts = render_report(
        pd.concat([bars, friday], ignore_index=True),
        None,
        rule=rule,
        schedule=schedule,
        point=0.01,
        zone="Europe/Paris",
        file_problems=[],
    )
    assert "vendredi après la fermeture : 1" in text
    assert alerts == []
