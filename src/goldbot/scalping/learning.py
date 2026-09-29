"""Filtre statistique entraîné hors ligne ; paramètres JSON, aucune exécution de pickle.

Le score estime la probabilité d'un résultat net positif selon les observations
d'entraînement. Ce n'est ni une prévision certaine ni une promesse de rentabilité.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from goldbot.config import Settings
from goldbot.scalping.engine import Candle, Plan, Setup

FEATURES = (
    "side", "spread_r", "cost_r", "body_r", "range_r", "momentum_r",
    "efficiency", "range_position", "tick_ratio", "hour_sin", "hour_cos",
)
VERSION = 1
THRESHOLD = 0.60  # fixé AVANT le test ; pas de recherche du meilleur seuil sur le test


def strategy_signature(settings: Settings) -> str:
    cfg = settings.scalping.model_dump(mode="json")
    # Le modèle décrit les signaux / coûts, pas le nom du fichier de collecte.
    for key in ("experiment_name", "magic", "experiment_days", "cadence_verified", "broker_min_seconds_between_orders"):
        cfg.pop(key)
    data = {"strategy": cfg, "server_time": settings.server_time.model_dump(mode="json"),
            "commission": settings.backtest.commission_per_lot_side, "features": FEATURES, "version": VERSION}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def signal_features(setup: Setup, recent: list[Candle], plan: Plan) -> list[float]:
    history = [c for c in recent if c.start_ms <= setup.candle.start_ms]
    if not history or plan.stop_distance <= 0:
        raise ValueError("historique ou stop invalide pour le modèle")
    c, side, risk = setup.candle, setup.side, plan.stop_distance
    closes = [x.close for x in history]
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    high, low = max(x.high for x in history), min(x.low for x in history)
    position = (c.close - low) / max(high - low, 1e-8)
    hour = ((c.start_ms // 1000) % 86400) / 86400 * 2 * math.pi
    result = [float(side), plan.spread / risk, plan.cost / risk, side * (c.close - c.open) / risk,
              (high - low) / risk, side * (c.close - closes[0]) / risk,
              abs(c.close - closes[0]) / max(path, 1e-8), position if side > 0 else 1 - position,
              c.ticks / max(sum(x.ticks for x in history) / len(history), 1), math.sin(hour), math.cos(hour)]
    if not all(math.isfinite(x) for x in result):
        raise ValueError("indicateurs non finis")
    return result


def _sigmoid(value):
    return 1 / (1 + np.exp(-np.clip(value, -40, 40)))


@dataclass(frozen=True)
class SignalModel:
    payload: dict

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.payload, sort_keys=True).encode()).hexdigest()

    @property
    def approved(self) -> bool:
        return self.payload.get("eligible_for_demo_filter") is True

    @classmethod
    def load(cls, path: Path, settings: Settings) -> SignalModel:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != VERSION or data.get("features") != list(FEATURES):
            raise ValueError("format ou indicateurs du modèle incompatibles")
        if data.get("strategy_signature") != strategy_signature(settings):
            raise ValueError("modèle incompatible avec les réglages / coûts de cette stratégie")
        n = len(FEATURES)
        for key in ("mean", "scale", "coefficients"):
            if len(data.get(key, [])) != n or not all(math.isfinite(float(v)) for v in data[key]):
                raise ValueError("paramètres du modèle invalides")
        if any(v <= 0 for v in data["scale"]):
            raise ValueError("normalisation du modèle invalide")
        for key in ("intercept", "calibration_slope", "calibration_intercept", "threshold"):
            if not math.isfinite(float(data[key])):
                raise ValueError("paramètres du modèle non finis")
        if data["threshold"] != THRESHOLD:
            raise ValueError("seuil du modèle incompatible")
        if not isinstance(data.get("data_through_ms"), int) or data["data_through_ms"] <= 0:
            raise ValueError("date des données du modèle invalide")
        return cls(data)

    def probability(self, values: list[float] | np.ndarray):
        x = np.asarray(values, dtype=float)
        if x.shape[-1] != len(FEATURES) or not np.isfinite(x).all():
            raise ValueError("indicateurs du modèle invalides")
        p = self.payload
        score = ((x - p["mean"]) / p["scale"]) @ np.asarray(p["coefficients"]) + p["intercept"]
        return _sigmoid(p["calibration_slope"] * score + p["calibration_intercept"])


def chronological_split(frame: pd.DataFrame, embargo_ms: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """60 % apprentissage, 20 % calibration, 20 % test ; purge des labels qui chevauchent.

    Les bornes sont temporelles : les signaux simultanés ne traversent pas un découpage.
    Les dernières positions encore ouvertes ne doivent pas être fournies à cette fonction.
    """
    frame = frame.sort_values("open_ms").reset_index(drop=True)
    n = len(frame)
    if n < 300:
        raise ValueError(f"{n} trades clôturés ; il en faut au moins 300 pour cet essai ML")
    b1, b2 = int(frame.iloc[int(n * .6)].open_ms), int(frame.iloc[int(n * .8)].open_ms)
    train = frame[(frame.open_ms < b1) & (frame.exit_ms < b1 - embargo_ms)]
    calibration = frame[(frame.open_ms >= b1) & (frame.open_ms < b2) & (frame.exit_ms < b2 - embargo_ms)]
    test = frame[frame.open_ms >= b2]
    if min(map(len, (train, calibration, test))) < 30:
        raise ValueError("pas assez de trades indépendants après purge temporelle")
    return train, calibration, test


def performance(values: np.ndarray) -> dict:
    values = np.asarray(values, dtype=float)
    gain, loss = float(values[values > 0].sum()), -float(values[values < 0].sum())
    return {"trades": len(values), "net_r": float(values.sum()),
            "mean_r": float(values.mean()) if len(values) else 0.0,
            "profit_factor": gain / loss if loss else None}


def train_model(frame: pd.DataFrame, settings: Settings, *, source: str) -> SignalModel:
    # Import paresseux : MT5 n'a pas besoin de sklearn pour utiliser le JSON figé.
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    cols = [f"f_{name}" for name in FEATURES]
    required = ["open_ms", "exit_ms", "net_r", *cols]
    if not set(required).issubset(frame.columns):
        raise ValueError("collecte sans indicateurs : relancer le nouveau bot avant l'apprentissage")
    frame = frame.sort_values("open_ms").reset_index(drop=True)
    if not np.isfinite(frame[required].to_numpy(dtype=float)).all() or (frame.exit_ms < frame.open_ms).any():
        raise ValueError("données d'apprentissage invalides")
    train, calibration, test = chronological_split(frame, settings.scalping.max_hold_s * 1000)
    for part in (train, calibration):
        if (part.net_r > 0).nunique() != 2:
            raise ValueError("apprentissage / calibration nécessitent des gains ET des pertes")
    scaler = StandardScaler().fit(train[cols])
    classifier = LogisticRegression(C=1.0, max_iter=1000).fit(scaler.transform(train[cols]), train.net_r > 0)
    calibration_scores = classifier.decision_function(scaler.transform(calibration[cols])).reshape(-1, 1)
    calibrator = LogisticRegression(C=1.0, max_iter=1000).fit(calibration_scores, calibration.net_r > 0)
    model = SignalModel({
        "version": VERSION, "features": list(FEATURES), "strategy_signature": strategy_signature(settings),
        "source": source, "threshold": THRESHOLD,
        "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
        "coefficients": classifier.coef_[0].tolist(), "intercept": float(classifier.intercept_[0]),
        "calibration_slope": float(calibrator.coef_[0, 0]), "calibration_intercept": float(calibrator.intercept_[0]),
    })
    probabilities = model.probability(test[cols].to_numpy())
    selected = test.net_r.to_numpy()[probabilities >= THRESHOLD]
    baseline, filtered = performance(test.net_r.to_numpy()), performance(selected)
    observed_days = int((test.open_ms // 86400000).nunique())
    # Un filtre démo peut être essayé s'il améliore le total net sur les mêmes occasions.
    eligible = len(selected) >= 30 and observed_days >= 5 and filtered["net_r"] > max(0, baseline["net_r"])
    model.payload.update({
        "eligible_for_demo_filter": eligible, "training_samples": len(train), "calibration_samples": len(calibration),
        "test_samples": len(test), "test_days": observed_days,
        "trained_through_ms": int(calibration.exit_ms.max()), "test_from_ms": int(test.open_ms.min()),
        "data_through_ms": int(frame.exit_ms.max()),
        "dataset_sha256": hashlib.sha256(frame[required].to_csv(index=False).encode()).hexdigest(),
        "test_baseline": baseline, "test_filtered": filtered,
        "brier_score": float(np.mean((probabilities - (test.net_r.to_numpy() > 0)) ** 2)),
        "constant_brier_score": float(np.mean(((calibration.net_r > 0).mean() - (test.net_r.to_numpy() > 0)) ** 2)),
        "note": "Test rétrospectif exploratoire ; prochaine validation sur de nouvelles données. Aucun gain garanti.",
    })
    return model
