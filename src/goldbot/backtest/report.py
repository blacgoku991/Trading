"""Rapport de backtest en français (markdown) et courbe d'equity (PNG)."""

from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from goldbot.backtest.engine import BacktestResult, Costs
from goldbot.backtest.metrics import breakdown, monthly_returns

_MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")


def equity_png(result: BacktestResult, path: Path, title: str) -> None:
    """Courbe d'equity et drawdown, en image."""
    import matplotlib

    matplotlib.use("Agg")  # pas d'écran : fonctionne partout
    import matplotlib.pyplot as plt

    equity = result.daily_equity
    peak = equity.cummax()
    figure, (top, bottom) = plt.subplots(2, 1, figsize=(11, 6), sharex=True, height_ratios=(3, 1))
    top.plot(equity.index, equity.to_numpy(), color="#1f5f99", linewidth=1.2)
    top.axhline(result.initial_equity, color="#888888", linewidth=0.8, linestyle="--")
    top.set_title(title)
    top.set_ylabel("equity")
    top.grid(alpha=0.3)
    bottom.fill_between(equity.index, (equity / peak - 1.0).to_numpy() * 100.0, 0.0, color="#b03a2e", alpha=0.6)
    bottom.set_ylabel("drawdown %")
    bottom.grid(alpha=0.3)
    figure.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=110)
    plt.close(figure)


def _number(value: float, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, float) and math.isinf(value):
        return "infini"
    return f"{value:,.{digits}f}".replace(",", " ").replace(".", ",")


def _table(frame: pd.DataFrame, digits: int = 2) -> list[str]:
    header = "| " + " | ".join([str(frame.index.name or ""), *map(str, frame.columns)]) + " |"
    lines = [header, "|" + "---|" * (len(frame.columns) + 1)]
    for index, *values in frame.itertuples(name=None):
        cells = [_number(v, digits) if isinstance(v, float) else str(v) for v in values]
        lines.append(f"| {index} | " + " | ".join(cells) + " |")
    return lines


def render(
    result: BacktestResult,
    summary: dict[str, float],
    *,
    title: str,
    description: str,
    params: dict[str, object],
    costs: Costs,
    period: tuple[pd.Timestamp, pd.Timestamp],
    zone: str,
    image_name: str | None,
) -> str:
    s = summary
    lines = [f"# {title}", "", description, ""]
    lines += [
        f"- Période : du {period[0]:%Y-%m-%d} au {period[1]:%Y-%m-%d} (UTC).",
        f"- Paramètres : {', '.join(f'{k} = {v}' for k, v in params.items())}.",
        f"- Coûts : {', '.join(f'{k} = {v}' for k, v in asdict(costs).items())}.",
    ]
    if result.halt_time is not None:
        lines.append(
            f"- **Le compte réel aurait été arrêté le {result.halt_time:%Y-%m-%d}** (drawdown maximal) : "
            "résultats ci-dessous en mode recherche, sans cet arrêt."
        )
    if result.halted:
        lines.append("- **Arrêt total** au drawdown maximal pendant la période (mode compte réel).")
    lines += ["", "## Résultats", ""]
    rows = [
        ("Trades", _number(s["trades"], 0), "par mois", _number(s["trades_per_month"], 1)),
        ("Résultat net", _number(s["net_profit"]), "rendement", f"{_number(s['return_pct'])} %"),
        ("Rendement annuel", f"{_number(s['cagr_pct'])} %", "drawdown max", f"{_number(s['max_drawdown_pct'])} %"),
        ("Profit factor", _number(s["profit_factor"]), "espérance", f"{_number(s['expectancy_r'], 3)} R"),
        ("Trades gagnants", f"{_number(s['win_rate_pct'], 1)} %", "gain / perte moyens",
         f"{_number(s['avg_win_r'])} R / {_number(s['avg_loss_r'])} R"),
        ("Sharpe", _number(s["sharpe"]), "Sortino", _number(s["sortino"])),
        ("Calmar", _number(s["calmar"]), "plus longue baisse", f"{_number(s['max_underwater_days'], 0)} jours"),
        ("Exposition", f"{_number(s['exposure_pct'], 1)} %", "5 meilleurs trades",
         f"{_number(s['best_five_share_pct'], 0)} % du profit"),
        ("MAE moyen", f"{_number(s['avg_mae_r'])} R", "MFE moyen", f"{_number(s['avg_mfe_r'])} R"),
    ]  # fmt: skip
    lines += ["| | | | |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in rows]
    if image_name:
        lines += ["", f"![Courbe d'equity]({image_name})"]

    monthly = monthly_returns(result)
    if not monthly.empty:
        monthly.columns = [_MONTHS[m - 1] for m in monthly.columns]
        lines += ["", "## Rendements mensuels (%)", "", *_table(monthly, 1)]
    for name, table in breakdown(result.trades, zone).items():
        lines += ["", f"## Par {name}", "", *_table(table)]
    if not result.skipped.empty:
        counts = result.skipped["reason"].value_counts()
        lines += ["", "## Signaux non exécutés", ""]
        lines += [f"- {reason} : {count}" for reason, count in counts.items()]
    return "\n".join(lines) + "\n"
