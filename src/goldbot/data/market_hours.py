"""Heures de cotation du symbole, en heure serveur (modèle Axi des métaux).

L'API Python MT5 n'expose pas les sessions de trading : elles viennent de la config
(docs/RESEARCH.md §2.5). Pause quotidienne à cheval sur minuit, ouverture le lundi,
fermeture le vendredi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from goldbot.config import MarketHoursConfig

_MONDAY, _FRIDAY = 0, 4


@dataclass(frozen=True)
class MarketSchedule:
    week_open: time
    week_close: time
    break_start: time
    break_end: time
    closed_dates: frozenset[date]

    @classmethod
    def from_config(cls, config: MarketHoursConfig) -> MarketSchedule:
        return cls(
            week_open=config.week_open,
            week_close=config.week_close,
            break_start=config.daily_break_start,
            break_end=config.daily_break_end,
            closed_dates=frozenset(config.closed_dates),
        )

    def is_open(self, server_time: datetime) -> bool:
        """Vrai si le symbole cote à cette heure serveur."""
        weekday, clock = server_time.weekday(), server_time.time()
        if weekday > _FRIDAY or server_time.date() in self.closed_dates:
            return False
        if clock >= self.break_start or clock < self.break_end:  # pause quotidienne
            return False
        if weekday == _MONDAY and clock < self.week_open:
            return False
        after_friday_close = weekday == _FRIDAY and clock >= self.week_close
        return not after_friday_close

    def open_mask(self, server_times: pd.DatetimeIndex) -> np.ndarray:
        """Version vectorisée de is_open, pour des heures serveur naïves (mêmes règles)."""
        weekday = np.asarray(server_times.weekday)
        days = server_times.normalize()
        clock = np.asarray((server_times - days) / pd.Timedelta(seconds=1))
        in_break = (clock >= seconds_of_day(self.break_start)) | (clock < seconds_of_day(self.break_end))
        before_week_open = (weekday == _MONDAY) & (clock < seconds_of_day(self.week_open))
        after_week_close = (weekday == _FRIDAY) & (clock >= seconds_of_day(self.week_close))
        closed_day = np.asarray(days.isin(pd.to_datetime(sorted(self.closed_dates))))
        return (weekday <= _FRIDAY) & ~in_break & ~before_week_open & ~after_week_close & ~closed_day

    def seconds_since_open(self, server_time: datetime) -> float | None:
        """Secondes écoulées depuis la dernière ouverture (None si le marché est fermé)."""
        if not self.is_open(server_time):
            return None
        opening = self.break_end
        if server_time.weekday() == _MONDAY:
            opening = max(opening, self.week_open)
        return (server_time - datetime.combine(server_time.date(), opening)).total_seconds()


def seconds_of_day(clock: time) -> float:
    return clock.hour * 3600 + clock.minute * 60 + clock.second + clock.microsecond / 1e6
