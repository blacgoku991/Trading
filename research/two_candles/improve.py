# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Améliorer « deux bougies v1 » dans son principe (demande de l'utilisateur, 29/09/2026, nuit) : entrée de reprise
(baissière puis haussière -> vente, l'inverse -> achat), stop au-delà des deux bougies, objectif fixe, 10 minutes,
achats et ventes pouvant être ouverts en même temps (chaque signal est un trade indépendant).

Pistes FIXÉES AVANT toute mesure (une seule fois chacune ; le choix se fait sur la période de développement) :

  filtres d'entrée                                        sorties
  - ecart   : spread <= 1,5 x médiane des 1 440 barres    - duree20 : 20 minutes au lieu de 10
  - seances : heure serveur 10:00-23:00 (Londres + NY)    - protege : stop à l'entrée dès +1 R
  - volat   : ATR M1 >= médiane des 1 440 barres          - suiveur : stop à l'entrée dès +1 R, puis à 1 R du
  - jour    : dans le sens du mouvement du jour si              meilleur prix
              >= 0,5 ATR journalier (idée S7)              - mi_temps : sortie à 5 minutes si le trade gagne
  - ema     : dans le sens de l'EMA20 / EMA50 M1
  - elan    : 1re bougie >= 0,5 ATR M1, 2e plus petite

Puis une seule combinaison, annoncée d'avance : le meilleur filtre + la meilleure sortie du développement.

Deux géométries :
  - atr  : stop au-delà des bougies, au moins 0,5 ATR M1, au plus 1 ATR M1 (sinon refus, comme le lot 0,3 / 1 %),
           objectif 2 ATR M1 : les distances de la démo au niveau de volatilité de 2026 (10 / 20 / 40 pips pour une
           ATR M1 de 20 pips), comparables d'une année à l'autre (l'ATR M1 médiane passe de 0,31 $ en 2019 à 2,16 $
           en 2026) ;
  - pips : la démo telle quelle (stop au moins 10 pips, au plus 20, objectif 40), mesurée seulement depuis 2025
           (avant, 40 pips en 10 minutes est hors de portée : jusqu'à 13 ATR M1).
Choix sur le développement (géométrie atr, 2019-07 -> 2023) : plus forte espérance par trade (R) avec au moins
5 000 trades. Validation (2024 -> 09/2025) puis final (10/2025 ->) une seule fois pour les retenues.
Mesure : barres M1 Axi (bid, spread de la barre avec plancher 0,15 $), entrée à l'ouverture suivante (ask à l'achat),
stop avant objectif dans la même minute, stop franchi en gap à l'ouverture, pas de trade à travers la coupure du jour,
stop déplacé (protection, suiveur) à partir de la minute suivante seulement (pas d'ordre connu dans la minute).

    python research/two_candles/improve.py --periode developpement
    python research/two_candles/improve.py --periode validation --variantes base,jour
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "market_read"))
import harness as H  # noqa: E402

H.PERIODS["recent"] = ("2025-01-01", "2100-01-01")  # géométrie pips : depuis 2025 seulement

LOG = HERE / "essais.csv"
BUFFER = 0.05  # 5 points au-delà des bougies (stop_buffer_points)
FILTERS = ("ecart", "seances", "volat", "jour", "ema", "elan")
EXITS = {
    "duree20": dict(hold=20),
    "protege": dict(be_r=1.0),
    "suiveur": dict(be_r=1.0, trail_r=1.0),
    "mi_temps": dict(mid_exit=5),
}
GEOMETRY = {"atr": dict(min_stop=0.5, max_stop=1.0, target=2.0), "pips": dict(min_stop=1.0, max_stop=2.0, target=4.0)}


