"""Test anti look-ahead automatique (CLAUDE.md §9).

Les intentions et les mises à jour du stop calculées sur un historique tronqué à la barre k
doivent être exactement celles de l'historique complet produites jusqu'à cette barre : une
stratégie qui regarde le futur donne un résultat différent quand ce futur n'existe pas encore.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from goldbot.strategies.base import Strategy


def lookahead_violations(strategy: Strategy, bars: pd.DataFrame, cuts: Iterable[int]) -> list[str]:
    """Liste des différences (vide si la stratégie est causale sur ces points de coupe)."""
    full = strategy.intents(bars)
    full_updates = strategy.stop_updates(bars)
    problems = []
    for cut in cuts:
        last = bars["time"].iloc[cut - 1]
        expected = [intent for intent in full if intent.time <= last]
        truncated = strategy.intents(bars.iloc[:cut])
        if truncated != expected:
            differences = [f"{a.tag}" for a, b in zip(truncated, expected, strict=False) if a != b]
            problems.append(
                f"coupe après {last} : {len(truncated)} intentions au lieu de {len(expected)}"
                + (f" (différentes : {', '.join(differences[:3])})" if differences else "")
            )
        expected_updates = [update for update in full_updates if update.time <= last]
        truncated_updates = strategy.stop_updates(bars.iloc[:cut])
        if truncated_updates != expected_updates:
            problems.append(
                f"coupe après {last} : {len(truncated_updates)} mises à jour du stop au lieu de {len(expected_updates)}"
            )
    return problems
