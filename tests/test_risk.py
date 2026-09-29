"""Taille de position et limites de risque (CLAUDE.md §3.5 et §9)."""

import pandas as pd
import pytest

from goldbot.risk.limits import DAILY_LOSS, MAX_DRAWDOWN, RiskLimits
from goldbot.risk.sizing import floor_to_step, position_size


@pytest.mark.parametrize(
    ("volume", "step", "expected"),
    [(0.029, 0.01, 0.02), (0.3, 0.01, 0.3), (0.30000000000000004, 0.01, 0.3), (1.2345, 0.1, 1.2), (0.0099, 0.01, 0.0)],
)
def test_floor_to_step(volume, step, expected):
    assert floor_to_step(volume, step) == expected


def test_position_size_rounds_down_and_never_up():
    # 50 $ de risque, 2 $ de SL sur 100 oz : 0,25 lot exactement ; 60 $ -> 0,30 ; 59,99 $ -> 0,29.
    kwargs = dict(volume_min=0.01, volume_max=20.0, volume_step=0.01)
    assert position_size(50.0, 200.0, **kwargs) == 0.25
    assert position_size(59.99, 200.0, **kwargs) == 0.29
    assert position_size(1.0, 200.0, **kwargs) == 0.0  # 0,005 lot < minimum : trade ignoré, jamais grossi
    assert position_size(1e9, 200.0, **kwargs) == 20.0  # plafonné au volume maximal
    assert position_size(50.0, 0.0, **kwargs) == 0.0


def _limits(settings, equity=10_000.0):
    limits = RiskLimits.start(settings.risk, equity)
    limits.new_day(1, equity)
    return limits


NOW = pd.Timestamp("2026-01-06 10:00", tz="UTC")


def test_risk_money_is_half_a_percent_of_equity(settings):
    assert _limits(settings).risk_money(10_000.0) == pytest.approx(50.0)


def test_daily_loss_stops_the_day_then_resets(settings):
    limits = _limits(settings)
    assert limits.on_equity(9_850.0) == []
    assert limits.on_equity(9_800.0) == [DAILY_LOSS]  # -2 % dans la journée, flottant inclus
    assert limits.entry_block(NOW, 0) == DAILY_LOSS
    assert limits.on_equity(9_790.0) == []  # signalé une seule fois
    limits.new_day(2, 9_790.0)
    assert limits.entry_block(NOW, 0) is None


def test_max_drawdown_halts_everything(settings):
    limits = _limits(settings)
    limits.on_equity(11_000.0)  # nouveau plus haut
    limits.new_day(2, 10_000.0)
    assert limits.on_equity(9_900.0) == [MAX_DRAWDOWN]  # -10 % depuis 11 000
    assert limits.halted
    limits.new_day(3, 9_900.0)
    assert limits.entry_block(NOW, 0) == MAX_DRAWDOWN  # relance manuelle uniquement


def test_positions_and_trades_per_day_are_capped(settings):
    limits = _limits(settings)
    assert limits.entry_block(NOW, settings.risk.max_open_positions) == "nombre maximal de positions ouvertes"
    for _ in range(settings.risk.max_trades_per_day):
        limits.on_entry()
    assert limits.entry_block(NOW, 0) == "nombre maximal de trades du jour"
    limits.new_day(2, 10_000.0)
    assert limits.entry_block(NOW, 0) is None


def test_losing_streak_pauses_until_the_next_session(settings):
    limits = _limits(settings)
    resume = NOW + pd.Timedelta(hours=3)
    for _ in range(settings.risk.max_consecutive_losses - 1):
        limits.on_exit(-10.0, NOW, lambda now: resume)
    assert limits.entry_block(NOW, 0) is None
    limits.on_exit(-10.0, NOW, lambda now: resume)
    assert "pertes d'affilée" in limits.entry_block(NOW, 0)
    assert limits.entry_block(resume, 0) is None


def test_a_win_resets_the_losing_streak(settings):
    limits = _limits(settings)
    limits.on_exit(-10.0, NOW, lambda now: NOW)
    limits.on_exit(-10.0, NOW, lambda now: NOW)
    limits.on_exit(5.0, NOW, lambda now: NOW)
    limits.on_exit(-10.0, NOW, lambda now: NOW + pd.Timedelta(hours=1))
    assert limits.entry_block(NOW, 0) is None
