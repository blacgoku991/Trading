"""Test de l'expérience de scalping sur l'historique de ticks, avec les règles du compte.

Mêmes briques que le bot en direct (engine.py), rejouées tick par tick dans l'ordre. Entrée au
premier tick après la bougie de confirmation, à l'ask ou au bid, plus le glissement choisi.
Mêmes refus qu'en direct : signal périmé, marché qui ferme avant la durée maximale, perte du jour,
délai entre entrées, positions et risque cumulé au maximum, lot minimal au-dessus du budget.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from goldbot.config import ScalpingConfig
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.quality import server_index
from goldbot.risk.sizing import position_size
from goldbot.scalping.engine import BreakoutDetector, CandleBuilder, SimTrade, plan_trade

_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class Instrument:
    point: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int
    freeze_level_points: int


@dataclass
class ScalpResult:
    trades: pd.DataFrame
    refusals: Counter
    signals: int
    initial_equity: float
    final_equity: float
    equity_low: float
    entries_per_minute: Counter = field(default_factory=Counter)


def reason_key(message: str) -> str:
    """« stop trop loin : 7.10 $ > 6.00 $ » -> « stop trop loin » (pour compter les refus par motif)."""
    return message.split(" :")[0]


def trend_by_minute(bars: pd.DataFrame, fast: int, slow: int) -> tuple[np.ndarray, np.ndarray]:
    """Heures d'ouverture (epoch serveur, s) des barres M1 et tendance EMA rapide / lente de chaque barre."""
    close = bars["close"].astype("float64")
    fast_ema = close.ewm(span=fast, adjust=False).mean().to_numpy()
    slow_ema = close.ewm(span=slow, adjust=False).mean().to_numpy()
    trend = np.sign(fast_ema - slow_ema).astype(int)
    trend[: slow - 1] = 0  # pas assez de barres
    return bars["time_server"].to_numpy(dtype="int64"), trend


