"""Validation anti-overfitting (CLAUDE.md §10) : Monte Carlo, fenêtres glissantes, concentration du profit."""

from __future__ import annotations

import numpy as np
import pandas as pd


def monte_carlo_drawdowns(
    r_multiples: np.ndarray, *, risk_pct: float, runs: int = 1000, skip_share: float = 0.1, seed: int = 0
) -> np.ndarray:
    """Drawdowns maximaux (%, négatifs) de runs tirages : ordre des trades mélangé, trades sautés au hasard.

    Chaque trade change l'equity de risk_pct x R % (risque fixe en % de l'equity, comme le bot).
    """
    rng = np.random.default_rng(seed)
    r = np.asarray(r_multiples, dtype="float64")
    drawdowns = np.empty(runs)
    for run in range(runs):
        sample = rng.permutation(r)
        sample = sample[rng.random(len(sample)) >= skip_share]
        equity = np.cumprod(1.0 + sample * risk_pct / 100.0)
        equity = np.concatenate(([1.0], equity))
        drawdowns[run] = (equity / np.maximum.accumulate(equity) - 1.0).min() * 100.0
    return drawdowns


def rolling_windows(trades: pd.DataFrame, freq: str = "QE") -> pd.DataFrame:
    """Trades, R total et R moyen par fenêtre calendaire (trimestre par défaut)."""
    exit_times = trades["exit_time"].dt.tz_localize(None)
    grouped = trades.groupby(exit_times.dt.to_period(freq.rstrip("E")))["r_multiple"]
    table = pd.DataFrame({"trades": grouped.size(), "R total": grouped.sum(), "R moyen": grouped.mean()})
    table.index.name = "période"
    return table


def best_trades_share(pnl: pd.Series, count: int = 5) -> float:
    """Part (%) du profit net apportée par les `count` meilleurs trades (critère : < 30 %)."""
    net = float(pnl.sum())
    return float(pnl.nlargest(count).sum()) / net * 100.0 if net > 0 else float("nan")
