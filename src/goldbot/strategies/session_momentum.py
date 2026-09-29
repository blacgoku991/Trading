"""Momentum intraday : le mouvement depuis l'ouverture de la journée tend à se prolonger.

Idée documentée sur les actions et les matières premières (momentum intraday : le début de
séance prédit la suite). Règles, dans le fuseau de la place :
- à signal_time, mouvement = clôture - ouverture du jour de cotation (reprise après la pause) ;
- si |mouvement| >= min_move_atr x ATR journalier : ordre au marché dans le sens du mouvement ;
- SL à sl_atr x ATR journalier du prix de signal ; TP à tp_r fois le risque (0 : pas de TP) ;
- sortie à exit_time. Un seul trade par jour.
Paramètres libres : min_move_atr, sl_atr, tp_r (horaires fixés à l'avance par place).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

import numpy as np
import pandas as pd

from goldbot.indicators.core import daily_atr_per_bar
from goldbot.indicators.sessions import LocalClock, local_to_utc, minute_of_day
from goldbot.strategies.base import LONG, MARKET, SHORT, OrderIntent, Strategy


@dataclass(frozen=True)
class SessionMomentumParams:
    zone: str
    signal_time: time
    exit_time: time
    min_move_atr: float
    sl_atr: float
    tp_r: float
    atr_days: int = 14
    fade: bool = False  # True : parier sur le retour (contre le mouvement) au lieu de la poursuite


class SessionMomentum(Strategy):
    def __init__(self, params: SessionMomentumParams, *, name: str, code: str, description: str) -> None:
        self.params = params
        self.name = name
        self.code = code
        self.description = description

    def intents(self, bars: pd.DataFrame) -> list[OrderIntent]:
        p = self.params
        if bars.empty:
            return []
        clock = LocalClock.of(bars["time"], p.zone)
        signal_minute = minute_of_day(p.signal_time)
        day = bars["time_server"].to_numpy() // 86_400
        day_open = pd.Series(bars["open"].to_numpy()).groupby(day).transform("first").to_numpy()
        frame = pd.DataFrame({"date": clock.dates, "minute": clock.minutes, "day": day})
        # Barre de signal : la première qui se termine à signal_time ou après, le même jour de cotation.
        candidates = frame[frame["minute"] + 1 >= signal_minute]
        first = candidates.groupby("date").head(1)
        atr = daily_atr_per_bar(bars, p.atr_days)
        times = pd.DatetimeIndex(bars["time"])
        closes = bars["close"].to_numpy()
        dates = first["date"].to_numpy().astype("datetime64[D]")
        exits = local_to_utc(dates, p.exit_time, p.zone)
        intents = []
        for bar, exit_at in zip(first.index.to_numpy(), exits, strict=True):
            daily_atr = atr[bar]
            if np.isnan(daily_atr) or times[bar] + pd.Timedelta(minutes=1) >= exit_at:
                continue
            move = closes[bar] - day_open[bar]
            if abs(move) < p.min_move_atr * daily_atr or move == 0:
                continue
            side = LONG if (move > 0) != p.fade else SHORT
            reference = closes[bar]
            sl = reference - side * p.sl_atr * daily_atr
            tp = reference + side * p.tp_r * p.sl_atr * daily_atr if p.tp_r > 0 else None
            intents.append(
                OrderIntent(
                    time=times[bar],
                    side=side,
                    kind=MARKET,
                    price=None,
                    sl=round(sl, 2),
                    tp=None if tp is None else round(tp, 2),
                    expires_at=times[bar] + pd.Timedelta(minutes=2),
                    exit_at=exit_at,
                    tag=f"{self.code}-{str(first.loc[bar, 'date'])[:10]}",
                    reason=f"mouvement depuis l'ouverture {move:+.2f} (ATR jour {daily_atr:.2f})",
                )
            )
        return intents
