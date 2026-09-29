# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Idée de l'utilisateur (29/09, nuit) : bougie dans un sens, puis une bougie contraire, puis entrée dans le sens de
la première à l'ouverture de la bougie suivante. Stop au-delà des deux bougies (au-dessus pour une vente), objectif
fixe de 2 ou 3 $ (« 4000 > 4003 »). Mesuré trade par trade (positions indépendantes, plusieurs à la fois permis).

Prix des barres = BID. Vente : entrée au bid d'ouverture, stop touché quand l'ask (bid + spread) atteint le stop,
objectif quand l'ask descend à l'objectif. Achat : entrée à l'ask, stop et objectif lus au bid. Stop et objectif dans
la même minute : stop d'abord. Sortie au plus tard après `hold` minutes, et jamais à travers la coupure du jour.
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import harness as H  # noqa: E402


def candles(b: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Bougies de `minutes` minutes (heure serveur), rendues à la clôture de leur dernière minute : index = barre M1
    qui les clôt (la décision se prend là, l'entrée à l'ouverture de la barre M1 suivante)."""
    if minutes == 1:
        return pd.DataFrame({"i": np.arange(len(b)), "o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"]})
    t = b["time_server"].to_numpy()
    bucket = t // (60 * minutes)
    g = pd.DataFrame({"bucket": bucket, "i": np.arange(len(b)), "o": b["open"].to_numpy(), "h": b["high"].to_numpy(),
                      "l": b["low"].to_numpy(), "c": b["close"].to_numpy(), "m": (t // 60) % minutes})
    agg = g.groupby("bucket").agg(i=("i", "last"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"),
                                  m=("m", "last"))
    return agg[agg["m"] == minutes - 1].reset_index(drop=True)  # bougies complètes seulement


def run(b: pd.DataFrame, period: str, *, minutes: int = 1, target: float = 3.0, hold: int = 30,
        min_stop: float = 0.0, max_stop: float = 6.0, slip: float = 0.0) -> dict[str, float]:
    k = candles(b, minutes)
    o, h, l, c = (k[x].to_numpy() for x in ("o", "h", "l", "c"))
    bear, bull = c < o, c > o
    sell = np.zeros(len(k), bool)
    buy = np.zeros(len(k), bool)
    sell[1:] = bear[:-1] & bull[1:]
    buy[1:] = bull[:-1] & bear[1:]
    hi2 = np.maximum(h, np.concatenate([[np.nan], h[:-1]]))
    lo2 = np.minimum(l, np.concatenate([[np.nan], l[:-1]]))
    idx = k["i"].to_numpy()

    O, Hb, Lb, C = (b[x].to_numpy(dtype="float64") for x in ("open", "high", "low", "close"))
    spread = H.spread_price(b)
    days = b["time_server"].to_numpy() // 86_400
    inside = H._in(b, period)
    n = len(b)
    rows = []
    for side, mask, level in ((-1, sell, hi2), (1, buy, lo2)):
        for q in np.nonzero(mask)[0]:
            i = idx[q]
            j = i + 1
            if j >= n or not inside[i] or days[j] != days[i]:
                continue
            if side < 0:
                entry = O[j] - slip
                sl = level[q] + spread[j] + 0.05  # au-dessus de la bougie, lu à l'ask
                dist = sl - entry
            else:
                entry = O[j] + spread[j] + slip
                sl = level[q] - 0.05  # sous la bougie, lu au bid
                dist = entry - sl
            if dist < min_stop:
                dist = min_stop
                sl = entry - side * dist
            if dist <= spread[j] or dist > max_stop:
                continue
            tp = entry + side * target
            last = min(j + hold - 1, n - 1)
            exit_price, reason = None, "durée max"
            m = j
            for m in range(j, last + 1):
                if days[m] != days[i]:
                    m -= 1
                    reason = "fin de jour"
                    break
                if side > 0:
                    if Lb[m] <= sl:
                        exit_price, reason = (min(sl, O[m]) if m > j else sl) - slip, "stop"
                        break
                    if Hb[m] >= tp:
                        exit_price, reason = tp, "objectif"
                        break
                else:
                    if Hb[m] + spread[m] >= sl:
                        exit_price, reason = (max(sl, O[m] + spread[m]) if m > j else sl) + slip, "stop"
                        break
                    if Lb[m] + spread[m] <= tp:
                        exit_price, reason = tp, "objectif"
                        break
            if exit_price is None:
                exit_price = C[m] - slip if side > 0 else C[m] + spread[m] + slip
            move = (exit_price - entry) * side
            rows.append((i, side, dist, move, move / dist, reason, days[i]))
    t = pd.DataFrame(rows, columns=["bar", "side", "dist", "move", "r", "reason", "day"])
    wins, losses = t[t["move"] > 0], t[t["move"] <= 0]
    years = pd.to_datetime(b["time"].to_numpy()[t["bar"].to_numpy()]).year
    by_year = t.groupby(years)["move"].sum()
    return {
        "trades": len(t),
        "par_jour": round(len(t) / max(t["day"].nunique(), 1), 1),
        "gagnants_pct": round(len(wins) / len(t) * 100, 1),
        "stop_moyen_pips": round(t["dist"].mean() * 10, 1),
        "gain_moy_pips": round(wins["move"].mean() * 10, 1),
        "perte_moy_pips": round(losses["move"].mean() * 10, 1),
        "net_par_trade_pips": round(t["move"].mean() * 10, 2),
        "pf": round(wins["move"].sum() / -losses["move"].sum(), 3),
        "esp_r": round(t["r"].mean(), 3),
        "t_jours": round(H._day_t(t["move"].to_numpy(), t["day"].to_numpy()), 1),
        "annees+": f"{int((by_year > 0).sum())}/{len(by_year)}",
        "objectif_pct": round((t["reason"] == "objectif").mean() * 100, 1),
    }


VARIANTS = {
    "M1, objectif 3 $ (idée telle quelle)": dict(minutes=1, target=3.0),
    "M1, objectif 2 $": dict(minutes=1, target=2.0),
    "M1, objectif 3 $, stop au moins 1 $": dict(minutes=1, target=3.0, min_stop=1.0),
    "M5, objectif 3 $": dict(minutes=5, target=3.0, hold=60),
    "M5, objectif 2 $": dict(minutes=5, target=2.0, hold=60),
}

if __name__ == "__main__":
    import multiprocessing as mp

    B = H.bars()

    def one(item):
        name, kw = item
        out = []
        for slip in (0.0, 0.10):
            out.append({"variante": name, "glissement_usd": slip, **run(B, "developpement", slip=slip, **kw)})
        return out

    with mp.get_context("fork").Pool(2) as pool:
        rows = [r for chunk in pool.map(one, list(VARIANTS.items())) for r in chunk]
    pd.set_option("display.width", 300)
    print(pd.DataFrame(rows).to_string())
