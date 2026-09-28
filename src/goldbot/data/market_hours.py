"""Heures de cotation du symbole, en heure serveur (modèle Axi des métaux).

L'API Python MT5 n'expose pas les sessions de trading : elles viennent de la config
(docs/RESEARCH.md §2.5). Pause quotidienne à cheval sur minuit, ouverture le lundi,
fermeture le vendredi.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

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
        if weekday == _FRIDAY and clock >= self.week_close:
            return False
        return True

    def seconds_since_open(self, server_time: datetime) -> float | None:
        """Secondes écoulées depuis la dernière ouverture (None si le marché est fermé)."""
        if not self.is_open(server_time):
            return None
        opening = self.break_end
        if server_time.weekday() == _MONDAY:
            opening = max(opening, self.week_open)
        return (server_time - datetime.combine(server_time.date(), opening)).total_seconds()
