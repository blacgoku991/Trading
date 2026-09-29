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


@pytest.fixture(scope="session")
def settings():
    return load_settings(CONFIG_PATH)


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
