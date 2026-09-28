"""Détection du symbole de l'or : le nom varie selon le broker et le type de compte (XAUUSD, XAUUSD.pro…)."""

from __future__ import annotations

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, SymbolSpec
from goldbot.config import SymbolConfig


class SymbolSelectionError(Exception):
    """Aucun symbole, ou plusieurs : un choix explicite est nécessaire dans la config."""


def is_gold_usd(spec: SymbolSpec) -> bool:
    name = spec.name.upper()
    is_gold = spec.currency_base == "XAU" or name.startswith(("XAUUSD", "GOLD"))
    return is_gold and spec.currency_profit == "USD"


def choose_gold_symbol(candidates: list[SymbolSpec]) -> SymbolSpec:
    """Choix automatique : exactement un symbole or/USD tradable, sinon erreur explicite."""
    tradable = [s for s in candidates if is_gold_usd(s) and s.trade_mode == C.SYMBOL_TRADE_MODE_FULL]
    if len(tradable) == 1:
        return tradable[0]
    seen = ", ".join(s.name for s in candidates) or "aucun"
    if not tradable:
        raise SymbolSelectionError(
            f"aucun symbole or/USD tradable trouvé (vus : {seen}). "
            "Indique le nom exact dans config/settings.yaml (symbol.name)."
        )
    names = ", ".join(s.name for s in tradable)
    raise SymbolSelectionError(
        f"plusieurs symboles or/USD tradables : {names}. "
        "Choisis-en un dans config/settings.yaml (symbol.name)."
    )


def resolve_gold_symbol(broker: Broker, config: SymbolConfig) -> tuple[SymbolSpec, list[SymbolSpec]]:
    """Trouve le symbole de l'or, le sélectionne dans le Market Watch et renvoie (spec, candidats)."""
    candidates = broker.symbols(config.search_group)
    name = choose_gold_symbol(candidates).name if config.is_auto else config.name.strip()
    broker.select_symbol(name)
    spec = broker.symbol(name)
    if spec.trade_mode != C.SYMBOL_TRADE_MODE_FULL:
        raise SymbolSelectionError(f"{name} n'est pas ouvert au trading (trade_mode={spec.trade_mode})")
    return spec, candidates
