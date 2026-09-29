"""Limites de risque du CLAUDE.md §3.5, identiques en backtest et en live.

- perte journalière (equity, flottant inclus) >= max_daily_loss_pct : tout fermer, pause jusqu'au lendemain ;
- drawdown depuis le plus haut >= max_drawdown_pct : arrêt total (relance manuelle) ;
- max_open_positions positions ouvertes, max_trades_per_day entrées par jour ;
- max_consecutive_losses pertes d'affilée : pause jusqu'à la session suivante.

Le « jour » est le jour de cotation du broker (jour serveur : la pause quotidienne tombe à minuit).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from goldbot.config import RiskConfig

DAILY_LOSS = "perte journalière maximale atteinte"
MAX_DRAWDOWN = "drawdown maximal atteint : arrêt total, relance manuelle"


@dataclass
class RiskLimits:
    config: RiskConfig
    peak_equity: float
    day: int | None = None
    day_start_equity: float = 0.0
    trades_today: int = 0
    consecutive_losses: int = 0
    stopped_day: int | None = None  # jour bloqué par la perte journalière
    pause_until: pd.Timestamp | None = None  # pause après une série de pertes
    halted: bool = False

    @classmethod
    def start(cls, config: RiskConfig, equity: float) -> RiskLimits:
        return cls(config=config, peak_equity=equity, day_start_equity=equity)

    def new_day(self, day: int, equity: float) -> None:
        if day != self.day:
            self.day, self.day_start_equity, self.trades_today = day, equity, 0

    def risk_money(self, equity: float) -> float:
        return equity * self.config.risk_per_trade_pct / 100.0

    def entry_block(self, now: pd.Timestamp, open_positions: int) -> str | None:
        """Raison qui interdit une nouvelle entrée maintenant, ou None si elle est permise."""
        if self.halted:
            return MAX_DRAWDOWN
        if self.stopped_day is not None and self.stopped_day == self.day:
            return DAILY_LOSS
        if self.pause_until is not None and now < self.pause_until:
            return f"{self.config.max_consecutive_losses} pertes d'affilée : pause jusqu'à la session suivante"
        if open_positions >= self.config.max_open_positions:
            return "nombre maximal de positions ouvertes"
        if self.trades_today >= self.config.max_trades_per_day:
            return "nombre maximal de trades du jour"
        return None

    def on_entry(self) -> None:
        self.trades_today += 1

    def on_exit(self, pnl: float, now: pd.Timestamp, next_session: Callable[[pd.Timestamp], pd.Timestamp]) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.pause_until = next_session(now)
            self.consecutive_losses = 0

    def on_equity(self, equity: float) -> list[str]:
        """À appeler à chaque réévaluation de l'equity. Renvoie les limites franchies à l'instant
        (la plus grave d'abord) : il faut alors tout fermer.
        """
        self.peak_equity = max(self.peak_equity, equity)
        crossed = []
        if not self.halted and equity <= self.peak_equity * (1.0 - self.config.max_drawdown_pct / 100.0):
            self.halted = True
            crossed.append(MAX_DRAWDOWN)
        daily_floor = self.day_start_equity * (1.0 - self.config.max_daily_loss_pct / 100.0)
        if self.stopped_day != self.day and equity <= daily_floor:
            self.stopped_day = self.day
            crossed.append(DAILY_LOSS)
        return crossed
