"""Entraînement explicite sur collecte démo simulée ou exports Axi ; jamais dans la boucle d'ordres."""

import argparse
import json
from pathlib import Path

import pandas as pd

from goldbot.config import ConfigError, load_settings
from goldbot.data.history import load_bars, load_ticks
from goldbot.data.market_hours import MarketSchedule
from goldbot.scalping.backtest import Instrument, run_backtest
from goldbot.scalping.learning import strategy_signature, train_model
from goldbot.scalping.store import ScalpStore


def main(argv=None, *, root=None, echo=print):
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(description="Entraîne un filtre expérimental, sans connexion ni ordre MT5.")
    parser.add_argument("--config", type=Path, default=root / "config" / "learning_demo.yaml")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--ticks", type=Path, help="dossier contenant manifest.json et les exports parquet Axi")
    source.add_argument("--store", type=Path, help="collecte SQLite (défaut : data/learning_demo.sqlite)")
    parser.add_argument("--output", type=Path, default=root / "data" / "models" / "pullback.json")
    args = parser.parse_args(argv)
    if args.output.exists():
        echo("REFUS : ce modèle existe déjà. Choisis un nouveau --output pour conserver les essais précédents.")
        return 2
    try:
        settings = load_settings(args.config)
        if args.ticks:
            folder = args.ticks
            symbol = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["symbol"]
            instrument = Instrument(*(float(symbol[k]) for k in (
                "point", "trade_contract_size", "volume_min", "volume_max", "volume_step",
                "trade_stops_level", "trade_freeze_level")))
            ticks, bars = load_ticks(folder, symbol["name"]), load_bars(folder, symbol["name"])
            if ticks.empty or bars.empty:
                raise ValueError("export de ticks / bougies vide")
            echo("Rejeu des ticks avec spread bid/ask, commissions configurées et glissement stressé...")
            result = run_backtest(ticks, bars, settings.scalping, instrument=instrument,
                schedule=MarketSchedule.from_config(settings.market_hours), initial_equity=5000,
                slippage_points=settings.scalping.extra_slippage_points,
                commission_per_lot_side=settings.backtest.commission_per_lot_side)
            # Les derniers trades forcés par la fin du fichier n'ont pas leur horizon complet.
            frame = result.trades
            if not frame.empty:
                frame = frame[frame.reason != "fin des données"]
            label = "ticks Axi, simulation stressée, compte hypothétique 5000 USD"
        else:
            path = args.store or root / "data" / f"{settings.scalping.experiment_name}.sqlite"
            if not path.is_file():
                raise ValueError("aucune collecte : lance d'abord run_learning_demo.py")
            store = ScalpStore(path)
            try:
                if store.meta("strategy_signature") != strategy_signature(settings):
                    raise ValueError("collecte incompatible avec les réglages actuels")
                frame = pd.DataFrame(store.learning_rows())
            finally:
                store.close()
            label = "signaux collectés en démo, résultats de simulation stressée (pas exécutions réelles)"
        model = train_model(frame, settings, source=label)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Mode exclusif : impossible d'écraser un modèle déjà produit par une autre instance.
        with args.output.open("x", encoding="utf-8") as out:
            json.dump(model.payload, out, ensure_ascii=False, indent=2, allow_nan=False)
        report = model.payload
        echo(f"Modèle enregistré : {args.output}")
        echo(f"Échantillons : apprentissage {report['training_samples']}, calibration {report['calibration_samples']}, "
             f"test {report['test_samples']} sur {report['test_days']} jours.")
        echo(f"Test sans filtre : {report['test_baseline']}\nTest avec filtre : {report['test_filtered']}")
        echo("Filtre admissible à un essai démo." if model.approved else
             "Filtre NON retenu : observation des scores seulement. La stratégie de base reste expérimentale.")
        echo("Un résultat passé positif ne valide pas une rentabilité future. Aucun ordre n'a été envoyé.")
        return 0
    except (ConfigError, OSError, ValueError, KeyError, ImportError) as exc:
        echo(f"Entraînement impossible : {exc}")
        return 1
