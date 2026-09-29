# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Banc d'essai commun des « lectures du marché » (sens achat / vente décidé à la clôture de chaque barre M1).

Interface d'un modèle : fn(bars: pd.DataFrame, **params) -> np.ndarray d'entiers (+1 achat, -1 vente, 0 rien),
une valeur par barre, calculée à la CLÔTURE de la barre avec les barres <= i seulement (aucun futur).

Colonnes des barres : time (UTC), time_server (epoch heure serveur, s), open, high, low, close (prix BID),
tick_volume, spread (points de 0,01 $, spread minimal de la minute). Les lignes se suivent (pas de trous
de week-end matérialisés : « n barres en arrière » saute les fermetures).

Mesures (mêmes pour tous) :
- forward(d) : mouvement signé du prix moyen sur H minutes après l'entrée (ouverture de la barre suivante),
  moins le coût aller-retour (spread de la barre avec plancher 0,15 $ + 2 x glissement) ; t-stat par blocs
  de jours (les signaux d'un même jour ne sont pas indépendants).
- simulate(d) : trades du scalper à l'échelle M1 : entrée à l'ouverture suivante (ask à l'achat, bid à la
  vente), stop = ATR M1(14) x 1 borné à [2 $, 6 $] (20 à 60 pips), objectif 3 x le stop, sortie à 10 barres,
  stop d'abord si stop et objectif sont dans la même barre, un trade à la fois.
- lookahead_check(fn) : le signal calculé sur des données tronquées doit être identique à celui calculé sur
  tout l'historique, jusqu'au même point.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]  # racine du dépôt
sys.path.insert(0, str(ROOT / "src"))

from goldbot.data.history import load_bars  # noqa: E402

POINT = 0.01
SPREAD_FLOOR = 0.15  # $ : plancher du spread (15 points), comme le moteur de backtest
PERIODS = {
    "smoke": ("2019-08-01", "2019-09-01"),  # seule période autorisée pendant la conception
    "developpement": ("2019-07-22", "2024-01-01"),
    "validation": ("2024-01-01", "2025-10-01"),
    "final": ("2025-10-01", "2100-01-01"),
}
_CACHE: dict[str, pd.DataFrame] = {}


def bars(start: str = "2019-06-01", end: str | None = None) -> pd.DataFrame:
    """Barres M1 Axi (vrai M1 depuis le 22/07/2019), index 0..n-1."""
    if "all" not in _CACHE:
        b = load_bars(ROOT / "data" / "export", "XAUUSD")
        _CACHE["all"] = b[b["time"] >= pd.Timestamp("2019-06-01", tz="UTC")].reset_index(drop=True)
    b = _CACHE["all"]
    mask = b["time"] >= pd.Timestamp(start, tz="UTC")
    if end is not None:
        mask &= b["time"] < pd.Timestamp(end, tz="UTC")
    return b[mask].reset_index(drop=True)


# ---------------------------------------------------------------- aides causales (mémoire finie, rejouables en direct)

def atr_m1(b: pd.DataFrame, n: int = 14) -> np.ndarray:
    """ATR M1 en moyenne simple sur n barres (identique quelle que soit la longueur de l'historique chargé)."""
    high, low, close = (b[c].to_numpy(dtype="float64") for c in ("high", "low", "close"))
    prev = np.concatenate([[np.nan], close[:-1]])
    tr = np.nanmax(np.vstack([high - low, np.abs(high - prev), np.abs(low - prev)]), axis=0)
    return pd.Series(tr).rolling(n, min_periods=n).mean().to_numpy()


def daily_atr(b: pd.DataFrame, days: int = 14) -> np.ndarray:
    """ATR journalier (moyenne simple des true ranges des `days` jours de cotation PRÉCÉDENTS), par barre."""
    day = b["time_server"].to_numpy() // 86_400
    g = pd.DataFrame({"d": day, "h": b["high"].to_numpy(), "l": b["low"].to_numpy(), "c": b["close"].to_numpy()})
    daily = g.groupby("d").agg(h=("h", "max"), l=("l", "min"), c=("c", "last"))
    prev = daily["c"].shift(1)
    tr = np.maximum(daily["h"] - daily["l"], np.maximum((daily["h"] - prev).abs(), (daily["l"] - prev).abs()))
    tr.iloc[0] = np.nan
    known = tr.rolling(days, min_periods=days).mean().shift(1)
    return known.reindex(day).to_numpy()


