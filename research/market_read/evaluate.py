"""Évaluation de TOUS les modèles de lecture du marché, en une fois, sur la période de développement.

Chaque (modèle, réglage) est un essai journalisé (trials.csv). Deux scénarios de coûts : réel (spread de la barre,
plancher 0,15 $) et stress (+0,10 $ de glissement par côté). La validation (2024 -> 09/2025) n'est lancée qu'une
fois, sur les modèles retenus, avec --validation.
"""

from __future__ import annotations

import importlib
import multiprocessing as mp
import sys
import time
from pathlib import Path

import pandas as pd

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import harness as H  # noqa: E402

MODULES = ["baselines", "models_reaction", "models_structure", "models_momentum", "models_volume", "models_reversion"]
B = None


def load_models(only: set[str] | None = None) -> list[tuple[str, str, int, dict]]:
    items = []
    for module in MODULES:
        try:
            mod = importlib.import_module(module)
        except ModuleNotFoundError:
            print(f"module absent : {module}")
            continue
        for name, (_fn, grid) in mod.MODELS.items():
            for k, params in enumerate(grid):
                label = f"{name} #{k + 1}"
                if only is None or label in only:
                    items.append((module, name, k, params))
    return items


def run(item, period: str) -> list[dict]:
    module, name, k, params = item
    fn = importlib.import_module(module).MODELS[name][0]
    t0 = time.time()
    d = fn(B, **params)
    seconds = time.time() - t0
    rows = []
    for scenario, slip in (("reel", 0.0), ("stress", 0.10)):
        f = H.forward(B, d, period, slip=slip)
        s, trades = H.simulate(B, d, period, slip=slip)
        rows.append({"module": module, "modele": name, "reglage": k + 1, "params": params, "periode": period,
                     "scenario": scenario, "calcul_s": round(seconds, 1), **f, **s})
    return rows


def _run_dev(item):
    return run(item, "developpement")


def _run_val(item):
    return run(item, "validation")


if __name__ == "__main__":
    validation = "--validation" in sys.argv
    only = None
    if validation:
        only = set(Path(HERE / "retenus.txt").read_text(encoding="utf-8").strip().splitlines())
    B = H.bars()
    items = load_models(only)
    print(f"{len(items)} essais")
    with mp.get_context("fork").Pool(3) as pool:
        chunks = pool.map(_run_val if validation else _run_dev, items)
    rows = [r for chunk in chunks for r in chunk]
    frame = pd.DataFrame(rows)
    out = HERE / ("essais_validation.csv" if validation else "essais_developpement.csv")
    frame.to_csv(out, index=False)
    cols = ["modele", "reglage", "scenario", "signaux", "achats_pct", "brut_5m", "brut_15m", "net_5m", "trades",
            "trades_par_jour", "gagnants_pct", "pf", "esp_r", "t_jours", "esp_r_achats", "esp_r_ventes",
            "annees_positives"]
    pd.set_option("display.width", 300)
    pd.set_option("display.max_rows", 200)
    view = frame[[c for c in cols if c in frame.columns]]
    print(view[view["scenario"] == "reel"].sort_values("esp_r", ascending=False).round(3).to_string())
    print(f"écrit : {out}")
