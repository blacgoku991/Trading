"""Stratégies construites depuis la config : le même assemblage sert au backtest et au live."""

from __future__ import annotations

from collections.abc import Mapping

from goldbot.config import SessionMomentumConfig
from goldbot.strategies.base import Portfolio
from goldbot.strategies.session_momentum import SessionMomentum, SessionMomentumParams

_NAMES = {
    False: ("S7 momentum intraday", "le mouvement depuis l'ouverture du jour se prolonge jusqu'à l'après-midi"),
    True: ("S8 retour en session asiatique", "le mouvement du matin asiatique a tendance à se retourner"),
}


def session_strategy(key: str, config: SessionMomentumConfig) -> SessionMomentum:
    title, idea = _NAMES[config.fade]
    return SessionMomentum(
        SessionMomentumParams(**config.model_dump(exclude={"enabled"})),
        name=f"{title} ({key})",
        code=f"{'S8' if config.fade else 'S7'}{key[:1].upper()}",
        description=f"{idea} ({key}, signal à {config.signal_time:%H:%M}, sortie à {config.exit_time:%H:%M}).",
    )


def session_portfolio(configs: Mapping[str, SessionMomentumConfig], *, include_disabled: bool = False) -> Portfolio:
    strategies = [
        session_strategy(key, config) for key, config in configs.items() if config.enabled or include_disabled
    ]
    return Portfolio(
        strategies,
        name="Portefeuille momentum de sessions",
        description="Une stratégie par session : " + " ; ".join(s.description for s in strategies),
    )