def day_open(b: pd.DataFrame) -> np.ndarray:
    day = b["time_server"].to_numpy() // 86_400
    return pd.Series(b["open"].to_numpy(dtype="float64")).groupby(day).transform("first").to_numpy()


def spread_price(b: pd.DataFrame) -> np.ndarray:
    return np.maximum(b["spread"].to_numpy(dtype="float64") * POINT, SPREAD_FLOOR)


# ---------------------------------------------------------------- contrôles

def lookahead_check(fn, params: dict | None = None, cuts: int = 6, start: str = "2019-08-01",
                    end: str = "2019-09-01") -> bool:
    """Signal sur données tronquées == signal sur tout l'échantillon, jusqu'au point de coupe."""
    params = params or {}
    b = bars(start, end)
    full = np.asarray(fn(b, **params))
    assert len(full) == len(b), "le signal doit avoir une valeur par barre"
    rng = np.random.default_rng(0)
    for cut in sorted(rng.integers(len(b) // 3, len(b) - 1, cuts)):
        part = np.asarray(fn(b.iloc[: cut + 1].reset_index(drop=True), **params))
        if not np.array_equal(part, full[: cut + 1]):
            bad = int(np.nonzero(part != full[: cut + 1])[0][0])
            raise AssertionError(f"look-ahead : barre {bad} différente avec coupe à {cut}")
    return True


def memory_check(fn, params: dict | None = None, live_bars: int = 30_000, compare: int = 2_000) -> bool:
    """Le bot en direct ne lit que les 30 000 dernières barres M1 : les dernières valeurs doivent être identiques
    à celles calculées sur un historique beaucoup plus long (mémoire finie, pas d'indicateur à mémoire infinie)."""
    params = params or {}
    b = bars("2019-08-01", "2019-12-01")
    full = np.asarray(fn(b, **params))
    short = np.asarray(fn(b.iloc[-live_bars:].reset_index(drop=True), **params))
    if not np.array_equal(full[-compare:], short[-compare:]):
        bad = int(np.nonzero(full[-compare:] != short[-compare:])[0][0])
        raise AssertionError(f"mémoire trop longue : valeur différente {compare - bad} barres avant la fin")
    return True


# ---------------------------------------------------------------- mesures

def _in(b: pd.DataFrame, period: str) -> np.ndarray:
    start, end = PERIODS[period]
    t = b["time"]
    return ((t >= pd.Timestamp(start, tz="UTC")) & (t < pd.Timestamp(end, tz="UTC"))).to_numpy()


def _day_t(values: np.ndarray, days: np.ndarray) -> float:
    if len(values) < 2:
        return float("nan")
    per_day = pd.Series(values).groupby(days).sum()
    if len(per_day) < 2 or per_day.std(ddof=1) == 0:
        return float("nan")
    return float(per_day.mean() / (per_day.std(ddof=1) / np.sqrt(len(per_day))))


def forward(b: pd.DataFrame, d: np.ndarray, period: str = "developpement", horizons=(1, 5, 15),
            slip: float = 0.0) -> dict[str, float]:
    """Mouvement signé après le signal (ouverture suivante -> clôture H barres plus tard), net du coût."""
    d = np.asarray(d)
    n = len(b)
    o, c = b["open"].to_numpy(), b["close"].to_numpy()
    spread = spread_price(b)
    days = b["time_server"].to_numpy() // 86_400
    mask = _in(b, period) & (d != 0)
    idx = np.nonzero(mask)[0]
    out: dict[str, float] = {"signaux": int(len(idx)), "achats_pct": float((d[idx] > 0).mean() * 100) if len(idx) else 0}
    for h in horizons:
        ok = idx[idx + h < n]
        entry = o[ok + 1]
        exit_ = c[ok + h]
        # même jour de cotation seulement (pas de trade à travers la fermeture)
        same = days[ok + h] == days[ok]
        ok, entry, exit_ = ok[same], entry[same], exit_[same]
        signed = (exit_ - entry) * d[ok]
        net = signed - spread[ok + 1] - 2 * slip
        out[f"brut_{h}m"] = float(signed.mean()) if len(ok) else float("nan")
        out[f"net_{h}m"] = float(net.mean()) if len(ok) else float("nan")
        out[f"reussite_{h}m"] = float((net > 0).mean() * 100) if len(ok) else float("nan")
        out[f"t_{h}m"] = _day_t(net, days[ok])
    return out


def simulate(b: pd.DataFrame, d: np.ndarray, period: str = "developpement", *, stop_atr: float = 1.0,
             min_stop: float = 2.0, max_stop: float = 6.0, target_ratio: float = 3.0, max_hold: int = 10,
             slip: float = 0.0) -> tuple[dict[str, float], pd.DataFrame]:
    """Trades à l'échelle M1 (voir l'en-tête), un à la fois. Résultats en $ par once et en R."""
    d = np.asarray(d)
    o, h, l, c = (b[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    spread = spread_price(b)
    atr = atr_m1(b, 14)
    days = b["time_server"].to_numpy() // 86_400
    inside = _in(b, period)
    n = len(b)
    rows = []
    i = 0
    while i < n - 2:
        side = d[i]
        if side == 0 or not inside[i] or np.isnan(atr[i]) or days[i + 1] != days[i]:
            i += 1
            continue
        stop = min(max(stop_atr * atr[i], min_stop), max_stop)
        j = i + 1
        entry = o[j] + spread[j] + slip if side > 0 else o[j] - slip
        sl = entry - side * stop
        tp = entry + side * target_ratio * stop
        last = min(i + max_hold, n - 1)
        exit_price, reason, k = None, "durée max", last
        for k in range(j, last + 1):
            if days[k] != days[i]:  # fermeture du jour : sortie à la dernière barre du jour
                k -= 1
                exit_price = c[k] - slip if side > 0 else c[k] + spread[k] + slip
                reason = "fin de jour"
                break
            if side > 0:
                if l[k] <= sl:
                    exit_price, reason = min(sl, o[k]) - slip if k > j else sl - slip, "stop"
                    break
                if h[k] >= tp:
                    exit_price, reason = tp, "objectif"
                    break
            else:
                ask_high, ask_low, ask_open = h[k] + spread[k], l[k] + spread[k], o[k] + spread[k]
                if ask_high >= sl:
                    exit_price, reason = max(sl, ask_open) + slip if k > j else sl + slip, "stop"
                    break
                if ask_low <= tp:
                    exit_price, reason = tp, "objectif"
                    break
        if exit_price is None:
            k = last
            exit_price = c[k] - slip if side > 0 else c[k] + spread[k] + slip
        move = (exit_price - entry) * side
        rows.append((i, int(side), entry, exit_price, move, move / stop, reason, days[i], k - i))
        i = k + 1
    t = pd.DataFrame(rows, columns=["bar", "side", "entry", "exit", "move", "r", "reason", "day", "bars"])
    return _trade_stats(t, b), t


def _trade_stats(t: pd.DataFrame, b: pd.DataFrame) -> dict[str, float]:
    if t.empty:
        return {"trades": 0}
    wins, losses = t[t["move"] > 0], t[t["move"] <= 0]
    gross_loss = -losses["move"].sum()
    years = pd.to_datetime(b["time"].to_numpy()[t["bar"].to_numpy()]).year
    by_year = t.groupby(years)["r"].mean()
    long_, short = t[t["side"] > 0], t[t["side"] < 0]
    return {
        "trades": len(t),
        "trades_par_jour": len(t) / max(t["day"].nunique(), 1),
        "gagnants_pct": len(wins) / len(t) * 100,
        "pf": float(wins["move"].sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "esp_r": float(t["r"].mean()),
        "net_usd_oz": float(t["move"].sum()),
        "t_jours": _day_t(t["r"].to_numpy(), t["day"].to_numpy()),
        "esp_r_achats": float(long_["r"].mean()) if len(long_) else float("nan"),
        "esp_r_ventes": float(short["r"].mean()) if len(short) else float("nan"),
        "annees_positives": f"{int((by_year > 0).sum())}/{len(by_year)}",
        "pire_annee_r": float(by_year.min()),
    }
