"""S1 Asian Range Breakout (CLAUDE.md §10).

Pendant la nuit européenne (session asiatique), l'or évolue souvent dans un range étroit.
À l'ouverture de Londres, la liquidité arrive et le prix sort fréquemment de ce range.

Règles, en heure de Londres (changements d'heure gérés par zoneinfo) :
- range = plus haut et plus bas des barres de range_start à range_end ;
- filtre : taille du range entre min_range_atr et max_range_atr fois l'ATR journalier
  (ATR des jours précédents seulement) ;
- à la clôture du range : ordre stop d'achat au plus haut + buffer, ordre stop de vente au
  plus bas - buffer (buffer = buffer_atr x ATR journalier) ; un seul trade par côté et par jour ;
- SL de l'autre côté du range ; TP à tp_r fois le risque ;
- ordres non déclenchés annulés à entry_end ; positions fermées à exit_time.
Paramètres libres : buffer_atr, tp_r, min_range_atr, max_range_atr (horaires fixés à l'avance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import numpy as np
import pandas as pd

from goldbot.indicators.core import daily_atr_per_bar
from goldbot.indicators.sessions import LONDON, LocalClock, local_to_utc, minute_of_day
from goldbot.strategies.base import LONG, SHORT, STOP, OrderIntent, Strategy

# Un range n'est retenu que si au moins cette part des minutes de la fenêtre a une barre (jours fériés).
MIN_RANGE_COVERAGE = 0.5


@dataclass(frozen=True)
class AsianBreakoutParams:
    range_start: time = time(0, 0)
    range_end: time = time(7, 0)
    entry_end: time = time(11, 0)
    exit_time: time = time(20, 0)
    buffer_atr: float = 0.05
    tp_r: float = 1.5
    min_range_atr: float = 0.15
    max_range_atr: float = 0.8
    atr_days: int = 14


class AsianBreakout(Strategy):
    name = "S1 cassure du range asiatique"
    description = (
        "Ordres stop de part et d'autre du range de la nuit (00:00-07:00 à Londres), SL de l'autre côté du range, "
        "TP en multiple du risque, sortie en fin de journée."
    )

    def __init__(self, params: AsianBreakoutParams | None = None) -> None:
        self.params = params or AsianBreakoutParams()

    def intents(self, bars: pd.DataFrame) -> list[OrderIntent]:
        p = self.params
        if bars.empty:
            return []
        clock = LocalClock.of(bars["time"], LONDON)
        start, end = minute_of_day(p.range_start), minute_of_day(p.range_end)
        frame = pd.DataFrame(
            {
                "date": clock.dates,
                "minute": clock.minutes,
                "high": bars["high"].to_numpy(),
                "low": bars["low"].to_numpy(),
            }
        )
        in_window = (frame["minute"] >= start) & (frame["minute"] < end)
        ranges = frame[in_window].groupby("date").agg(high=("high", "max"), low=("low", "min"), bars=("high", "size"))
        # Le range est complet à la clôture de la première barre qui se termine à range_end ou après.
        closing = frame[(frame["minute"] + 1 >= end) & (frame["minute"] >= start)]
        emission = closing.groupby("date").head(1)
        emission = pd.Series(emission.index.to_numpy(), index=emission["date"].to_numpy())
        ranges = ranges[ranges["bars"] >= MIN_RANGE_COVERAGE * (end - start)].join(emission.rename("bar"), how="inner")
        if ranges.empty:
            return []
        atr = daily_atr_per_bar(bars, p.atr_days)
        dates = ranges.index.to_numpy().astype("datetime64[D]")
        expiries = local_to_utc(dates, p.entry_end, LONDON)
        exits = local_to_utc(dates, p.exit_time, LONDON)
        times = pd.DatetimeIndex(bars["time"])

        intents: list[OrderIntent] = []
        for row, expires_at, exit_at in zip(ranges.itertuples(), expiries, exits, strict=True):
            bar = int(row.bar)
            daily_atr = atr[bar]
            size = row.high - row.low
            if np.isnan(daily_atr) or not p.min_range_atr * daily_atr <= size <= p.max_range_atr * daily_atr:
                continue
            signal_time = times[bar]
            if signal_time + pd.Timedelta(minutes=1) >= expires_at:
                continue  # plus de temps pour entrer
            buffer = p.buffer_atr * daily_atr
            day = str(row.Index)[:10]
            description = f"range asiatique {row.low:.2f}-{row.high:.2f} ({size:.2f}), ATR jour {daily_atr:.2f}"
            for side, entry, sl in ((LONG, row.high + buffer, row.low), (SHORT, row.low - buffer, row.high)):
                risk = (entry - sl) * side
                intents.append(
                    OrderIntent(
                        time=signal_time,
                        side=side,
                        kind=STOP,
                        price=round(entry, 2),
                        sl=round(sl, 2),
                        tp=round(entry + side * p.tp_r * risk, 2),
                        expires_at=expires_at,
                        exit_at=exit_at,
                        tag=f"S1-{day}-{'L' if side == LONG else 'S'}",
                        reason=f"cassure {'haussière' if side == LONG else 'baissière'} du {description}",
                    )
                )
        return intents
