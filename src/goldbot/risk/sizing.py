"""Taille de position à risque fixe (CLAUDE.md §9) : jamais d'arrondi du risque vers le haut."""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def floor_to_step(volume: float, step: float) -> float:
    """Arrondi vers le bas au pas de volume, sans erreur de flottant (0.1 + 0.2 != 0.3)."""
    steps = (Decimal(str(volume)) / Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
    return float(steps * Decimal(str(step)))


def position_size(
    risk_money: float, loss_per_lot: float, *, volume_min: float, volume_max: float, volume_step: float
) -> float:
    """Lots = risque en devise / perte pour 1 lot au SL, arrondi vers le bas au pas de volume.

    Renvoie 0 si le résultat est sous le volume minimal : le trade est alors ignoré, jamais grossi.
    """
    if risk_money <= 0 or loss_per_lot <= 0:
        return 0.0
    lots = floor_to_step(min(risk_money / loss_per_lot, volume_max), volume_step)
    return lots if lots >= volume_min else 0.0
