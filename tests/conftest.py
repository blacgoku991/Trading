from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from goldbot.broker.fake_broker import FakeBroker, make_tick
from goldbot.config import load_settings
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "settings.yaml"

# Mardi 29/09/2026 10:00 UTC = 06:00 à New York (heure d'été) = 13:00 heure serveur : marché ouvert.
OPEN_NOW = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
# Samedi : marché fermé.
WEEKEND_NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def tick_at(rule, now, *, age_s=1.0, bid=4150.00, ask=4150.15):
    """Tick horodaté comme MT5 (epoch serveur), vieux de age_s secondes à l'instant now."""
    return make_tick(bid, ask, int((rule.server_epoch(now) - age_s) * 1000))


def open_minutes(schedule, start, end):
    """Epochs serveur (s) de chaque minute de cotation de [start, end[ (heures serveur)."""
    minutes = pd.date_range(start, end, freq="min", inclusive="left")
    minutes = minutes[schedule.open_mask(minutes)]
    return ((minutes - pd.Timestamp(0)) // pd.Timedelta(seconds=1)).to_numpy()


def open_instants_ms(schedule, start, end, step="10s"):
    """Epochs serveur (ms), tous les step, pendant la cotation de [start, end[ (heures serveur)."""
    instants = pd.date_range(start, end, freq=step, inclusive="left")
    instants = instants[schedule.open_mask(instants)]
    return ((instants - pd.Timestamp(0)) // pd.Timedelta(milliseconds=1)).to_numpy()


def server_epoch_of(text):
    """« 2026-01-06 13:00 » (heure serveur) -> epoch serveur en secondes."""
    return int((pd.Timestamp(text) - pd.Timestamp(0)) // pd.Timedelta(seconds=1))


def v1_exit_settings():
    """Réglages du dépôt, sauf le scalper : sorties et risque de la v1 (0,1 % par trade calculé, -1 % par jour) et
    les deux stratégies actives. Les tests de scalping
    sont écrits avec ces valeurs et ne doivent pas dépendre des réglages choisis pour la démo."""
    settings = load_settings(CONFIG_PATH)
    data = settings.scalping.model_dump()
    data.update(
        min_stop_points=50,
        target_ratio=1.2,
        max_hold_s=120,
        fixed_volume=None,
        risk_per_trade_pct=0.1,
        max_total_risk_pct=0.5,
        daily_loss_pct=1.0,
    )
    data["cadence"].update(ceiling_total_risk_pct=1.0)
    data["learning"].update(version=1, target_ratios=[0.8, 1.2, 2.0])
    data["pullback"]["enabled"] = True  # les tests couvrent les deux stratégies
    return settings.model_copy(update={"scalping": type(settings.scalping).model_validate(data)})


@pytest.fixture(scope="session")
def settings():
    return v1_exit_settings()


@pytest.fixture
def rule(settings):
    return ServerTimeRule.from_config(settings.server_time)


@pytest.fixture
def schedule(settings):
    return MarketSchedule.from_config(settings.market_hours)


@pytest.fixture
def broker(rule):
    fake = FakeBroker(ticks={"XAUUSD": tick_at(rule, OPEN_NOW)}, commission_per_lot_side=5.0)
    fake.connect()
    return fake
