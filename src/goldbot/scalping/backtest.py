"""Rejeu de l'expérience de scalping sur l'historique de ticks, avec les règles du compte.

Mêmes briques que le bot en direct (engine.py, policy.py), rejouées tick par tick dans l'ordre. Entrée au
premier tick après la bougie de signal, à l'ask ou au bid, plus le glissement choisi. Mêmes refus qu'en
direct : signal périmé, marché qui ferme avant la durée maximale, plan (stop, coût), lot minimal
au-dessus du budget, perte du jour, même occasion déjà en position, positions et risque cumulé au
maximum, entrées par minute, délai entre entrées ; cadence liée au bénéfice (policy.Cadence), mise à jour à
chaque trade fermé comme en direct.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from goldbot.config import ScalpingConfig
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.quality import server_index
from goldbot.indicators.core import atr as atr_series
from goldbot.risk.sizing import position_size
from goldbot.scalping.engine import CandleBuilder, SimTrade, make_detectors, plan_trade
from goldbot.scalping.learning import LearningBook
from goldbot.scalping.policy import EntryPolicy, Exposure, reason_key, split_volume

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
    refusals: Counter  # (stratégie, motif) -> nombre
    signals: Counter  # stratégie -> nombre
    initial_equity: float
    final_equity: float
    equity_low: float
    entries_per_minute: Counter = field(default_factory=Counter)


def context_by_minute(bars: pd.DataFrame, config: ScalpingConfig) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Heure d'ouverture (epoch serveur, s) de chaque barre M1, tendance EMA rapide / lente et ATR M1."""
    close = bars["close"].astype("float64")
    fast = close.ewm(span=config.breakout.ema_fast, adjust=False).mean().to_numpy()
    slow = close.ewm(span=config.breakout.ema_slow, adjust=False).mean().to_numpy()
    trend = np.sign(fast - slow).astype(int)
    trend[: config.breakout.ema_slow - 1] = 0  # pas assez de barres
    atr = atr_series(bars["high"].astype("float64"), bars["low"].astype("float64"), close,
                     config.pullback.atr_period).fillna(0.0).to_numpy()  # fmt: skip
    return bars["time_server"].to_numpy(dtype="int64"), trend, atr


