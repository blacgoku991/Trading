"""Protocole de test des hypothèses (recherche dans la littérature, demande du 29/09/2026).

Trois périodes, fixées AVANT de regarder le moindre résultat :
- développement, du 22/07/2019 au 31/12/2023 : grille de réglages annoncée à l'avance, choix sur un plateau ;
- validation, du 01/01/2024 au 30/09/2025 : une seule fois, réglages figés ;
- final, du 01/10/2025 à la fin des données : une seule fois, pour les survivantes de la validation. Cette période a
  déjà servi une fois (test de S7, docs/STRATEGIES.md) : c'est sa deuxième utilisation, et les rapports le disent.

Capital de recherche de 100 000 $ : aucun signal n'est ignoré à cause du lot minimal, on mesure la règle et pas la
taille du compte (la contrainte du compte de 5 000 € se vérifie à part). Mêmes coûts, même moteur et mêmes limites
de risque que le backtest principal. Les intentions sont calculées une fois sur tout l'historique (stratégies
causales, test anti look-ahead obligatoire) puis rejouées période par période.

Tout essai est ajouté au journal (reports/essais_livres.csv) : le nombre de variantes compte pour juger un
résultat (test multiple, ratio de Sharpe « dégonflé » de Bailey et López de Prado).
"""

from __future__ import annotations

import csv
import itertools
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from goldbot.backtest.engine import Backtest, BacktestResult
from goldbot.backtest.metrics import summarize
from goldbot.backtest.runner import Dataset, costs_for
from goldbot.backtest.validation import monte_carlo_drawdowns
from goldbot.config import Settings
from goldbot.strategies.base import OrderIntent, StopUpdate, Strategy

RESEARCH_EQUITY = 100_000.0
EULER_GAMMA = 0.5772156649015329
_NORMAL = NormalDist()


@dataclass(frozen=True)
class Period:
    name: str
    start: pd.Timestamp
    end: pd.Timestamp | None  # exclue ; None : fin des données


