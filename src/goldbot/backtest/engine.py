"""Moteur de backtest événementiel sur barres M1 (CLAUDE.md §9).

Hypothèses, toutes du côté prudent :
- les prix des barres MT5 sont des bid ; ask = bid + spread de la barre (x multiplicateur) ;
- un signal naît à la clôture d'une barre et s'exécute à la barre suivante ;
- achat à l'ask, vente au bid ; SL d'un long déclenché par le bid, SL d'un short par l'ask ;
- SL et TP touchés dans la même barre : SL ; ouverture au-delà du SL (gap) : sortie à l'ouverture ;
- glissement défavorable sur les entrées et sur les sorties au marché ou au SL, aucun sur les TP ;
- ordre stop déclenché dans la barre : le SL est testé sur toute la barre (pire cas) ;
- les limites de risque (risk/limits.py) sont celles du live.
Le compte simulé est tenu dans la devise de cotation (USD) : les résultats en % et en R ne
dépendent pas de la devise du compte.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from goldbot.config import RiskConfig
from goldbot.indicators.sessions import NEW_YORK, next_session_start
from goldbot.risk.limits import MAX_DRAWDOWN, RiskLimits
from goldbot.risk.sizing import position_size
from goldbot.strategies.base import LONG, MARKET, OrderIntent

# Ouvertures de session pour la pause après une série de pertes (fuseau, heure locale).
SESSIONS = (("Asia/Tokyo", time(9, 0)), ("Europe/London", time(8, 0)), (NEW_YORK, time(8, 0)))
_MINUTE = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class Instrument:
    point: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int = 0

    @classmethod
    def from_symbol(cls, symbol: dict) -> Instrument:
        """Depuis la section symbol de manifest.json (export MT5)."""
        return cls(
            point=float(symbol["point"]),
            contract_size=float(symbol["trade_contract_size"]),
            volume_min=float(symbol["volume_min"]),
            volume_max=float(symbol["volume_max"]),
            volume_step=float(symbol["volume_step"]),
            stops_level_points=int(symbol.get("trade_stops_level", 0)),
        )


@dataclass(frozen=True)
class Costs:
    spread_multiplier: float = 1.0  # appliqué au spread de chaque barre
    min_spread_points: float = 0.0  # plancher (spreads nuls de 2019 dans l'historique Axi)
    slippage_points: float = 0.0  # glissement défavorable par exécution au marché ou au SL
    commission_per_lot_side: float = 0.0  # en devise du compte, par lot et par côté
    swap_long_points: float = 0.0  # par lot et par nuit (mode « points » de MT5)
    swap_short_points: float = 0.0
    triple_swap_weekday: int = 2  # lundi = 0 : nuit du mercredi facturée 3 fois


@dataclass
class _Position:
    intent: OrderIntent
    entry_index: int
    entry_time: pd.Timestamp
    entry_price: float
    lots: float
    risk_money: float  # perte au SL avec ce volume (commissions et glissement inclus)
    exit_index: int  # sortie horaire à l'ouverture de cette barre (n = jamais)
    spread_points: float
    worst: float  # prix le plus défavorable atteint (bid pour un long, ask pour un short)
    best: float
    commission: float = 0.0
    swap: float = 0.0


@dataclass(frozen=True)
class Trade:
    strategy_tag: str
    side: int
    reason: str
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    sl: float
    tp: float | None
    lots: float
    pnl: float  # net : prix, commissions et swaps
    commission: float
    swap: float
    r_multiple: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    spread_points: float


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # equity (flottant inclus) aux instants où elle change, index UTC
    daily_equity: pd.Series  # equity en fin de jour de cotation
    skipped: pd.DataFrame  # intentions non exécutées et pourquoi
    initial_equity: float
    halted: bool = False  # arrêt total au drawdown maximal (mode compte réel)
    halt_time: pd.Timestamp | None = None  # mode recherche : date où le compte aurait été arrêté
    notes: list[str] = field(default_factory=list)


class Backtest:
    def __init__(
        self,
        bars: pd.DataFrame,
        *,
        instrument: Instrument,
        costs: Costs,
        risk: RiskConfig,
        initial_equity: float,
        halt_on_drawdown: bool = True,
    ) -> None:
        """halt_on_drawdown=False : mode recherche, l'arrêt total au drawdown maximal est seulement noté.

        Utile pour mesurer une stratégie sur tout l'historique ; les autres limites restent actives.
        """
        self.halt_on_drawdown = halt_on_drawdown
        self.bars = bars.reset_index(drop=True)
        self.instrument = instrument
        self.costs = costs
        self.risk = risk
        self.initial_equity = initial_equity
        self.times = pd.DatetimeIndex(self.bars["time"])
        self.o = self.bars["open"].to_numpy(dtype="float64")
        self.h = self.bars["high"].to_numpy(dtype="float64")
        self.l = self.bars["low"].to_numpy(dtype="float64")
        self.c = self.bars["close"].to_numpy(dtype="float64")
        # Spread d'une barre MT5 = le plus petit de la minute (docs/RESEARCH.md annexe E) : multiplicateur et plancher.
        spread_points = self.bars["spread"].to_numpy(dtype="float64") * costs.spread_multiplier
        self.spread = np.maximum(spread_points, costs.min_spread_points) * instrument.point
        self.day = self.bars["time_server"].to_numpy() // 86_400
        self.slip = costs.slippage_points * instrument.point

    # --- préparation --------------------------------------------------------------------------

    def _schedule(self, intents: list[OrderIntent]) -> dict[int, list[tuple[OrderIntent, int, int]]]:
        """Intention -> (barre d'activation, barre d'expiration, barre de sortie horaire)."""
        stamps = self.times.as_unit("ns").asi8  # Timestamp.value est toujours en nanosecondes
        scheduled: dict[int, list[tuple[OrderIntent, int, int]]] = {}
        for intent in intents:
            signal = int(np.searchsorted(stamps, intent.time.value))
            if signal >= len(stamps) or stamps[signal] != intent.time.value:
                raise ValueError(f"intention {intent.tag} datée d'une barre absente ({intent.time})")
            expiry = int(np.searchsorted(stamps, intent.expires_at.value))
            exit_index = len(stamps) if intent.exit_at is None else int(np.searchsorted(stamps, intent.exit_at.value))
            scheduled.setdefault(signal + 1, []).append((intent, expiry, exit_index))
        return scheduled

    # --- exécution ----------------------------------------------------------------------------------

    def run(self, intents: list[OrderIntent]) -> BacktestResult:
        n = len(self.bars)
        scheduled = self._schedule(intents)
        self.limits = RiskLimits.start(self.risk, self.initial_equity)
        self.balance = self.initial_equity
        self.equity_now = self.initial_equity
        self.positions: list[_Position] = []
        self.trades: list[Trade] = []
        self.skipped: list[tuple[pd.Timestamp, str, str]] = []
        self.curve: list[tuple[pd.Timestamp, float]] = []
        self.daily: list[tuple[pd.Timestamp, float]] = []
        self.halt_time: pd.Timestamp | None = None
        pending: list[tuple[OrderIntent, int, int]] = []
        current_day = None

        for i in range(n):
            if self.day[i] != current_day:
                if current_day is not None:
                    self.daily.append((self.times[i - 1], self.equity_now))
                    self._charge_swaps(int(current_day), int(self.day[i]))
                current_day = self.day[i]
                self.limits.new_day(int(current_day), self.equity_now)
            new = scheduled.get(i)
            if not (new or pending or self.positions):
                continue

            self._exits_at_open(i)
            for item in new or ():
                intent, _, exit_index = item
                if intent.kind == MARKET:
                    self._market_entry(i, intent, exit_index)
                else:
                    pending.append(item)
            pending = self._stop_orders(i, pending)
            self._exits_in_bar(i)
            if self.positions:
                self._end_of_bar(i)
                if self.limits.halted or self.limits.stopped_day == self.day[i]:
                    for intent, _, _ in pending:
                        self.skipped.append((self.times[i], intent.tag, "ordre annulé : limite de risque atteinte"))
                    pending = []
            if self.limits.halted and not self.positions:
                break

        if self.positions:
            last = n - 1
            for position in list(self.positions):
                self._close(position, last, self._exit_price(position, last, "close"), "fin des données", end=True)
        if n:
            self.daily.append((self.times[min(i, n - 1)], self.equity_now))
        return self._result()

    def _exit_price(self, position: _Position, i: int, at: str) -> float:
        """Sortie au marché à l'ouverture ou à la clôture de la barre, glissement défavorable compris."""
        bid = self.o[i] if at == "open" else self.c[i]
        if position.intent.side == LONG:
            return bid - self.slip
        return bid + self.spread[i] + self.slip

    def _exits_at_open(self, i: int) -> None:
        for position in list(self.positions):
            intent = position.intent
            if position.exit_index == i:
                self._close(position, i, self._exit_price(position, i, "open"), "sortie horaire")
                continue
            if intent.side == LONG:
                opening = self.o[i]
                if opening <= intent.sl:
                    self._close(position, i, opening - self.slip, "SL (gap)")
                elif intent.tp is not None and opening >= intent.tp:
                    self._close(position, i, intent.tp, "TP")
            else:
                opening = self.o[i] + self.spread[i]
                if opening >= intent.sl:
                    self._close(position, i, opening + self.slip, "SL (gap)")
                elif intent.tp is not None and opening <= intent.tp:
                    self._close(position, i, intent.tp, "TP")

    def _market_entry(self, i: int, intent: OrderIntent, exit_index: int) -> None:
        fill = self.o[i] + self.spread[i] + self.slip if intent.side == LONG else self.o[i] - self.slip
        self._enter(i, intent, fill, exit_index)

    def _stop_orders(self, i: int, pending: list[tuple[OrderIntent, int, int]]) -> list[tuple[OrderIntent, int, int]]:
        still = []
        for item in pending:
            intent, expiry, exit_index = item
            if i >= expiry or i >= exit_index:
                continue  # expiré sans déclenchement
            if intent.side == LONG:
                triggered = self.h[i] + self.spread[i] >= intent.price
                fill = max(intent.price, self.o[i] + self.spread[i]) + self.slip
            else:
                triggered = self.l[i] <= intent.price
                fill = min(intent.price, self.o[i]) - self.slip
            if triggered:
                self._enter(i, intent, fill, exit_index)
            else:
                still.append(item)
        return still

    def _enter(self, i: int, intent: OrderIntent, fill: float, exit_index: int) -> None:
        when = self.times[i]
        block = self.limits.entry_block(when, len(self.positions))
        distance = (fill - intent.sl) * intent.side
        min_distance = self.instrument.stops_level_points * self.instrument.point
        if block is None and distance <= min_distance:
            block = "SL trop proche du prix d'entrée (ou dépassé)"
        if block is None and intent.tp is not None and (intent.tp - fill) * intent.side <= min_distance:
            block = "TP déjà atteint ou trop proche à l'entrée"
        lots = 0.0
        if block is None:
            loss_per_lot = (distance + self.slip) * self.instrument.contract_size
            loss_per_lot += 2 * self.costs.commission_per_lot_side
            lots = position_size(
                self.limits.risk_money(self.equity_now),
                loss_per_lot,
                volume_min=self.instrument.volume_min,
                volume_max=self.instrument.volume_max,
                volume_step=self.instrument.volume_step,
            )
            if lots == 0:
                block = "volume sous le minimum du broker pour ce risque"
        if block is not None:
            self.skipped.append((when, intent.tag, block))
            return
        commission = -self.costs.commission_per_lot_side * lots
        self.balance += commission
        risk_money = lots * (loss_per_lot - 2 * self.costs.commission_per_lot_side) + 2 * abs(commission)
        self.positions.append(
            _Position(
                intent=intent,
                entry_index=i,
                entry_time=when,
                entry_price=fill,
                lots=lots,
                risk_money=risk_money,
                exit_index=exit_index,
                spread_points=self.spread[i] / self.instrument.point,
                worst=fill,
                best=fill,
                commission=commission,
            )
        )
        self.limits.on_entry()

    def _exits_in_bar(self, i: int) -> None:
        for position in list(self.positions):
            intent = position.intent
            if intent.side == LONG:
                low, high = self.l[i], self.h[i]
                position.worst, position.best = min(position.worst, low), max(position.best, high)
                if low <= intent.sl:
                    self._close(position, i, intent.sl - self.slip, "SL")
                elif intent.tp is not None and high >= intent.tp:
                    self._close(position, i, intent.tp, "TP")
            else:
                low, high = self.l[i] + self.spread[i], self.h[i] + self.spread[i]
                position.worst, position.best = max(position.worst, high), min(position.best, low)
                if high >= intent.sl:
                    self._close(position, i, intent.sl + self.slip, "SL")
                elif intent.tp is not None and low <= intent.tp:
                    self._close(position, i, intent.tp, "TP")

    def _floating(self, i: int) -> float:
        total = 0.0
        size = self.instrument.contract_size
        for position in self.positions:
            if position.intent.side == LONG:
                total += (self.c[i] - position.entry_price) * position.lots * size
            else:
                total += (position.entry_price - self.c[i] - self.spread[i]) * position.lots * size
        return total

    def _end_of_bar(self, i: int) -> None:
        self.equity_now = self.balance + self._floating(i)
        self.curve.append((self.times[i] + _MINUTE, self.equity_now))
        self._apply_equity_limits(i)

    def _apply_equity_limits(self, i: int) -> None:
        crossed = self.limits.on_equity(self.equity_now)
        if MAX_DRAWDOWN in crossed and not self.halt_on_drawdown:
            # Mode recherche : on note la date où le compte aurait été arrêté, puis on continue.
            crossed.remove(MAX_DRAWDOWN)
            self.limits.halted = False
            self.limits.peak_equity = self.equity_now
            if self.halt_time is None:
                self.halt_time = self.times[i]
        if crossed:
            for position in list(self.positions):
                self._close(position, i, self._exit_price(position, i, "close"), crossed[0], end=True)

    def _close(self, position: _Position, i: int, price: float, reason: str, *, end: bool = False) -> None:
        # Une limite de risque franchie pendant une boucle de sorties peut déjà avoir fermé cette position.
        index = next((k for k, open_ in enumerate(self.positions) if open_ is position), None)
        if index is None:
            return
        del self.positions[index]
        intent = position.intent
        size = self.instrument.contract_size
        commission = -self.costs.commission_per_lot_side * position.lots
        gross = (price - position.entry_price) * intent.side * position.lots * size
        pnl = gross + position.commission + commission + position.swap
        self.balance += gross + commission
        when = self.times[i] + (_MINUTE if end else pd.Timedelta(0))
        distance = abs(position.entry_price - intent.sl)
        self.trades.append(
            Trade(
                strategy_tag=intent.tag,
                side=intent.side,
                reason=intent.reason,
                signal_time=intent.time,
                entry_time=position.entry_time,
                exit_time=when,
                entry_price=position.entry_price,
                exit_price=price,
                sl=intent.sl,
                tp=intent.tp,
                lots=position.lots,
                pnl=pnl,
                commission=position.commission + commission,
                swap=position.swap,
                r_multiple=pnl / position.risk_money,
                mae_r=max(0.0, (position.entry_price - position.worst) * intent.side / distance),
                mfe_r=max(0.0, (position.best - position.entry_price) * intent.side / distance),
                exit_reason=reason,
                spread_points=position.spread_points,
            )
        )
        self.equity_now = self.balance + self._floating(i)
        self.curve.append((when, self.equity_now))
        self.limits.on_exit(pnl, when, lambda now: next_session_start(now, SESSIONS))
        self._apply_equity_limits(i)

    def _charge_swaps(self, previous_day: int, new_day: int) -> None:
        """Swaps des positions qui passent la nuit : un par fin de jour ouvré (lundi = 0), triple le jour dit."""
        if not self.positions:
            return
        value = self.instrument.point * self.instrument.contract_size
        for day in range(previous_day, new_day):
            weekday = (day + 3) % 7  # le 1er janvier 1970 était un jeudi
            if weekday > 4:
                continue
            nights = 3 if weekday == self.costs.triple_swap_weekday else 1
            for position in self.positions:
                points = self.costs.swap_long_points if position.intent.side == LONG else self.costs.swap_short_points
                charge = points * value * position.lots * nights
                position.swap += charge
                self.balance += charge

    def _result(self) -> BacktestResult:
        columns = [f.name for f in Trade.__dataclass_fields__.values()]
        trades = pd.DataFrame([asdict(t) for t in self.trades], columns=columns)
        equity = pd.Series(
            [value for _, value in self.curve], index=pd.DatetimeIndex([t for t, _ in self.curve]), dtype="float64"
        )
        daily = pd.Series(
            [value for _, value in self.daily], index=pd.DatetimeIndex([t for t, _ in self.daily]), dtype="float64"
        )
        skipped = pd.DataFrame(self.skipped, columns=["time", "tag", "reason"])
        return BacktestResult(
            trades=trades,
            equity=equity,
            daily_equity=daily,
            skipped=skipped,
            initial_equity=self.initial_equity,
            halted=self.limits.halted,
            halt_time=self.halt_time,
        )
