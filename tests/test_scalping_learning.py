"""Tests causaux et validation des paramètres ML ; données synthétiques, aucune preuve de gain."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from goldbot.config import load_settings
from goldbot.scalping.engine import Candle, Plan, Setup
from goldbot.scalping.learning import FEATURES, SignalModel, chronological_split, signal_features, train_model
from goldbot.scalping.pullback import PullbackDetector
from tests.conftest import ROOT


def bar(k, opening, close, high=None, low=None):
    return Candle(k * 5000, opening, high if high is not None else max(opening, close) + .05,
                  low if low is not None else min(opening, close) - .05, close, close - .08,
                  close + .08, .16, 10)


def sequence():
    return ([bar(k, 4000, 4000) for k in range(12)] +
            [bar(12, 4000, 4001), bar(13, 4001, 4000.70), bar(14, 4000.70, 4000.80),
             bar(15, 4000.80, 4000.95), bar(16, 4000.95, 4001.00)])


@pytest.mark.parametrize("side", [1, -1])
def test_pullback_requires_impulse_retracement_and_resumption_and_fires_once(settings, side):
    candles = sequence()
    if side < 0:
        candles = [replace(c, open=8000-c.open, close=8000-c.close, high=8000-c.low,
                           low=8000-c.high, bid=8000-c.ask, ask=8000-c.bid) for c in candles]
    detector = PullbackDetector(settings.scalping)
    signals = [detector.on_candle(c, side) for c in candles]
    assert all(s is None for s in signals[:15])
    assert signals[15].side == side and signals[16] is None


def test_deep_pullback_and_gap_cancel_the_setup(settings):
    for bad in (bar(13, 4001, 4000.1), replace(sequence()[13], start_ms=100000)):
        detector = PullbackDetector(settings.scalping)
        for c in sequence()[:13]:
            assert detector.on_candle(c, 1) is None
        assert detector.on_candle(bad, 1) is None
        assert detector.impulse is None


def test_appending_future_candles_cannot_change_a_signal_or_its_features(settings):
    candles = sequence()
    def feed(data):
        detector = PullbackDetector(settings.scalping)
        return [detector.on_candle(c, 1) for c in data]
    assert feed(candles[:16]) == feed(candles)[:16]
    setup = feed(candles)[15]
    plan = Plan(1, 4001.03, 3999.8, 4002.5, 1.23, 1.47, .3, .16)
    assert signal_features(setup, candles[:16], plan) == signal_features(setup, candles, plan)


def samples(n=1500):
    rng = np.random.default_rng(42)
    x = rng.normal(size=(n, len(FEATURES)))
    opened = 1700000000000 + np.arange(n) * 3600000
    frame = pd.DataFrame(x, columns=[f"f_{name}" for name in FEATURES])
    frame["open_ms"], frame["exit_ms"] = opened, opened + 120000
    # Signal connu sur ce jeu synthétique ; la plupart des occasions restent perdantes.
    frame["net_r"] = np.where(x[:, 0] > .8, 1.2, -1.0)
    return frame


def test_split_removes_overlapping_labels_and_groups_simultaneous_signals():
    frame = samples()
    frame.loc[899, "exit_ms"] = frame.iloc[901].open_ms
    a, b, c = chronological_split(frame, 120000)
    assert a.exit_ms.max() < b.open_ms.min() - 120000
    assert b.exit_ms.max() < c.open_ms.min() - 120000
    assert 899 not in a.index


def test_holdout_labels_do_not_change_learned_parameters_and_json_predictions(tmp_path, settings):
    frame = samples()
    first = train_model(frame, settings, source="synthetic test")
    frame.loc[1200:, "net_r"] *= -1
    second = train_model(frame, settings, source="synthetic test")
    for field in ("mean", "scale", "coefficients", "intercept", "calibration_slope", "calibration_intercept"):
        assert first.payload[field] == second.payload[field]
    assert first.approved and not second.approved
    path = tmp_path / "model.json"
    path.write_text(json.dumps(first.payload))
    loaded = SignalModel.load(path, settings)
    features = samples().iloc[0][[f"f_{name}" for name in FEATURES]].to_numpy(dtype=float)
    assert loaded.probability(features) == pytest.approx(first.probability(features))
    changed = settings.model_copy(update={"scalping": settings.scalping.model_copy(update={"target_ratio": 2.0})})
    with pytest.raises(ValueError, match="incompatible"):
        SignalModel.load(path, changed)


def test_learning_rejects_too_little_data_and_nonfinite_features(settings):
    with pytest.raises(ValueError, match="au moins 300"):
        train_model(samples(100), settings, source="synthetic test")
    frame = samples()
    frame.loc[0, "f_cost_r"] = float("nan")
    with pytest.raises(ValueError, match="invalides"):
        train_model(frame, settings, source="synthetic test")


def test_demo_profile_has_distinct_identity_and_five_entry_cap():
    settings = load_settings(ROOT / "config" / "learning_demo.yaml")
    assert settings.scalping.strategy == "pullback"
    assert settings.scalping.max_entries_per_minute == 5
    assert settings.scalping.max_open_positions == 5
    assert settings.scalping.broker_min_seconds_between_orders == 12
    assert settings.scalping.experiment_name == "learning_demo"


def test_training_command_uses_the_recorded_stressed_outcomes_and_never_overwrites(tmp_path, settings):
    from goldbot.scalping.learning import strategy_signature
    from goldbot.scalping.store import NOT_SENT, ScalpStore
    from goldbot.scalping.train_cli import main
    from tests.conftest import CONFIG_PATH
    path, output = tmp_path / "collect.sqlite", tmp_path / "model.json"
    store = ScalpStore(path)
    store.set_meta("strategy_signature", strategy_signature(settings))
    store.set_meta("fee_per_oz", "0.1")
    for k, row in samples(300).iterrows():
        tag = f"PB-{k}"
        # Stop initial à 1 $, résultat net donné par le label synthétique, fee soustraite une seule fois.
        store.record(tag, int(row.open_ms), 1, NOT_SENT, entry=4000, sl=3999)
        store.record_features(tag, row[[f"f_{x}" for x in FEATURES]].tolist(), None, None)
        store.record_sim(tag, int(row.open_ms), 1, 4000, 3999, 4001.2, .01, False)
        store.close_sim(tag, 4000 + float(row.net_r) + .1, "objectif" if row.net_r > 0 else "stop",
                        float(row.net_r), int(row.exit_ms))
    store.close()
    args = ["--config", str(CONFIG_PATH), "--store", str(path), "--output", str(output)]
    messages = []
    assert main(args, echo=messages.append) == 0, messages
    model = SignalModel.load(output, settings)
    assert model.payload["source"].startswith("signaux collectés en démo")
    previous = output.read_bytes()
    assert main(args, echo=messages.append) == 2
    assert output.read_bytes() == previous