def context(b: pd.DataFrame) -> dict[str, np.ndarray]:
    """Signal de reprise et contextes causaux (valeurs connues à la clôture de la barre)."""
    o, h, l, c = (b[x].to_numpy(dtype="float64") for x in ("open", "high", "low", "close"))
    col = np.sign(c - o).astype(int)
    prev = np.concatenate([[0], col[:-1]])
    side = np.where((prev != 0) & (col != 0) & (prev != col), prev, 0)
    atr = H.atr_m1(b, 14)
    spread = H.spread_price(b)
    move = c - H.day_open(b)
    day_dir = np.where(np.abs(move) >= 0.5 * H.daily_atr(b, 14), np.sign(move), 0)  # NaN (début) : 0
    close = pd.Series(c)
    ema = np.sign(close.ewm(span=20, adjust=False).mean() - close.ewm(span=50, adjust=False).mean()).to_numpy()
    body, prev_body = np.abs(c - o), np.abs(np.concatenate([[np.nan], (c - o)[:-1]]))
    return {
        "side": side,
        "atr": atr,
        "ecart": spread <= 1.5 * pd.Series(spread).rolling(1440, min_periods=1440).median().shift(1).to_numpy(),
        "seances": ((b["time_server"].to_numpy() // 3600) % 24 >= 10) & ((b["time_server"].to_numpy() // 3600) % 24 < 23),
        "volat": atr >= pd.Series(atr).rolling(1440, min_periods=1440).median().shift(1).to_numpy(),
        "jour": side == day_dir,
        "ema": side == ema,
        "elan": (prev_body >= 0.5 * atr) & (body <= prev_body),
    }


def simulate(b: pd.DataFrame, ctx: dict[str, np.ndarray], period: str, *, geometry: str = "atr",
             filters: tuple[str, ...] = (), hold: int = 10, be_r: float | None = None, trail_r: float | None = None,
             mid_exit: int | None = None, slip: float = 0.0) -> pd.DataFrame:  # fmt: skip
    """Un trade par signal (achats et ventes indépendants). Colonnes : bar, side, move ($/oz), r, reason, day."""
    O, Hh, L, C = (b[x].to_numpy(dtype="float64") for x in ("open", "high", "low", "close"))
    sp = H.spread_price(b)
    days = b["time_server"].to_numpy() // 86_400
    n = len(b)
    mask = (ctx["side"] != 0) & H._in(b, period) & ~np.isnan(ctx["atr"])
    for name in filters:
        mask &= ctx[name]
    idx = np.nonzero(mask)[0]
    idx = idx[(idx >= 1) & (idx + 1 < n)]
    idx = idx[(days[idx + 1] == days[idx]) & (days[idx - 1] == days[idx])]
    side = ctx["side"][idx].astype("float64")
    j = idx + 1
    hi2, lo2 = np.maximum(Hh[idx], Hh[idx - 1]), np.minimum(L[idx], L[idx - 1])
    entry = np.where(side > 0, O[j] + sp[j] + slip, O[j] - slip)
    sl = np.where(side < 0, hi2 + sp[j] + BUFFER, lo2 - BUFFER)
    dist = (entry - sl) * side
    g = GEOMETRY[geometry]
    unit = ctx["atr"][idx] if geometry == "atr" else np.ones(len(idx))
    min_stop, max_stop, target = g["min_stop"] * unit, g["max_stop"] * unit, g["target"] * unit
    widen = dist < min_stop
    dist = np.where(widen, min_stop, dist)
    sl = np.where(widen, entry - side * min_stop, sl)
    keep = dist <= max_stop + 1e-9
    idx, side, j, entry, sl, dist, target = idx[keep], side[keep], j[keep], entry[keep], sl[keep], dist[keep], target[keep]
    tp = entry + side * target
    m_count = len(idx)
    exit_price = np.full(m_count, np.nan)
    reason = np.full(m_count, "", dtype=object)
    alive = np.ones(m_count, dtype=bool)
    best = np.zeros(m_count)
    stop = sl.copy()
    long_ = side > 0
    for k in range(hold):
        m = np.minimum(j + k, n - 1)
        live = alive & (j + k < n)
        new_day = live & (days[m] != days[idx])
        p = m - 1
        at_close = np.where(long_, C[p] - slip, C[p] + sp[p] + slip)
        exit_price[new_day], reason[new_day], alive[new_day] = at_close[new_day], "fin de jour", False
        live &= ~new_day
        ask_open, ask_high, ask_low = O[m] + sp[m], Hh[m] + sp[m], L[m] + sp[m]
        hit = np.where(long_, L[m] <= stop, ask_high >= stop) & live
        fill = np.where(long_, np.minimum(stop, O[m]) - slip, np.maximum(stop, ask_open) + slip)
        fill = np.where(k == 0, stop - side * slip, fill)
        exit_price[hit], reason[hit], alive[hit] = fill[hit], "stop", False
        live &= ~hit
        won = np.where(long_, Hh[m] >= tp, ask_low <= tp) & live
        exit_price[won], reason[won], alive[won] = tp[won], "objectif", False
        live &= ~won
        close_px = np.where(long_, C[m] - slip, C[m] + sp[m] + slip)
        if mid_exit is not None and k == mid_exit - 1:
            gain = live & ((close_px - entry) * side > 0)
            exit_price[gain], reason[gain], alive[gain] = close_px[gain], "mi-temps", False
            live &= ~gain
        if k == hold - 1:
            exit_price[live], reason[live], alive[live] = close_px[live], "durée max", False
            break
        best = np.maximum(best, np.where(long_, Hh[m] - entry, entry - ask_low))
        if be_r is not None:
            armed = live & (best >= be_r * dist)
            stop = np.where(armed, np.where(long_, np.maximum(stop, entry), np.minimum(stop, entry)), stop)
        if trail_r is not None:
            armed = live & (best >= be_r * dist)
            trail = entry + side * (best - trail_r * dist)
            stop = np.where(armed, np.where(long_, np.maximum(stop, trail), np.minimum(stop, trail)), stop)
    leftover = alive  # fin des données
    last = np.minimum(j + hold - 1, n - 1)
    exit_price[leftover] = np.where(long_, C[last] - slip, C[last] + sp[last] + slip)[leftover]
    reason[leftover] = "fin des données"
    move = (exit_price - entry) * side
    return pd.DataFrame({"bar": idx, "side": side.astype(int), "move": move, "r": move / dist, "reason": reason,
                         "day": days[idx], "stop": dist})  # fmt: skip


def stats(t: pd.DataFrame, b: pd.DataFrame) -> dict[str, object]:
    if t.empty:
        return {"trades": 0}
    wins, losses = t[t["move"] > 0], t[t["move"] <= 0]
    years = pd.to_datetime(b["time"].to_numpy()[t["bar"].to_numpy()]).year
    by_year = t.groupby(years)["r"].mean()
    reasons = t["reason"].value_counts(normalize=True)
    return {
        "trades": len(t),
        "par_jour": round(len(t) / t["day"].nunique(), 1),
        "gagnants_pct": round(len(wins) / len(t) * 100, 1),
        "pf": round(wins["move"].sum() / -losses["move"].sum(), 3) if len(losses) else float("inf"),
        "esp_r": round(t["r"].mean(), 4),
        "pips_par_trade": round(t["move"].mean() * 10, 2),
        "stop_pips": round(t["stop"].mean() * 10, 1),
        "t_jours": round(H._day_t(t["r"].to_numpy(), t["day"].to_numpy()), 1),
        "annees+": f"{int((by_year > 0).sum())}/{len(by_year)}",
        "objectif_pct": round(reasons.get("objectif", 0) * 100, 1),
        "stop_pct": round(reasons.get("stop", 0) * 100, 1),
    }


def variants() -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {"base": {}}
    out.update({name: {"filters": (name,)} for name in FILTERS})
    out.update(EXITS)
    return out


_B: pd.DataFrame | None = None
_CTX: dict[str, np.ndarray] | None = None


def _one(job: tuple[str, str, str, float, dict[str, object]]) -> dict[str, object]:
    name, period, geometry, slip, kw = job
    kw = {k: v for k, v in kw.items() if not k.startswith("_")}
    t = simulate(_B, _CTX, period, geometry=geometry, slip=slip, **kw)
    return {"variante": name, "periode": period, "geometrie": geometry, "glissement": slip, **stats(t, _B)}


def main() -> None:
    global _B, _CTX
    parser = argparse.ArgumentParser()
    parser.add_argument("--periode", default="developpement", choices=["developpement", "validation", "final", "recent"])
    parser.add_argument("--variantes", default="", help="noms séparés par des virgules (défaut : toutes)")
    parser.add_argument("--combinaison", default="", help="filtre+sortie choisis sur le développement")
    args = parser.parse_args()
    _B = H.bars()
    _CTX = context(_B)
    table = variants()
    if args.combinaison:
        f, e = args.combinaison.split("+")
        table[args.combinaison] = {"filters": (f,), **EXITS[e]}
    names = [v for v in args.variantes.split(",") if v] or list(table)
    jobs = []
    for name in names:
        for slip in (0.0, 0.10):
            if args.periode == "recent":  # géométrie pips depuis 2025 (seulement pour les retenues)
                jobs.append((name, "recent", "pips", slip, table[name]))
            else:
                jobs.append((name, args.periode, "atr", slip, table[name]))
    with mp.get_context("fork").Pool(4) as pool:
        rows = pool.map(_one, jobs)
    frame = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(frame.to_string(index=False))
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["date", *frame.columns])
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow({"date": stamp, **row})


if __name__ == "__main__":
    main()
