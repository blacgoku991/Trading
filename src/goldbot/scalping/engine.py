"""Moteur de l'expérience de scalping : bougies de 5 s, cassure confirmée, plan de trade, simulation au tick.

Le même code sert au test sur l'historique de ticks et au bot en direct (démo). Tout est en flux :
on lui donne les ticks un par un, dans l'ordre, et il ne regarde jamais le futur.

- Bougies : prix médian (bid + ask) / 2, pour qu'un simple élargissement du spread ne ressemble pas
  à une cassure ; seulement pendant les heures de cotation (ticks filtrés par l'appelant).
- Signal : clôture au-delà du plus haut (ou plus bas) des 60 s précédentes, bougie de signal exclue,
  puis une deuxième clôture au-delà du même niveau ; tendance M1 (EMA20/EMA50 sur barres clôturées)
  dans le même sens. Après un signal, le côté est désarmé jusqu'à ce qu'une clôture revienne dans le
  range des 60 s précédentes (pause du mouvement) : la cassure suivante est alors un nouveau setup.
- Plan : entrée au prix réellement disponible (ask à l'achat, bid à la vente) ; stop derrière la
  structure des 30 dernières secondes, en respectant les distances minimales du broker ; objectif
  à 1,2 fois la distance du stop ; refus si l'objectif est trop petit face au coût estimé
  (spread + glissements + commission aller-retour).
- Simulation : stop et objectif testés tick par tick (bid pour un achat, ask pour une vente), sortie
  forcée à la durée maximale, même en perte. Le spread n'est compté qu'une fois : il est dans les prix ;
  la commission, elle, est retirée du résultat.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from goldbot.config import ScalpingConfig

LONG, SHORT = 1, -1


@dataclass(frozen=True)
class Candle:
    start_ms: int  # début (epoch serveur en ms), multiple de la durée des bougies
    open: float  # prix médians
    high: float
    low: float
    close: float
    bid: float  # dernière cotation de la bougie
    ask: float
    max_spread: float
    ticks: int


class CandleBuilder:
    """Construit les bougies à partir des ticks ; une bougie est rendue quand elle est terminée."""

    def __init__(self, seconds: int) -> None:
        self.size_ms = seconds * 1000
        self._start: int | None = None
        self._closed_until = -(2**62)  # fin de la dernière bougie rendue : un tick plus ancien arrive trop tard
        self._o = self._h = self._l = self._c = self._bid = self._ask = self._spread = 0.0
        self._n = 0

    def _finish(self) -> Candle:
        candle = Candle(
            self._start, self._o, self._h, self._l, self._c, self._bid, self._ask, self._spread, self._n
        )  # fmt: skip
        self._closed_until = self._start + self.size_ms
        self._start = None
        return candle

    def add(self, time_msc: int, bid: float, ask: float) -> list[Candle]:
        """Ajoute un tick ; renvoie la bougie précédente si ce tick en commence une nouvelle."""
        if time_msc < self._closed_until:
            return []  # tick arrivé après la clôture de sa bougie (en direct) : ignoré
        start = time_msc // self.size_ms * self.size_ms
        done = []
        if self._start is not None and start != self._start:
            done.append(self._finish())
        mid = (bid + ask) / 2.0
        if self._start is None:
            self._start = start
            self._o = self._h = self._l = mid
            self._spread = 0.0
            self._n = 0
        self._h, self._l, self._c = max(self._h, mid), min(self._l, mid), mid
        self._bid, self._ask = bid, ask
        self._spread = max(self._spread, ask - bid)
        self._n += 1
        return done

    def close_until(self, now_ms: int) -> list[Candle]:
        """Termine la bougie en cours si son temps est écoulé (aucun tick ne peut plus y entrer)."""
        if self._start is not None and now_ms >= self._start + self.size_ms:
            return [self._finish()]
        return []

    @property
    def pending_start(self) -> int | None:
        return self._start


@dataclass(frozen=True)
class Setup:
    side: int
    level: float  # niveau cassé (plus haut ou plus bas des 60 s précédentes)
    candle: Candle  # bougie de confirmation
    tag: str  # identifiant unique du signal


class BreakoutDetector:
    def __init__(self, config: ScalpingConfig) -> None:
        self.lookback_ms = config.breakout_lookback_s * 1000
        self.min_window = config.min_window_candles
        self.confirm = config.confirm_closes
        self.history: deque[Candle] = deque()
        self._pending: dict[int, tuple[float, int]] = {}  # côté -> (niveau, clôtures au-delà)
        self._disarmed: dict[int, float] = {}  # côté -> niveau à repasser pour réarmer

    def on_candle(self, candle: Candle, trend: int) -> Setup | None:
        window = [c for c in self.history if c.start_ms >= candle.start_ms - self.lookback_ms]
        levels = {}
        if len(window) >= self.min_window:
            levels = {LONG: max(c.high for c in window), SHORT: min(c.low for c in window)}
        setup = None
        for side in (LONG, SHORT):
            level_now = levels.get(side)
            beyond_now = level_now is not None and (candle.close - level_now) * side > 0
            # Réarmement : une clôture revenue dans le range des 60 s précédentes (le mouvement a marqué
            # une pause) ; la cassure suivante sera un nouveau setup.
            if side in self._disarmed and level_now is not None and not beyond_now:
                del self._disarmed[side]
            pending = self._pending.pop(side, None)
            if pending is not None:
                level, count = pending
                if (candle.close - level) * side > 0 and trend == side:
                    count += 1
                    if count >= self.confirm:
                        self._disarmed[side] = level
                        setup = setup or self._setup(side, level, candle)
                    else:
                        self._pending[side] = (level, count)
                # sinon : retour dans le range ou tendance retournée, pas de confirmation
            elif side not in self._disarmed and beyond_now and trend == side:
                if self.confirm <= 1:
                    self._disarmed[side] = level_now
                    setup = setup or self._setup(side, level_now, candle)
                else:
                    self._pending[side] = (level_now, 1)
        self.history.append(candle)
        while self.history and self.history[0].start_ms < candle.start_ms - self.lookback_ms:
            self.history.popleft()
        return setup

    @staticmethod
    def _setup(side: int, level: float, candle: Candle) -> Setup:
        return Setup(side, level, candle, f"SC-{candle.start_ms}-{'L' if side == LONG else 'S'}")


@dataclass(frozen=True)
class Plan:
    side: int
    entry: float  # prix disponible au moment de la décision (ask à l'achat, bid à la vente)
    sl: float
    tp: float
    stop_distance: float
    target: float  # gain par once si l'objectif est touché (spread déjà inclus)
    cost: float  # spread + glissements + commission aller-retour estimés, par once
    spread: float


def plan_trade(
    setup: Setup,
    bid: float,
    ask: float,
    recent: list[Candle],
    config: ScalpingConfig,
    *,
    point: float,
    stops_level_points: int,
    freeze_level_points: int,
    digits: int = 2,
    commission_per_oz: float = 0.0,
) -> Plan | str:
    """Plan de trade, ou le motif du refus. commission_per_oz : commission aller-retour, en prix par once."""
    side = setup.side
    spread = ask - bid
    entry = ask if side == LONG else bid
    cutoff = setup.candle.start_ms - config.stop_lookback_s * 1000
    structure = [c for c in recent if c.start_ms >= cutoff] or [setup.candle]
    buffer = config.stop_buffer_points * point
    # La structure est en prix médian : le stop d'un achat se déclenche au bid (médian - spread / 2).
    if side == LONG:
        sl = min(c.low for c in structure) - spread / 2 - buffer
    else:
        sl = max(c.high for c in structure) + spread / 2 + buffer
    sl = round(sl, digits)
    distance = round((entry - sl) * side, digits)
    minimum = max(config.min_stop_points * point, (stops_level_points + freeze_level_points) * point + spread)
    if distance < minimum:
        return f"stop trop proche : {distance:.2f} $ < {minimum:.2f} $"
    if distance > config.max_stop_points * point:
        return f"stop trop loin : {distance:.2f} $ > {config.max_stop_points * point:.2f} $"
    target = round(config.target_ratio * distance, digits)
    cost = spread + 2 * config.expected_slippage_points * point + commission_per_oz
    if target < config.min_target_cost_ratio * cost:
        return f"objectif trop faible face au coût : {target:.2f} $ < {config.min_target_cost_ratio:g} x {cost:.2f} $"
    tp = round(entry + side * target, digits)
    return Plan(side, entry, sl, tp, distance, target, cost, spread)


@dataclass
class SimTrade:
    """Trade simulé au tick : entrée avec glissement, puis stop, objectif ou durée maximale."""

    tag: str
    side: int
    entry: float
    sl: float
    tp: float
    open_ms: int
    deadline_ms: int
    slip: float  # glissement défavorable (prix) sur l'entrée, le stop et la sortie forcée
    volume: float = 0.0
    fee: float = 0.0  # commission aller-retour, en prix par once
    exit_price: float | None = None
    exit_ms: int | None = None
    reason: str | None = None

    @classmethod
    def open(cls, tag: str, plan: Plan, bid: float, ask: float, time_ms: int, max_hold_s: int, slip: float,
             volume: float = 0.0, fee: float = 0.0) -> SimTrade:  # fmt: skip
        entry = ask + slip if plan.side == LONG else bid - slip
        return cls(tag, plan.side, entry, plan.sl, plan.tp, time_ms, time_ms + max_hold_s * 1000, slip, volume, fee)

    def on_tick(self, time_ms: int, bid: float, ask: float) -> bool:
        """Met à jour le trade ; True s'il vient d'être fermé."""
        if self.exit_price is not None:
            return False
        if self.side == LONG:
            if bid <= self.sl:
                return self._close(time_ms, min(self.sl, bid) - self.slip, "stop")
            if bid >= self.tp:
                return self._close(time_ms, self.tp, "objectif")
            if time_ms >= self.deadline_ms:
                return self._close(time_ms, bid - self.slip, "durée max")
        else:
            if ask >= self.sl:
                return self._close(time_ms, max(self.sl, ask) + self.slip, "stop")
            if ask <= self.tp:
                return self._close(time_ms, self.tp, "objectif")
            if time_ms >= self.deadline_ms:
                return self._close(time_ms, ask + self.slip, "durée max")
        return False

    def _close(self, time_ms: int, price: float, reason: str) -> bool:
        self.exit_price, self.exit_ms, self.reason = price, time_ms, reason
        return True

    @property
    def move(self) -> float:
        """Résultat par once (USD), spread, glissement et commission inclus."""
        return 0.0 if self.exit_price is None else (self.exit_price - self.entry) * self.side - self.fee

    def move_at(self, bid: float, ask: float) -> float:
        """Résultat latent par once si le trade était fermé à ces prix (avec glissement et commission)."""
        exit_price = bid - self.slip if self.side == LONG else ask + self.slip
        return (exit_price - self.entry) * self.side - self.fee


def ema_trend(closes: list[float] | tuple[float, ...], fast: int, slow: int) -> int:
    """+1 si EMA rapide > EMA lente, -1 si inférieure, 0 si pas assez de barres."""
    if len(closes) < slow:
        return 0
    alpha_fast, alpha_slow = 2.0 / (fast + 1), 2.0 / (slow + 1)
    ema_f = ema_s = closes[0]
    for value in closes[1:]:
        ema_f += alpha_fast * (value - ema_f)
        ema_s += alpha_slow * (value - ema_s)
    return 1 if ema_f > ema_s else -1 if ema_f < ema_s else 0
