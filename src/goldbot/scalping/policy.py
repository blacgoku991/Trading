"""Règles d'entrée communes au rejeu et au bot démo : mêmes entrées, même décision.

Trois cas à ne pas confondre :
- nouveau signal : une occasion distincte (clé d'occasion différente de celles des trades ouverts) ;
  il compte dans les limites (positions, risque cumulé, entrées par minute, délai entre entrées) ;
- fractionnement : un même trade envoyé en plusieurs ordres (volume au-delà du maximum par ordre, ou
  exécution partielle). Les ordres partagent l'identifiant du signal (#1, #2…) et son budget de risque ;
  ils comptent pour une seule entrée ;
- doublon accidentel : le même signal (même identifiant) ou la même occasion (même clé) rencontré une
  deuxième fois. Jamais de deuxième ordre : il est compté, pas envoyé.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from goldbot.config import ScalpingConfig


@dataclass(frozen=True)
class Exposure:
    """Trade ouvert connu des règles d'entrée (envoyé au broker ou simulé)."""

    tag: str
    key: str
    side: int
    risk: float  # perte au stop, dans la devise du compte (tous ses ordres)


class EntryPolicy:
    def __init__(self, config: ScalpingConfig) -> None:
        self.cfg = config
        self.entries: deque[int] = deque()  # heures (ms) des entrées acceptées, 60 dernières secondes

    def refusal(
        self,
        now_ms: int,
        *,
        key: str,
        risk: float,
        equity: float,
        day_result: float,
        day_start_equity: float,
        open_trades: list[Exposure],
    ) -> str | None:
        """Motif du refus, ou None si l'entrée est permise."""
        cfg = self.cfg
        if day_result <= -cfg.daily_loss_pct / 100 * day_start_equity:
            return f"perte du jour atteinte : {day_result:+.2f}, limite -{cfg.daily_loss_pct:g} %"
        if any(trade.key == key for trade in open_trades):
            return "même occasion déjà en position : doublon évité"
        if len(open_trades) >= cfg.max_open_positions:
            return f"positions ouvertes au maximum : {cfg.max_open_positions}"
        if sum(trade.risk for trade in open_trades) + risk > cfg.max_total_risk_pct / 100 * equity:
            return f"risque cumulé au maximum : {cfg.max_total_risk_pct:g} % de l'equity"
        while self.entries and self.entries[0] <= now_ms - 60_000:
            self.entries.popleft()
        if len(self.entries) >= cfg.max_entries_per_minute:
            return f"entrées par minute au maximum : {cfg.max_entries_per_minute} sur 60 s"
        if self.entries and now_ms - self.entries[-1] < cfg.min_seconds_between_entries * 1000:
            return f"délai entre deux entrées : moins de {cfg.min_seconds_between_entries:g} s"
        return None

    def accept(self, now_ms: int) -> None:
        self.entries.append(now_ms)


def split_volume(volume: float, volume_max: float, volume_step: float) -> list[float]:
    """Fractionnement d'un trade en ordres d'au plus volume_max lots : parts égales, multiples du pas."""
    steps = round(volume / volume_step)
    if steps <= 0:
        return []
    max_steps = max(1, int(volume_max / volume_step + 1e-9))
    count = -(-steps // max_steps)
    base, extra = divmod(steps, count)
    return [round((base + (1 if i < extra else 0)) * volume_step, 8) for i in range(count)]


def reason_key(message: str) -> str:
    """« stop trop loin : 7.10 $ > 6.00 $ » -> « stop trop loin » (pour compter les refus par motif)."""
    return message.split(" :")[0]
