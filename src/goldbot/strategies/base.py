"""Interface commune des stratégies : le même code sert au backtest et au live (CLAUDE.md §6).

Une stratégie lit des barres M1 CLÔTURÉES et renvoie des intentions d'ordre. Chaque intention
est datée par la barre dont la clôture l'a produite : elle devient active à la barre suivante.
Expiration et sortie horaire sont des instants UTC, pas des numéros de barre, pour que le
calcul ne dépende jamais de barres futures (test anti look-ahead).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

MARKET = "market"
STOP = "stop"
LONG, SHORT = 1, -1


@dataclass(frozen=True)
class OrderIntent:
    time: pd.Timestamp  # ouverture (UTC) de la barre dont la clôture produit l'intention
    side: int  # LONG ou SHORT
    kind: str  # MARKET (à l'ouverture de la barre suivante) ou STOP (déclenché au niveau price)
    price: float | None  # niveau du stop ; None au marché
    sl: float  # stop loss (obligatoire, règle 3)
    tp: float | None
    expires_at: pd.Timestamp  # un ordre STOP non déclenché avant cet instant est annulé
    exit_at: pd.Timestamp | None  # sortie au marché à cet instant si la position est encore ouverte
    tag: str  # identifiant unique du signal (idempotence en live)
    reason: str  # explication lisible, reprise dans le journal des trades

    def __post_init__(self) -> None:
        if self.side not in (LONG, SHORT):
            raise ValueError(f"sens invalide : {self.side}")
        if self.kind not in (MARKET, STOP) or (self.kind == STOP) != (self.price is not None):
            raise ValueError(f"ordre {self.kind} incohérent (prix {self.price})")
        reference = self.price
        if reference is not None and (self.sl - reference) * self.side >= 0:
            raise ValueError(f"SL {self.sl} du mauvais côté du prix d'entrée {reference}")
        if reference is not None and self.tp is not None and (self.tp - reference) * self.side <= 0:
            raise ValueError(f"TP {self.tp} du mauvais côté du prix d'entrée {reference}")


class Strategy(ABC):
    """Règles 100 % codées (règle 8 : aucun LLM dans la décision)."""

    name: str
    description: str  # une phrase, reprise en tête des rapports

    @abstractmethod
    def intents(self, bars: pd.DataFrame) -> list[OrderIntent]:
        """Intentions produites par les barres de bars, toutes considérées comme clôturées.

        bars : colonnes time (UTC), time_server, open, high, low, close (prix bid), spread (points),
        index 0..n-1. Le résultat pour une barre ne doit dépendre que des barres précédentes et d'elle-même.
        """


class Portfolio(Strategy):
    """Plusieurs stratégies sur le même compte : leurs intentions réunies, dans l'ordre du temps.

    Les limites de risque (positions, trades par jour, pertes) s'appliquent au total, comme en live.
    """

    def __init__(self, strategies: list[Strategy], *, name: str, description: str) -> None:
        self.strategies = strategies
        self.name = name
        self.description = description

    def intents(self, bars: pd.DataFrame) -> list[OrderIntent]:
        merged = [intent for strategy in self.strategies for intent in strategy.intents(bars)]
        return sorted(merged, key=lambda intent: intent.time)