def run_backtest(
    ticks: pd.DataFrame,
    bars: pd.DataFrame,
    config: ScalpingConfig,
    *,
    instrument: Instrument,
    schedule: MarketSchedule,
    initial_equity: float,
    slippage_points: float,
    min_seconds_between_entries: float | None = None,
    commission_per_lot_side: float = 0.0,
) -> ScalpResult:
    """Rejoue les ticks (colonnes time_msc_server, bid, ask) ; montants dans la devise de cotation (USD)."""
    server = server_index(ticks["time_msc_server"].to_numpy() // 1000)
    open_mask = schedule.open_mask(server)
    times = ticks["time_msc_server"].to_numpy(dtype="int64")[open_mask]
    bids = ticks["bid"].to_numpy(dtype="float64")[open_mask]
    asks = ticks["ask"].to_numpy(dtype="float64")[open_mask]
    bar_times, trends = trend_by_minute(bars, config.ema_fast, config.ema_slow)

    point = instrument.point
    slip = slippage_points * point
    fee = 2 * commission_per_lot_side / instrument.contract_size  # commission aller-retour, par once
    spacing_ms = 1000 * (config.min_seconds_between_entries if min_seconds_between_entries is None
                         else min_seconds_between_entries)  # fmt: skip
    size_ms = config.candle_seconds * 1000
    builder, detector = CandleBuilder(config.candle_seconds), BreakoutDetector(config)
    recent: deque = deque()
    equity = low = initial_equity
    open_trades: list[tuple[SimTrade, float]] = []  # (trade, risque en USD)
    closed: list[dict[str, object]] = []
    refusals: Counter = Counter()
    per_minute: Counter = Counter()
    signals = 0
    last_entry = -(10**18)
    day, day_pnl = None, 0.0

    for i in range(len(times)):
        now, bid, ask = int(times[i]), float(bids[i]), float(asks[i])
        # 1. Positions ouvertes : stop, objectif, durée maximale.
        if open_trades:
            still = []
            for trade, risk in open_trades:
                if trade.on_tick(now, bid, ask):
                    pnl = trade.move * trade.volume * instrument.contract_size
                    equity += pnl
                    day_pnl += pnl
                    low = min(low, equity)
                    closed.append(
                        {
                            "tag": trade.tag,
                            "side": trade.side,
                            "open_ms": trade.open_ms,
                            "exit_ms": trade.exit_ms,
                            "entry": trade.entry,
                            "sl": trade.sl,
                            "tp": trade.tp,
                            "exit": trade.exit_price,
                            "reason": trade.reason,
                            "lots": trade.volume,
                            "risk": risk,
                            "pnl": pnl,
                        }
                    )
                else:
                    still.append((trade, risk))
            open_trades = still
        # 2. Nouvelle bougie terminée : signal éventuel, décidé au prix de ce tick.
        for candle in builder.add(now, bid, ask):
            recent.append(candle)
            while recent and recent[0].start_ms < candle.start_ms - config.breakout_lookback_s * 1000:
                recent.popleft()
            bar = int(np.searchsorted(bar_times, (candle.start_ms + size_ms) // 1000 - 60, side="right")) - 1
            trend = int(trends[bar]) if bar >= 0 else 0
            setup = detector.on_candle(candle, trend)
            if setup is None:
                continue
            signals += 1
            today = now // 86_400_000
            if today != day:
                day, day_pnl = today, 0.0
            if now > candle.start_ms + 2 * size_ms:
                refusals["signal périmé"] += 1
                continue
            if not schedule.is_open(_EPOCH + timedelta(milliseconds=now + config.max_hold_s * 1000)):
                refusals["le marché ferme avant la durée max"] += 1
                continue
            if day_pnl <= -config.daily_loss_pct / 100 * equity:
                refusals["perte du jour atteinte"] += 1
                continue
            if now - last_entry < spacing_ms:
                refusals["délai entre deux entrées"] += 1
                continue
            if len(open_trades) >= config.max_open_positions:
                refusals["positions ouvertes au maximum"] += 1
                continue
            plan = plan_trade(
                setup,
                bid,
                ask,
                list(recent),
                config,
                point=point,
                stops_level_points=instrument.stops_level_points,
                freeze_level_points=instrument.freeze_level_points,
                commission_per_oz=fee,
            )
            if isinstance(plan, str):
                refusals[reason_key(plan)] += 1
                continue
            # Comme en direct : perte au stop avec le glissement attendu, commission comprise.
            worst_move = plan.stop_distance + config.expected_slippage_points * point + fee
            loss_per_lot = worst_move * instrument.contract_size
            lots = position_size(
                equity * config.risk_per_trade_pct / 100,
                loss_per_lot,
                volume_min=instrument.volume_min,
                volume_max=instrument.volume_max,
                volume_step=instrument.volume_step,
            )
            if lots == 0:
                refusals["lot minimum au-dessus du budget"] += 1
                continue
            risk = lots * loss_per_lot
            if sum(r for _, r in open_trades) + risk > config.max_total_risk_pct / 100 * equity:
                refusals["risque cumulé au maximum"] += 1
                continue
            trade = SimTrade.open(setup.tag, plan, bid, ask, now, config.max_hold_s, slip, volume=lots, fee=fee)
            open_trades.append((trade, risk))
            last_entry = now
            per_minute[now // 60_000] += 1

    frame = pd.DataFrame(closed)
    return ScalpResult(frame, refusals, signals, initial_equity, equity, low, Counter(per_minute.values()))


def summary(result: ScalpResult, days: float) -> dict[str, float]:
    trades = result.trades
    if trades.empty:
        return {"trades": 0}
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] <= 0]
    equity = result.initial_equity + trades.sort_values("exit_ms")["pnl"].cumsum()
    peak = np.maximum.accumulate(np.concatenate([[result.initial_equity], equity.to_numpy()]))
    drawdown = (np.concatenate([[result.initial_equity], equity.to_numpy()]) / peak - 1).min() * 100
    gross_loss = -losses["pnl"].sum()
    return {
        "trades": len(trades),
        "par_jour": len(trades) / days,
        "gagnants_pct": len(wins) / len(trades) * 100,
        "gain_moyen": wins["pnl"].mean() if len(wins) else 0.0,
        "perte_moyenne": losses["pnl"].mean() if len(losses) else 0.0,
        "gains": wins["pnl"].sum(),
        "pertes": losses["pnl"].sum(),
        "profit_factor": wins["pnl"].sum() / gross_loss if gross_loss > 0 else float("inf"),
        "net": trades["pnl"].sum(),
        "drawdown_pct": drawdown,
        "duree_moyenne_s": ((trades["exit_ms"] - trades["open_ms"]) / 1000).mean(),
        "stop": int((trades["reason"] == "stop").sum()),
        "objectif": int((trades["reason"] == "objectif").sum()),
        "duree_max": int((trades["reason"] == "durée max").sum()),
    }
