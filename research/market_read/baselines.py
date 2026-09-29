"""Références : hasard, sens du jour (v3), tendance EMA20/50 M1 (v1)."""
import numpy as np
from harness import day_open, daily_atr


def random_side(b, seed=0):
    # déterministe par barre (hash de l'heure) : identique sur données tronquées
    t = b["time_server"].to_numpy().astype("int64")
    return np.where(((t * 2654435761) >> 7) & 1, 1, -1).astype(int)


def day_move(b, k=0.5):
    move = b["close"].to_numpy() - day_open(b)
    atr = daily_atr(b, 14)
    with np.errstate(invalid="ignore"):
        strong = np.abs(move) >= k * atr
    return np.where(strong, np.sign(move), 0).astype(int)


def ema_trend(b, fast=20, slow=50):
    c = b["close"].astype("float64")
    f = c.ewm(span=fast, adjust=False).mean().to_numpy()
    s = c.ewm(span=slow, adjust=False).mean().to_numpy()
    d = np.sign(f - s).astype(int)
    d[: slow * 10] = 0  # chauffe (pour être identique sur données tronquées à 1e-12 près)
    return d


MODELS = {
    "hasard": (random_side, [{}]),
    "sens du jour (v3)": (day_move, [{"k": 0.5}]),
    "tendance EMA20/50 M1 (v1)": (ema_trend, [{}]),
}
