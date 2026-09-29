"""Heures locales des places de marché (Londres, New York…), avec zoneinfo : jamais d'offset fixe.

Les changements d'heure européens et américains sont décalés de quelques semaines par an :
chaque session est définie dans le fuseau de sa place (CLAUDE.md §8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import numpy as np
import pandas as pd

LONDON = "Europe/London"
NEW_YORK = "America/New_York"


@dataclass(frozen=True)
class LocalClock:
    """Pour chaque barre : date locale et minute du jour dans le fuseau donné."""

    dates: np.ndarray  # datetime64[D]
    minutes: np.ndarray  # 0 à 1439

    @classmethod
    def of(cls, times_utc: pd.Series, zone: str) -> LocalClock:
        local = times_utc.dt.tz_convert(zone)
        minutes = (local.dt.hour * 60 + local.dt.minute).to_numpy()
        dates = local.dt.tz_localize(None).dt.floor("D").to_numpy().astype("datetime64[D]")
        return cls(dates, minutes)


def minute_of_day(clock: time) -> int:
    return clock.hour * 60 + clock.minute


def local_to_utc(days: np.ndarray, clock: time, zone: str) -> pd.DatetimeIndex:
    """Dates locales + heure locale -> instants UTC (heure inexistante : décalée ; ambiguë : la première)."""
    naive = pd.DatetimeIndex(pd.to_datetime(days)) + pd.Timedelta(hours=clock.hour, minutes=clock.minute)
    local = naive.tz_localize(zone, ambiguous=np.ones(len(naive), dtype=bool), nonexistent="shift_forward")
    return local.tz_convert("UTC")


def next_session_start(after_utc: pd.Timestamp, sessions: tuple[tuple[str, time], ...]) -> pd.Timestamp:
    """Prochaine ouverture de session (fuseau, heure locale) strictement après l'instant donné."""
    candidates = []
    for zone, clock in sessions:
        local_day = after_utc.tz_convert(zone).date()
        for offset in (0, 1, 2):
            day = np.array([np.datetime64(date.fromordinal(local_day.toordinal() + offset))])
            start = local_to_utc(day, clock, zone)[0]
            if start > after_utc:
                candidates.append(start)
                break
    return min(candidates)
