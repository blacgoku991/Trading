"""Backtest d'une stratégie sur l'historique exporté (logique de scripts/run_backtest.py). Tourne partout."""

from __future__ import annotations

import argparse
import csv
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from goldbot.backtest.metrics import summarize
from goldbot.backtest.report import equity_png, render
from goldbot.backtest.runner import costs_for, load_dataset, run
from goldbot.config import ConfigError, Settings, load_settings
from goldbot.strategies.asian_breakout import AsianBreakout, AsianBreakoutParams
from goldbot.strategies.base import Strategy

EXIT_OK = 0
EXIT_NO_DATA = 2
EXIT_CONFIG = 4


def _asian_breakout(settings: Settings) -> tuple[Strategy, dict[str, object]]:
    config = settings.strategies.asian_breakout
    params = AsianBreakoutParams(**config.model_dump())
    return AsianBreakout(params), config.model_dump()


STRATEGIES: dict[str, Callable[[Settings], tuple[Strategy, dict[str, object]]]] = {"s1": _asian_breakout}


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _log_trial(path: Path, row: dict[str, object]) -> None:
    """Journal de TOUS les essais (CLAUDE.md §10 : conscience du multiple testing)."""
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row), delimiter=";")
        if new:
            writer.writeheader()
        writer.writerow(row)


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    now_utc: Callable[[], datetime] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="run_backtest.py", description="Backtest d'une stratégie sur l'or.")
    parser.add_argument("--strategie", choices=sorted(STRATEGIES), default="s1")
    parser.add_argument("--debut", type=date.fromisoformat, help="première date (UTC) ; défaut : début des données")
    parser.add_argument("--fin", type=date.fromisoformat, help="date de fin exclue (UTC) ; défaut : hors échantillon")
    parser.add_argument(
        "--hors-echantillon",
        action="store_true",
        help="teste la période hors échantillon (validation finale : à n'utiliser qu'une fois)",
    )
    parser.add_argument("--compte-reel", action="store_true", help="arrêt total au drawdown maximal, comme en live")
    parser.add_argument("--spread-x", type=float, help="multiplicateur de spread (stress : 1.5)")
    parser.add_argument("--glissement", type=float, help="glissement en points par exécution")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--dir", type=Path, help="dossier des données (défaut : export.directory)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG

    oos = settings.backtest.out_of_sample_start
    if args.hors_echantillon:
        start, end = max(args.debut or oos, oos), args.fin
        echo("ATTENTION : période hors échantillon. Elle ne doit servir qu'une fois, pour la validation finale.")
    else:
        start, end = args.debut, min(args.fin or oos, oos)
    folder = _resolve(root, args.dir or settings.export.directory)
    try:
        dataset = load_dataset(folder, start=start, end=end)
    except (FileNotFoundError, ValueError) as exc:
        echo(f"ERREUR : données introuvables ou inutilisables dans {folder} : {exc}")
        return EXIT_NO_DATA
    if dataset.bars.empty:
        echo("ERREUR : aucune barre sur la période demandée")
        return EXIT_NO_DATA

    strategy, params = STRATEGIES[args.strategie](settings)
    costs = costs_for(
        settings.backtest, dataset.symbol, spread_multiplier=args.spread_x, slippage_points=args.glissement
    )
    first, last = dataset.bars["time"].iloc[0], dataset.bars["time"].iloc[-1]
    echo(f"{strategy.name} : {len(dataset.bars)} barres du {first:%Y-%m-%d} au {last:%Y-%m-%d}...")
    result = run(
        strategy,
        dataset,
        costs=costs,
        risk=settings.risk,
        initial_equity=settings.backtest.initial_equity,
        halt_on_drawdown=args.compte_reel,
    )
    summary = summarize(result)

    stamp = (now_utc or (lambda: datetime.now(UTC)))().strftime("%Y%m%dT%H%M%SZ")
    reports = _resolve(root, settings.paths.reports)
    name = f"backtest_{args.strategie}_{stamp}"
    image = reports / f"{name}.png"
    if len(result.daily_equity) > 1:
        equity_png(result, image, f"{strategy.name} - equity")
    text = render(
        result,
        summary,
        title=f"Backtest {strategy.name}",
        description=strategy.description,
        params=params,
        costs=costs,
        period=(first, last + timedelta(minutes=1)),
        zone=settings.bot.display_timezone,
        image_name=image.name if image.exists() else None,
    )
    report = reports / f"{name}.md"
    report.write_text(text, encoding="utf-8")
    _log_trial(
        reports / "essais.csv",
        {
            "date": stamp,
            "strategie": args.strategie,
            "debut": f"{first:%Y-%m-%d}",
            "fin": f"{last:%Y-%m-%d}",
            "hors_echantillon": args.hors_echantillon,
            "parametres": params,
            "spread_x": costs.spread_multiplier,
            "glissement": costs.slippage_points,
            **{key: round(value, 4) for key, value in summary.items()},
        },
    )
    echo(
        f"{summary['trades']} trades, profit factor {summary['profit_factor']:.2f}, espérance "
        f"{summary['expectancy_r']:+.3f} R, rendement {summary['return_pct']:+.1f} %, drawdown max "
        f"{summary['max_drawdown_pct']:.1f} %"
    )
    if result.halt_time is not None:
        echo(f"Le compte réel aurait été arrêté le {result.halt_time:%Y-%m-%d} (drawdown maximal).")
    echo(f"Rapport : {report}")
    return EXIT_OK
