"""Statistiques d'un backtest (CLAUDE.md §9) : rendement, risque, régularité, répartition."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from goldbot.backtest.engine import BacktestResult

TRADING_DAYS_PER_YEAR = 252
# Sessions par heure d'entrée à Londres : Asie avant 8 h, Londres jusqu'à 13 h, New York ensuite.
_SESSION_BINS = (-1, 7, 12, 23)
_SESSION_LABELS = ("Asie", "Londres", "New York")
_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def max_drawdown(equity: pd.Series, initial: float) -> tuple[float, pd.Timedelta]:
    """Plus forte baisse depuis un plus haut (en %, négative) et plus longue durée sous un plus haut."""
    values = pd.concat([pd.Series([initial], index=equity.index[:1] - pd.Timedelta(seconds=1)), equity])
    peak = values.cummax()
    drawdown = float((values / peak - 1.0).min() * 100.0)
    underwater = values < peak
    longest = pd.Timedelta(0)
    start = None
    for moment, below in underwater.items():
        if below and start is None:
            start = moment
        elif not below and start is not None:
            longest = max(longest, moment - start)
            start = None
    if start is not None:
        longest = max(longest, values.index[-1] - start)
    return drawdown, longest


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else math.inf if numerator > 0 else 0.0


def summarize(result: BacktestResult) -> dict[str, float]:
    """Indicateurs principaux (montants dans la devise du backtest, rendements en %)."""
    trades = result.trades
    initial = result.initial_equity
    final = float(result.equity.iloc[-1]) if len(result.equity) else initial
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] < 0]
    gross_profit, gross_loss = float(wins["pnl"].sum()), float(-losses["pnl"].sum())
    drawdown, underwater = max_drawdown(result.equity, initial) if len(result.equity) else (0.0, pd.Timedelta(0))
    daily = result.daily_equity
    returns = pd.concat([pd.Series([initial]), daily.reset_index(drop=True)]).pct_change().dropna()
    mean, std = float(returns.mean()), float(returns.std())
    downside = float(np.sqrt((returns.clip(upper=0.0) ** 2).mean()))
    years = (daily.index[-1] - daily.index[0]).days / 365.25 if len(daily) > 1 else 0.0
    cagr = ((final / initial) ** (1 / years) - 1) * 100.0 if years > 0 and final > 0 else 0.0
    net = final - initial
    best_five = float(trades["pnl"].nlargest(5).sum())
    exposure = (trades["exit_time"] - trades["entry_time"]).sum() if len(trades) else pd.Timedelta(0)
    period = daily.index[-1] - daily.index[0] if len(daily) > 1 else pd.Timedelta(0)
    return {
        "trades": len(trades),
        "net_profit": net,
        "return_pct": net / initial * 100.0,
        "cagr_pct": cagr,
        "profit_factor": _ratio(gross_profit, gross_loss),
        "expectancy_r": float(trades["r_multiple"].mean()) if len(trades) else 0.0,
        "win_rate_pct": float((trades["pnl"] > 0).mean() * 100.0) if len(trades) else 0.0,
        "avg_win_r": float(wins["r_multiple"].mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses["r_multiple"].mean()) if len(losses) else 0.0,
        "max_drawdown_pct": drawdown,
        "max_underwater_days": underwater.total_seconds() / 86_400,
        "sharpe": mean / std * math.sqrt(TRADING_DAYS_PER_YEAR) if std > 0 else 0.0,
        "sortino": mean / downside * math.sqrt(TRADING_DAYS_PER_YEAR) if downside > 0 else 0.0,
        "calmar": cagr / abs(drawdown) if drawdown < 0 else 0.0,
        "exposure_pct": exposure / period * 100.0 if period > pd.Timedelta(0) else 0.0,
        "best_five_share_pct": best_five / net * 100.0 if net > 0 else math.nan,
        "avg_mae_r": float(trades["mae_r"].mean()) if len(trades) else 0.0,
        "avg_mfe_r": float(trades["mfe_r"].mean()) if len(trades) else 0.0,
        "trades_per_month": len(trades) / (years * 12) if years > 0 else 0.0,
    }


def monthly_returns(result: BacktestResult) -> pd.DataFrame:
    """Rendement de chaque mois (%), années en lignes et mois en colonnes."""
    daily = result.daily_equity
    if daily.empty:
        return pd.DataFrame()
    month_end = daily.groupby(daily.index.tz_localize(None).to_period("M")).last()
    previous = month_end.shift(1).fillna(result.initial_equity)
    returns = (month_end / previous - 1.0) * 100.0
    table = pd.DataFrame({"année": returns.index.year, "mois": returns.index.month, "r": returns.to_numpy()})
    return table.pivot(index="année", columns="mois", values="r")


def breakdown(trades: pd.DataFrame, zone: str) -> dict[str, pd.DataFrame]:
    """Nombre de trades, R moyen et R total par session, jour de la semaine, heure d'entrée et année."""
    if trades.empty:
        return {}
    london_hour = trades["entry_time"].dt.tz_convert("Europe/London").dt.hour
    local = trades["entry_time"].dt.tz_convert(zone)
    keys = {
        "session": pd.cut(london_hour, bins=_SESSION_BINS, labels=_SESSION_LABELS),
        "jour": local.dt.weekday.map(dict(enumerate(_WEEKDAYS))),
        "heure": local.dt.hour,
        "année": local.dt.year,
        "sens": trades["side"].map({1: "achat", -1: "vente"}),
        "sortie": trades["exit_reason"],
    }
    tables = {}
    for name, key in keys.items():
        grouped = trades.groupby(key.to_numpy(), observed=True)["r_multiple"]
        table = pd.DataFrame({"trades": grouped.size(), "R moyen": grouped.mean(), "R total": grouped.sum()})
        table.index.name = name
        tables[name] = table
    return tables
