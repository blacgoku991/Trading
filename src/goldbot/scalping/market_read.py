"""Lecture du marché : sens achat / vente relu à la clôture de chaque barre M1.

Le scalper ne prend ses cassures de 5 s que dans le sens de la lecture ; elle peut changer d'une minute à
l'autre selon la réaction des bougies (demande de l'utilisateur du 29/09 : « pas seulement des achats ou des
ventes, en fonction du marché et de la réaction des bougies »). Un modèle est une fonction
fn(bars, **params) -> tableau d'entiers (+1 achat, -1 vente, 0 pas de sens clair), une valeur par barre,
calculée avec les barres closes jusqu'à elle seulement (aucun futur) et à mémoire finie : le bot en direct
ne lit que les 30 000 dernières barres M1 et doit trouver exactement la même valeur qu'au rejeu.

Le même code sert au rejeu (backtest.py) et au bot démo (live.py).
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from goldbot.config import ScalpingConfig
from goldbot.scalping.engine import day_direction

Model = Callable[..., np.ndarray]
MODELS: dict[str, Model] = {}


def check_model(config: ScalpingConfig) -> None:
    """Refus clair au démarrage si le modèle demandé n'existe pas."""
    read = config.market_read
    if read.enabled and read.model not in MODELS:
        raise ValueError(f"market_read : modèle inconnu {read.model!r} (connus : {', '.join(sorted(MODELS))})")


def read_direction(bars: pd.DataFrame, config: ScalpingConfig) -> np.ndarray:
    """Sens permis à la clôture de chaque barre : lecture du marché, sens du jour, ou aucun filtre (zéros)."""
    if config.market_read.enabled:
        check_model(config)
        if bars.empty:
            return np.zeros(0, dtype=int)
        values = np.asarray(MODELS[config.market_read.model](bars, **config.market_read.params))
        return np.nan_to_num(values.astype("float64")).astype(int)
    if config.direction_filter:
        return day_direction(bars, config)
    return np.zeros(len(bars), dtype=int)
