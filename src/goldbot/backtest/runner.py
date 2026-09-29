"""Chargement des données et lancement d'un backtest (scripts, recherche, tests)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from goldbot.backtest.engine import Backtest, BacktestResult, Costs, Instrument
from goldbot.config import BacktestConfig, RiskConfig
from goldbot.data.history import load_bars, minute_history_start
from goldbot.strategies.base import Strategy


@dataclass(frozen=True)
class Dataset:
    bars: pd.DataFrame
    symbol: dict  # section symbol de manifest.json

    @property
    def instrument(self) -> Instrument:
        return Instrument.from_symbol(self.symbol)


def load_dataset(folder: Path, *, start: date | None = None, end: date | None = None) -> Dataset:
    """Barres M1 exportées, à partir du vrai historique minute, sur [start, end[ (dates UTC)."""
    manifest = json.loads((Path(folder) / "manifest.json").read_text(encoding="utf-8"))
    symbol = manifest["symbol"]
    bars = load_bars(folder, symbol["name"])
    first = minute_history_start(bars)
    if first is None:
        raise ValueError("aucune période avec de vraies barres d'une minute")
    keep = bars["time_server"] >= first
    if start is not None:
        keep &= bars["time"] >= pd.Timestamp(start, tz="UTC")
    if end is not None:
        keep &= bars["time"] < pd.Timestamp(end, tz="UTC")
    return Dataset(bars[keep].reset_index(drop=True), symbol)


def costs_for(
    config: BacktestConfig,
    symbol: dict,
    *,
    spread_multiplier: float | None = None,
    slippage_points: float | None = None,
) -> Costs:
    """Coûts de la config et swaps réels du symbole (mode points de MT5)."""
    if symbol.get("swap_mode") != 1:  # SYMBOL_SWAP_MODE_POINTS
        raise ValueError(f"mode de swap {symbol.get('swap_mode')} non géré par le backtest (seul le mode points l'est)")
    return Costs(
        spread_multiplier=spread_multiplier or config.spread_multiplier,
        min_spread_points=config.min_spread_points,
        slippage_points=config.slippage_points if slippage_points is None else slippage_points,
        commission_per_lot_side=config.commission_per_lot_side,
        swap_long_points=float(symbol["swap_long"]),
        swap_short_points=float(symbol["swap_short"]),
        # MT5 : dimanche = 0 ; Python : lundi = 0.
        triple_swap_weekday=(int(symbol["swap_rollover3days"]) - 1) % 7,
    )


def run(
    strategy: Strategy,
    dataset: Dataset,
    *,
    costs: Costs,
    risk: RiskConfig,
    initial_equity: float,
    halt_on_drawdown: bool = False,
) -> BacktestResult:
    backtest = Backtest(
        dataset.bars,
        instrument=dataset.instrument,
        costs=costs,
        risk=risk,
        initial_equity=initial_equity,
        halt_on_drawdown=halt_on_drawdown,
    )
    return backtest.run(strategy.intents(dataset.bars))
