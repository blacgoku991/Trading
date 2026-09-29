"""Indicateurs maison, vectorisés et causaux : la valeur à la barre t n'utilise que les barres jusqu'à t.

Pas de TA-Lib (CLAUDE.md §6). Les moyennes « Wilder » sont des moyennes exponentielles
de coefficient 1/période, comme dans les définitions d'origine de l'ATR et du RSI.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(values: pd.Series, period: int) -> pd.Series:
    """Moyenne mobile exponentielle classique (coefficient 2 / (période + 1))."""
    return values.ewm(span=period, adjust=False, min_periods=period).mean()


def wilder(values: pd.Series, period: int) -> pd.Series:
    """Moyenne de Wilder (coefficient 1 / période)."""
    return values.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    previous = close.shift(1)
    ranges = pd.concat([high - low, (high - previous).abs(), (low - previous).abs()], axis=1)
    return ranges.max(axis=1, skipna=True)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range de Wilder."""
    return wilder(true_range(high, low, close), period)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI de Wilder, entre 0 et 100 (100 si aucune baisse sur la période)."""
    change = close.diff()
    gain = wilder(change.clip(lower=0.0), period)
    loss = wilder((-change).clip(lower=0.0), period)
    with np.errstate(divide="ignore", invalid="ignore"):
        value = 100.0 - 100.0 / (1.0 + gain / loss)
    return value.where(loss != 0, 100.0).where(gain.notna())


def trading_days(bars: pd.DataFrame) -> pd.DataFrame:
    """Barres M1 -> une ligne par jour de cotation (jour SERVEUR : la pause quotidienne tombe à minuit).

    Colonnes : open, high, low, close, first (index de la première barre), last (index de la dernière).
    """
    day = bars["time_server"].to_numpy() // 86_400
    grouped = bars.assign(_day=day, _row=np.arange(len(bars))).groupby("_day", sort=True)
    days = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        first=("_row", "first"),
        last=("_row", "last"),
    )
    days.index.name = "server_day"
    return days


def daily_atr_per_bar(bars: pd.DataFrame, period: int = 14) -> np.ndarray:
    """ATR journalier connu au début de chaque jour (jours précédents seulement), recopié sur chaque barre."""
    days = trading_days(bars)
    known = atr(days["high"], days["low"], days["close"], period).shift(1)
    day = bars["time_server"].to_numpy() // 86_400
    return known.reindex(day).to_numpy()
