"""S1 Asian Range Breakout (CLAUDE.md §10).

Pendant la nuit européenne (session asiatique), l'or évolue souvent dans un range étroit.
À l'ouverture de Londres, la liquidité arrive et le prix sort fréquemment de ce range.
Règles communes aux cassures de range : strategies/range_breakout.py, en heure de Londres.
Paramètres libres : buffer_atr, tp_r, min_range_atr, max_range_atr (horaires fixés à l'avance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from goldbot.indicators.sessions import LONDON
from goldbot.strategies.range_breakout import RangeBreakoutParams, SessionRangeBreakout


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


class AsianBreakout(SessionRangeBreakout):
    def __init__(self, params: AsianBreakoutParams | None = None) -> None:
        p = params or AsianBreakoutParams()
        super().__init__(
            RangeBreakoutParams(zone=LONDON, **p.__dict__),
            name="S1 cassure du range asiatique",
            code="S1",
            description=(
                "Ordres stop de part et d'autre du range de la nuit (00:00-07:00 à Londres), SL de l'autre côté du "
                "range, TP en multiple du risque, sortie en fin de journée."
            ),
        )
