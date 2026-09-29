# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Variantes de l'idée « deux bougies » (29/09, nuit, 2e demande de l'utilisateur : « suivre la série, changer quand
ça se retourne »). Même mesure que idea_user.py (bid, spread de la barre, stop avant objectif, 10 minutes, stop au
moins 10 pips, objectif 40 pips, une position), seules les règles d'entrée changent :

- reprise (règle actuelle) : baissière puis haussière -> vente (on parie que la haussière n'est qu'un rebond) ;
- retournement : baissière puis haussière -> achat (on suit la bougie qui change de couleur) ;
- série : deux bougies de même couleur -> entrée dans leur sens ;
- suivre : à chaque bougie non plate, entrée dans son sens (retournement + série).
Stop au-delà des deux dernières bougies (sous leur plus bas pour un achat).
"""

from __future__ import annotations

import multiprocessing as mp
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import harness as H  # noqa: E402


def entries(b: pd.DataFrame, mode: str) -> np.ndarray:
    o, c = b["open"].to_numpy(), b["close"].to_numpy()
    col = np.sign(c - o).astype(int)  # +1 haussière, -1 baissière, 0 plate
    prev = np.concatenate([[0], col[:-1]])
    side = np.zeros(len(b), dtype=int)
    change = (prev != 0) & (col != 0) & (prev != col)
    if mode == "reprise":
        side[change] = prev[change]
    elif mode == "retournement":
        side[change] = col[change]
    elif mode == "serie":
        same = (col != 0) & (prev == col)
        side[same] = col[same]
    elif mode == "suivre":
        side = col.copy()
    return side


def run(b: pd.DataFrame, period: str, mode: str, *, target: float = 4.0, hold: int = 10, min_stop: float = 1.0,
        max_stop: float = 6.0, slip: float = 0.0, tp_candle: bool = False, min_target: float = 1.0) -> dict[str, float]:
    """tp_candle : objectif au-delà des deux dernières bougies (leur plus haut pour un achat, leur plus bas pour une
    vente), à distance bornée à [min_target, target] ; sinon objectif fixe à `target`."""
    side_at = entries(b, mode)
    O, Hb, Lb, C = (b[x].to_numpy(dtype="float64") for x in ("open", "high", "low", "close"))
    hi2 = np.maximum(Hb, np.concatenate([[np.nan], Hb[:-1]]))
    lo2 = np.minimum(Lb, np.concatenate([[np.nan], Lb[:-1]]))
    spread = H.spread_price(b)
    days = b["time_server"].to_numpy() // 86_400
    inside = H._in(b, period)
    n = len(b)
    rows = []
    for i in np.nonzero(side_at)[0]:
        side, j = side_at[i], i + 1
        if j >= n or not inside[i] or days[j] != days[i] or days[i - 1] != days[i]:
            continue
        if side < 0:
            entry = O[j] - slip
            sl = hi2[i] + spread[j] + 0.05
            dist = sl - entry
        else:
            entry = O[j] + spread[j] + slip
            sl = lo2[i] - 0.05
            dist = entry - sl
        if dist < min_stop:
            dist, sl = min_stop, entry - side * min_stop
        if dist > max_stop:
            continue
        if tp_candle:
            level = hi2[i] if side > 0 else lo2[i] + spread[j]  # achat : bid au-dessus du plus haut ; vente : ask
            reach = min(max((level - entry) * side, min_target), target)
        else:
            reach = target
        tp = entry + side * reach
        last = min(j + hold - 1, n - 1)
        exit_price, m = None, j
        for m in range(j, last + 1):
            if days[m] != days[i]:
                m -= 1
                break
            if side > 0:
                if Lb[m] <= sl:
                    exit_price = (min(sl, O[m]) if m > j else sl) - slip
                    break
                if Hb[m] >= tp:
                    exit_price = tp
                    break
            else:
                if Hb[m] + spread[m] >= sl:
                    exit_price = (max(sl, O[m] + spread[m]) if m > j else sl) + slip
                    break
                if Lb[m] + spread[m] <= tp:
                    exit_price = tp
                    break
        if exit_price is None:
            exit_price = C[m] - slip if side > 0 else C[m] + spread[m] + slip
        move = (exit_price - entry) * side
        rows.append((i, move, move / dist, days[i]))
    t = pd.DataFrame(rows, columns=["bar", "move", "r", "day"])
    wins, losses = t[t["move"] > 0], t[t["move"] <= 0]
    years = pd.to_datetime(b["time"].to_numpy()[t["bar"].to_numpy()]).year
    by_year = t.groupby(years)["move"].sum()
    return {"trades": len(t), "par_jour": round(len(t) / t["day"].nunique(), 0),
            "gagnants_pct": round(len(wins) / len(t) * 100, 1), "net_par_trade_pips": round(t["move"].mean() * 10, 2),
            "pf": round(wins["move"].sum() / -losses["move"].sum(), 3), "esp_r": round(t["r"].mean(), 3),
            "t_jours": round(H._day_t(t["move"].to_numpy(), t["day"].to_numpy()), 1),
            "annees+": f"{int((by_year > 0).sum())}/{len(by_year)}"}


if __name__ == "__main__":
    B = H.bars()

    VARIANTS = {
        "suivre, objectif 40 pips": dict(mode="suivre"),
        "suivre, objectif au-delà des 2 bougies (10-40 pips)": dict(mode="suivre", tp_candle=True),
        "suivre, objectif au-delà des 2 bougies (15-40 pips)": dict(mode="suivre", tp_candle=True, min_target=1.5),
        "reprise, objectif au-delà des 2 bougies (10-40 pips)": dict(mode="reprise", tp_candle=True),
    }

    def one(item):
        name, kw = item
        return [{"regle": name, "periode": p, **run(B, p, **kw)} for p in ("developpement", "final")]

    with mp.get_context("fork").Pool(2) as pool:
        rows = [r for c in pool.map(one, list(VARIANTS.items())) for r in c]
    pd.set_option("display.width", 250)
    print(pd.DataFrame(rows).to_string())