PERIODS = {
    "developpement": Period(
        "developpement", pd.Timestamp("2019-07-22", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC")
    ),
    "validation": Period("validation", pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-10-01", tz="UTC")),
    "final": Period("final", pd.Timestamp("2025-10-01", tz="UTC"), None),
}

# Coûts : réels (spread de la barre, plancher et glissement de la config) et stress (spread x 1,5, glissement 10 pts).
SCENARIOS: dict[str, tuple[float | None, float | None]] = {"reel": (None, None), "stress": (1.5, 10.0)}


def in_period(times: pd.Series | pd.DatetimeIndex, period: Period) -> np.ndarray:
    keep = np.array(times >= period.start, dtype=bool)
    if period.end is not None:
        keep &= np.array(times < period.end, dtype=bool)
    return keep


def _within(moment: pd.Timestamp, period: Period) -> bool:
    return moment >= period.start and (period.end is None or moment < period.end)


def run_intents(
    intents: Sequence[OrderIntent],
    dataset: Dataset,
    period: Period,
    *,
    settings: Settings,
    scenario: str = "reel",
    equity: float = RESEARCH_EQUITY,
    updates: Sequence[StopUpdate] = (),
) -> BacktestResult:
    """Backtest des intentions (et mises à jour du stop) nées dans la période, sur les barres de la période."""
    bars = dataset.bars[in_period(dataset.bars["time"], period)].reset_index(drop=True)
    kept = [intent for intent in intents if _within(intent.time, period)]
    changes = [update for update in updates if _within(update.time, period)]
    spread_x, slippage = SCENARIOS[scenario]
    costs = costs_for(settings.backtest, dataset.symbol, spread_multiplier=spread_x, slippage_points=slippage)
    backtest = Backtest(
        bars,
        instrument=dataset.instrument,
        costs=costs,
        risk=settings.risk,
        initial_equity=equity,
        halt_on_drawdown=False,
    )
    return backtest.run(kept, changes)


# --- statistiques -----------------------------------------------------------------------------------


def r_stats(r: np.ndarray) -> dict[str, float]:
    """Statistiques par trade, en R : moyenne, t de Student, probabilité que le Sharpe réel soit > 0 (PSR)."""
    r = np.asarray(r, dtype="float64")
    n = len(r)
    if n < 2:
        return {"n": n, "mean_r": float(r.mean()) if n else 0.0, "std_r": 0.0, "t_stat": 0.0, "sharpe_trade": 0.0,
                "psr": 0.5, "skew": 0.0, "kurtosis": 3.0, "pf_r": 0.0}  # fmt: skip
    mean, std = float(r.mean()), float(r.std(ddof=1))
    sharpe = mean / std if std > 0 else 0.0
    centered = r - mean
    m2 = float((centered**2).mean())
    skew = float((centered**3).mean() / m2**1.5) if m2 > 0 else 0.0
    kurt = float((centered**4).mean() / m2**2) if m2 > 0 else 3.0
    gains, losses = float(r[r > 0].sum()), float(-r[r < 0].sum())
    return {
        "n": n,
        "mean_r": mean,
        "std_r": std,
        "t_stat": sharpe * math.sqrt(n),
        "sharpe_trade": sharpe,
        "psr": probabilistic_sharpe(sharpe, n, skew, kurt, 0.0),
        "skew": skew,
        "kurtosis": kurt,
        "pf_r": gains / losses if losses > 0 else math.inf,
    }


def probabilistic_sharpe(sharpe: float, n: int, skew: float, kurt: float, benchmark: float) -> float:
    """Probabilité que le vrai Sharpe dépasse benchmark (Bailey et López de Prado, 2012), Sharpe par observation."""
    if n < 2:
        return 0.5
    variance = 1.0 - skew * sharpe + (kurt - 1.0) / 4.0 * sharpe**2
    if variance <= 0:
        return 0.5
    return _NORMAL.cdf((sharpe - benchmark) * math.sqrt(n - 1) / math.sqrt(variance))


def expected_max_sharpe(trial_sharpes: Sequence[float]) -> float:
    """Meilleur Sharpe attendu par hasard parmi N essais sans avantage (Bailey et López de Prado, 2014)."""
    values = np.asarray([s for s in trial_sharpes if np.isfinite(s)], dtype="float64")
    count = len(values)
    if count < 2:
        return 0.0
    spread = float(values.std(ddof=1))
    first = _NORMAL.inv_cdf(1.0 - 1.0 / count)
    second = _NORMAL.inv_cdf(1.0 - 1.0 / (count * math.e))
    return spread * ((1.0 - EULER_GAMMA) * first + EULER_GAMMA * second)


def deflated_sharpe(r: np.ndarray, trial_sharpes: Sequence[float]) -> float:
    """Ratio de Sharpe « dégonflé » : probabilité que le Sharpe dépasse le meilleur attendu par hasard sur N essais."""
    stats = r_stats(r)
    benchmark = expected_max_sharpe(trial_sharpes)
    return probabilistic_sharpe(stats["sharpe_trade"], int(stats["n"]), stats["skew"], stats["kurtosis"], benchmark)


def yearly(trades: pd.DataFrame) -> pd.DataFrame:
    """Trades, R moyen et R total par année (heure de sortie, UTC)."""
    if trades.empty:
        return pd.DataFrame(columns=["trades", "R moyen", "R total"])
    grouped = trades.groupby(trades["exit_time"].dt.year)["r_multiple"]
    table = pd.DataFrame({"trades": grouped.size(), "R moyen": grouped.mean(), "R total": grouped.sum()})
    table.index.name = "année"
    return table


def best_share_r(r: np.ndarray, count: int = 5) -> float:
    """Part (%) du total en R apportée par les `count` meilleurs trades (critère : < 30 %)."""
    r = np.asarray(r, dtype="float64")
    total = float(r.sum())
    return float(np.sort(r)[::-1][:count].sum()) / total * 100.0 if total > 0 else math.nan


# --- évaluation et journal -----------------------------------------------------------------------------


def evaluate_intents(
    intents: Sequence[OrderIntent],
    dataset: Dataset,
    settings: Settings,
    *,
    periods: Iterable[str] = ("developpement",),
    scenarios: Iterable[str] = ("reel",),
    equity: float = RESEARCH_EQUITY,
    updates: Sequence[StopUpdate] = (),
) -> list[dict[str, object]]:
    """Une ligne de résultats par (période, scénario de coûts)."""
    rows = []
    for name in periods:
        period = PERIODS[name]
        for scenario in scenarios:
            result = run_intents(intents, dataset, period, settings=settings, scenario=scenario, equity=equity,
                                 updates=updates)  # fmt: skip
            rows.append(result_row(result, period=name, scenario=scenario))
    return rows


def result_row(result: BacktestResult, *, period: str, scenario: str) -> dict[str, object]:
    trades = result.trades
    r = trades["r_multiple"].to_numpy() if len(trades) else np.array([])
    summary = summarize(result) if len(result.equity) else {}
    stats = r_stats(r)
    years = yearly(trades)
    return {
        "periode": period,
        "scenario": scenario,
        "trades": int(stats["n"]),
        "pf": round(float(summary.get("profit_factor", 0.0)), 3),
        "exp_r": round(stats["mean_r"], 4),
        "t_stat": round(stats["t_stat"], 2),
        "psr": round(stats["psr"], 3),
        "sharpe_trade": round(stats["sharpe_trade"], 4),
        "win_pct": round(float(summary.get("win_rate_pct", 0.0)), 1),
        "ret_pct": round(float(summary.get("return_pct", 0.0)), 2),
        "dd_pct": round(float(summary.get("max_drawdown_pct", 0.0)), 2),
        "annees_positives": f"{int((years['R total'] > 0).sum())}/{len(years)}",
        "meilleurs5_pct": round(best_share_r(r), 1),
        "ignores": len(result.skipped),
        "par_annee": " ".join(f"{year}:{value:+.2f}" for year, value in years["R moyen"].items()),
    }


def evaluate(
    strategy: Strategy,
    dataset: Dataset,
    settings: Settings,
    **kwargs: object,
) -> list[dict[str, object]]:
    """Intentions calculées une fois sur tout l'historique, puis backtest par période et par scénario."""
    intents, updates = strategy.intents(dataset.bars), strategy.stop_updates(dataset.bars)
    return evaluate_intents(intents, dataset, settings, updates=updates, **kwargs)  # type: ignore[arg-type]


def log_trials(path: Path, hypothesis: str, params: Mapping[str, object], rows: Iterable[Mapping[str, object]],
               *, stamp: str) -> None:  # fmt: skip
    """Ajoute les essais au journal (CLAUDE.md §10 : conscience du test multiple). Rien n'est jamais effacé."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["date", "hypothese", "parametres", *RESULT_FIELDS]
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="ignore")
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow({"date": stamp, "hypothese": hypothesis, "parametres": json.dumps(params, default=str),
                             **row})  # fmt: skip


RESULT_FIELDS = [
    "periode", "scenario", "trades", "pf", "exp_r", "t_stat", "psr", "sharpe_trade", "win_pct", "ret_pct", "dd_pct",
    "annees_positives", "meilleurs5_pct", "ignores", "par_annee",
]  # fmt: skip


# --- grilles et plateaux ------------------------------------------------------------------------------------


def grid(axes: Mapping[str, Sequence[object]]) -> list[dict[str, object]]:
    """Toutes les combinaisons des valeurs annoncées pour chaque paramètre (ordre des axes conservé)."""
    names = list(axes)
    return [dict(zip(names, values, strict=True)) for values in itertools.product(*(axes[n] for n in names))]


def plateau(results: pd.DataFrame, axes: Mapping[str, Sequence[object]], metric: str = "exp_r",
            *, min_trades: int = 0) -> pd.DataFrame:  # fmt: skip
    """Score de voisinage : moyenne du critère sur la combinaison et ses voisines (un cran sur chaque axe).

    On choisit la combinaison au meilleur voisinage, pas le pic isolé (CLAUDE.md §10). Une voisine avec trop peu de
    trades compte pour 0 (on ne récompense pas les zones vides). Colonnes ajoutées : voisinage, voisines.
    """
    names = list(axes)
    position = {name: {value: k for k, value in enumerate(axes[name])} for name in names}
    index = {tuple(position[n][row[n]] for n in names): k for k, row in results.iterrows()}
    scores, counts = [], []
    for _, row in results.iterrows():
        center = tuple(position[n][row[n]] for n in names)
        values = []
        for offsets in itertools.product((-1, 0, 1), repeat=len(names)):
            if sum(abs(o) for o in offsets) > 1:
                continue  # voisines directes seulement (un axe à la fois)
            key = tuple(c + o for c, o in zip(center, offsets, strict=True))
            if key in index:
                other = results.loc[index[key]]
                values.append(float(other[metric]) if other["trades"] >= min_trades else 0.0)
        scores.append(float(np.mean(values)))
        counts.append(len(values))
    return results.assign(voisinage=scores, voisines=counts)


def monte_carlo_row(r: np.ndarray, *, risk_pct: float = 0.5, runs: int = 1000) -> dict[str, float]:
    """Drawdown médian, 95e centile et pire (%, négatifs) : ordre mélangé, 10 % des trades sautés."""
    if len(r) < 2:
        return {"mc_dd_median": 0.0, "mc_dd_95": 0.0, "mc_dd_pire": 0.0}
    drawdowns = monte_carlo_drawdowns(np.asarray(r), risk_pct=risk_pct, runs=runs)
    return {
        "mc_dd_median": round(float(np.median(drawdowns)), 2),
        "mc_dd_95": round(float(np.percentile(drawdowns, 5)), 2),
        "mc_dd_pire": round(float(drawdowns.min()), 2),
    }