def run_backtest(
    ticks: pd.DataFrame,
    bars: pd.DataFrame,
    config: ScalpingConfig,
    *,
    instrument: Instrument,
    schedule: MarketSchedule,
    initial_equity: float,
    slippage_points: float,
    strategies: tuple[str, ...] | None = None,
    commission_per_lot_side: float = 0.0,
) -> ScalpResult:
    """Rejoue les ticks (colonnes time_msc_server, bid, ask) ; montants dans la devise de cotation (USD).

    strategies : codes des stratégies à rejouer (par défaut, celles actives dans la config).
    """
    server = server_index(ticks["time_msc_server"].to_numpy() // 1000)
    open_mask = schedule.open_mask(server)
    times = ticks["time_msc_server"].to_numpy(dtype="int64")[open_mask]
    bids = ticks["bid"].to_numpy(dtype="float64")[open_mask]
    asks = ticks["ask"].to_numpy(dtype="float64")[open_mask]
    bar_times, trends, atrs = context_by_minute(bars, config)

    point = instrument.point
    slip = slippage_points * point
    fee = 2 * commission_per_lot_side / instrument.contract_size  # commission aller-retour, par once
    size_ms = config.candle_seconds * 1000
    builder, detectors, policy = (
        CandleBuilder(config.candle_seconds),
        make_detectors(config, strategies),
        EntryPolicy(config),
    )
    # Apprentissage à chaque trade (même code qu'en direct) : variantes simulées au prix exécutable.
    book = LearningBook(config, point=point, digits=2, fee_per_oz=fee) if config.learning.enabled else None
    equity = low = day_start_equity = initial_equity
    open_trades: list[tuple[SimTrade, Exposure, str, int, str]] = []  # (trade, exposition, stratégie, ordres, variante)
    closed: list[dict[str, object]] = []
    refusals: Counter = Counter()
    signals: Counter = Counter()
    per_minute: Counter = Counter()
    day, day_pnl = None, 0.0

    for i in range(len(times)):
        now, bid, ask = int(times[i]), float(bids[i]), float(asks[i])
        today = now // 86_400_000
        if today != day:
            day, day_pnl, day_start_equity = today, 0.0, equity
        # 1. Positions ouvertes : stop, objectif, durée maximale.
        if book is not None and book.sims:
            book.on_tick(now, bid, ask)
        if open_trades:
            still = []
            for trade, exposure, strategy, parts, variant in open_trades:
                if trade.on_tick(now, bid, ask):
                    pnl = trade.move * trade.volume * instrument.contract_size
                    level = policy.cadence.level  # palier au moment de la sortie (avant sa mise à jour)
                    policy.cadence.on_close(pnl)
                    policy.on_exit(trade.side, trade.reason, trade.open_ms, trade.exit_ms, pnl)
                    equity += pnl
                    day_pnl += pnl
                    low = min(low, equity)
                    closed.append(
                        {
                            "strategy": strategy,
                            "variant": variant,
                            "tag": trade.tag,
                            "key": exposure.key,
                            "side": trade.side,
                            "open_ms": trade.open_ms,
                            "exit_ms": trade.exit_ms,
                            "entry": trade.entry,
                            "sl": trade.sl,
                            "tp": trade.tp,
                            "exit": trade.exit_price,
                            "reason": trade.reason,
                            "lots": trade.volume,
                            "parts": parts,
                            "risk": exposure.risk,
                            "fees": trade.fee * trade.volume * instrument.contract_size,
                            "pnl": pnl,
                            "palier": level,
                        }
                    )
                else:
                    still.append((trade, exposure, strategy, parts, variant))
            open_trades = still
        # 2. Nouvelle bougie terminée : signaux éventuels, décidés au prix de ce tick.
        for candle in builder.add(now, bid, ask):
            bar = int(np.searchsorted(bar_times, (candle.start_ms + size_ms) // 1000 - 60, side="right")) - 1
            trend = int(trends[bar]) if bar >= 0 else 0
            atr = float(atrs[bar]) if bar >= 0 else 0.0
            for detector in detectors:
                setup = detector.on_candle(candle, trend, atr)
                if setup is None:
                    continue
                code = setup.strategy
                signals[code] += 1
                if now > candle.start_ms + 2 * size_ms:
                    refusals[(code, "signal périmé")] += 1
                    continue
                if not schedule.is_open(_EPOCH + timedelta(milliseconds=now + config.max_hold_s * 1000)):
                    refusals[(code, "le marché ferme avant la durée max")] += 1
                    continue
                plan = plan_trade(
                    setup,
                    bid,
                    ask,
                    config,
                    point=point,
                    stops_level_points=instrument.stops_level_points,
                    freeze_level_points=instrument.freeze_level_points,
                    commission_per_oz=fee,
                )
                if isinstance(plan, str):
                    refusals[(code, reason_key(plan))] += 1
                    continue
                variant = ""
                if book is not None:
                    chosen, variant, _ = book.decide(setup, plan, bid, ask, now)
                    if chosen is None:
                        refusals[(code, "apprentissage : aucune variante gagnante en ce moment")] += 1
                        continue
                    plan = chosen  # sortie et sens choisis d'après les derniers résultats
                # Comme en direct : perte au stop avec le glissement attendu, commission comprise.
                worst_move = plan.stop_distance + config.expected_slippage_points * point + fee
                loss_per_lot = worst_move * instrument.contract_size
                lots = position_size(
                    equity * config.risk_per_trade_pct / 100,
                    loss_per_lot,
                    volume_min=instrument.volume_min,
                    volume_max=float("inf"),  # au-delà du maximum par ordre : fractionnement
                    volume_step=instrument.volume_step,
                )
                if lots == 0:
                    refusals[(code, "lot minimum au-dessus du budget")] += 1
                    continue
                risk = lots * loss_per_lot
                reason = policy.refusal(
                    now,
                    key=setup.key,
                    risk=risk,
                    equity=equity,
                    day_result=day_pnl,
                    day_start_equity=day_start_equity,
                    open_trades=[exposure for _, exposure, _, _, _ in open_trades],
                    side=plan.side,
                )
                if reason is not None:
                    refusals[(code, reason_key(reason))] += 1
                    continue
                policy.accept(now)
                parts = len(split_volume(lots, instrument.volume_max, instrument.volume_step))
                trade = SimTrade.open(setup.tag, plan, bid, ask, now, config.max_hold_s, slip, volume=lots, fee=fee)
                open_trades.append((trade, Exposure(setup.tag, setup.key, plan.side, risk), code, parts, variant))
                per_minute[now // 60_000] += 1

    frame = pd.DataFrame(closed)
    return ScalpResult(frame, refusals, signals, initial_equity, equity, low, Counter(per_minute.values()))


def summary(result: ScalpResult, days: float, strategy: str | None = None) -> dict[str, float]:
    """Mesures d'un rejeu (toutes stratégies, ou une seule), montants en devise de cotation."""
    trades = result.trades
    if not trades.empty and strategy is not None:
        trades = trades[trades["strategy"] == strategy]
    if trades.empty:
        return {"trades": 0}
    trades = trades.sort_values("exit_ms")
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] <= 0]
    path = np.concatenate([[result.initial_equity], result.initial_equity + trades["pnl"].cumsum().to_numpy()])
    drawdown = (path / np.maximum.accumulate(path) - 1).min() * 100
    gross_loss = -losses["pnl"].sum()
    pnl = trades["pnl"].to_numpy()
    error = pnl.std(ddof=1) / np.sqrt(len(pnl)) if len(pnl) > 1 else float("nan")
    by_day = trades.groupby(trades["open_ms"] // 86_400_000)["pnl"].sum()
    in_r = trades["pnl"] / trades["risk"]
    return {
        "trades": len(trades),
        "par_jour": len(trades) / days,
        "gagnants_pct": len(wins) / len(trades) * 100,
        "gain_moyen": wins["pnl"].mean() if len(wins) else 0.0,
        "perte_moyenne": losses["pnl"].mean() if len(losses) else 0.0,
        "gains": wins["pnl"].sum(),
        "pertes": losses["pnl"].sum(),
        "frais": trades["fees"].sum(),
        "profit_factor": wins["pnl"].sum() / gross_loss if gross_loss > 0 else float("inf"),
        "net": pnl.sum(),
        "esperance_r": in_r.mean(),
        "t_stat": pnl.mean() / error if error and error > 0 else float("nan"),
        "drawdown_pct": drawdown,
        "jours": len(by_day),
        "jours_positifs": int((by_day > 0).sum()),
        "duree_moyenne_s": ((trades["exit_ms"] - trades["open_ms"]) / 1000).mean(),
        "stop": int((trades["reason"] == "stop").sum()),
        "objectif": int((trades["reason"] == "objectif").sum()),
        "duree_max": int((trades["reason"] == "durée max").sum()),
        "fractionnes": int((trades["parts"] > 1).sum()),
    }
