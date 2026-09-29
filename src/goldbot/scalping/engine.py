"""Moteur de l'expérience de scalping : bougies de 5 s, détecteurs de signaux, plan de trade, simulation au tick.

Le même code sert au rejeu des ticks et au bot en direct (démo). Tout est en flux : on lui donne les
ticks un par un, dans l'ordre, et il ne regarde jamais le futur.

- Bougies : prix médian (bid + ask) / 2, pour qu'un simple élargissement du spread ne ressemble pas
  à un mouvement ; seulement pendant les heures de cotation (ticks filtrés par l'appelant).
- Deux stratégies, chacune identifiée par un code, une version et l'empreinte de ses réglages :
  - « cassure » (B) : clôture au-delà du plus haut (ou plus bas) des 60 s précédentes, bougie de signal
    exclue, puis une deuxième clôture au-delà du même niveau ; tendance M1 (EMA20 / EMA50 sur barres
    clôturées) dans le même sens. Le côté est réarmé quand une clôture revient dans le range.
  - « impulsion-repli » (P) : impulsion d'au moins 1 ATR M1 en 60 s au plus, repli de 30 à 70 % de
    l'impulsion, puis reprise (clôture au-delà du plus haut de la bougie précédente) dans les 60 s qui
    suivent le sommet de l'impulsion. Une seule entrée par impulsion.
- Plan : entrée au prix réellement disponible (ask à l'achat, bid à la vente) ; stop derrière la
  structure du signal, en respectant les distances minimales du broker ; objectif à 1,2 fois la
  distance du stop ; refus si l'objectif est trop petit face au coût estimé (spread + glissements +
  commission aller-retour).
- Simulation : stop et objectif testés tick par tick (bid pour un achat, ask pour une vente), sortie
  forcée à la durée maximale, même en perte. Le spread n'est compté qu'une fois : il est dans les prix ;
  la commission, elle, est retirée du résultat.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from goldbot.config import ScalpingConfig
from goldbot.indicators.core import trading_days, true_range

LONG, SHORT = 1, -1
BREAKOUT, PULLBACK, TWO_CANDLES = "B", "P", "R"
STRATEGY_NAMES = {BREAKOUT: "cassure", PULLBACK: "impulsion-repli", TWO_CANDLES: "deux bougies"}


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
    strategy: str  # BREAKOUT, PULLBACK ou TWO_CANDLES
    side: int
    level: float  # niveau clé : niveau cassé, ou sommet de l'impulsion
    structure: float  # prix médian derrière lequel va le stop (creux récent pour un achat)
    candle: Candle  # bougie qui déclenche le signal
    key: str  # identifiant de l'occasion : deux signaux de même clé sont la même occasion
    reason: str  # motif d'entrée, en clair
    features: dict[str, float] = field(default_factory=dict)  # contexte chiffré du signal (analyse des erreurs)

    @property
    def tag(self) -> str:
        """Identifiant unique du signal (commentaire des ordres MT5, suivi de #1, #2… par ordre)."""
        return f"SC-{self.strategy}-{self.candle.start_ms}-{'L' if self.side == LONG else 'S'}"


class BreakoutDetector:
    """Cassure des 60 s précédentes, confirmée par une deuxième clôture, dans le sens de la tendance M1."""

    code = BREAKOUT

    def __init__(self, config: ScalpingConfig) -> None:
        cfg = config.breakout
        self.lookback_ms = cfg.lookback_s * 1000
        self.stop_lookback_ms = cfg.stop_lookback_s * 1000
        self.keep_ms = max(self.lookback_ms, self.stop_lookback_ms)  # historique gardé : range et structure du stop
        self.min_window = cfg.min_window_candles
        self.confirm = cfg.confirm_closes
        self.history: deque[Candle] = deque()
        self._pending: dict[int, tuple[float, int]] = {}  # côté -> (niveau, clôtures au-delà)
        self._disarmed: dict[int, float] = {}  # côté -> niveau à repasser pour réarmer

    def on_candle(self, candle: Candle, trend: int, atr: float) -> Setup | None:
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
                        setup = setup or self._setup(side, level, candle, trend, atr, window)
                    else:
                        self._pending[side] = (level, count)
                # sinon : retour dans le range ou tendance retournée, pas de confirmation
            elif side not in self._disarmed and beyond_now and trend == side:
                if self.confirm <= 1:
                    self._disarmed[side] = level_now
                    setup = setup or self._setup(side, level_now, candle, trend, atr, window)
                else:
                    self._pending[side] = (level_now, 1)
        self.history.append(candle)
        while self.history and self.history[0].start_ms < candle.start_ms - self.keep_ms:
            self.history.popleft()
        return setup

    def _setup(self, side: int, level: float, candle: Candle, trend: int, atr: float, window: list[Candle]) -> Setup:
        cutoff = candle.start_ms - self.stop_lookback_ms
        recent = [c for c in self.history if c.start_ms >= cutoff] + [candle]
        structure = min(c.low for c in recent) if side == LONG else max(c.high for c in recent)
        trend_text = "haussière" if trend > 0 else "baissière"
        width = max(c.high for c in window) - min(c.low for c in window) if window else 0.0
        return Setup(
            BREAKOUT,
            side,
            level,
            structure,
            candle,
            key=f"B{side:+d}@{level:.2f}",
            reason=f"cassure de {level:.2f} confirmée (tendance M1 {trend_text})",
            features={
                "break_atr": (candle.close - level) * side / atr if atr > 0 else 0.0,  # clôture au-delà du niveau
                "range_atr": width / atr if atr > 0 else 0.0,  # largeur du range des 60 s cassé
                "trend_aligned": 1.0,
            },
        )


@dataclass
class _Impulse:
    """Impulsion suivie, en coordonnées « achat » (prix changés de signe pour une vente)."""

    start_ms: int  # bougie du point de départ
    low: float  # point de départ
    extreme: float  # sommet atteint
    extreme_ms: int
    pull: float | None = None  # creux du repli depuis le sommet


def _oriented(candle: Candle, side: int) -> tuple[float, float, float]:
    """(plus haut, plus bas, clôture) vus du côté du trade : une vente se lit comme un achat inversé."""
    if side == LONG:
        return candle.high, candle.low, candle.close
    return -candle.low, -candle.high, -candle.close


class PullbackDetector:
    """Impulsion, repli partiel, puis reprise dans le sens de l'impulsion (achat décrit ; vente symétrique).

    1. Impulsion : entre le plus bas des impulse_max_s dernières secondes (bougies closes) et le plus haut
       de la bougie qui vient de clôturer, un mouvement d'au moins impulse_atr x l'ATR M1.
    2. Repli : après le sommet, le creux descend de retrace_min à retrace_max de l'impulsion. Plus profond,
       l'impulsion est abandonnée ; un nouveau sommet avant tout repli suffisant prolonge l'impulsion.
    3. Reprise : une bougie clôture au-dessus du plus haut de la bougie précédente, au plus pullback_max_s
       après le sommet. Stop derrière le creux du repli. Une seule entrée par impulsion ; l'impulsion
       suivante doit partir d'un point situé après le sommet de la précédente.
    """

    code = PULLBACK

    def __init__(self, config: ScalpingConfig) -> None:
        self.cfg = config.pullback
        self.candle_ms = config.candle_seconds * 1000
        self.impulse_ms = self.cfg.impulse_max_s * 1000
        self.pullback_ms = self.cfg.pullback_max_s * 1000
        self.history: deque[Candle] = deque()
        self._state: dict[int, _Impulse] = {}
        self._floor = {LONG: -(2**62), SHORT: -(2**62)}  # départ d'une nouvelle impulsion : après ce moment

    def on_candle(self, candle: Candle, trend: int, atr: float) -> Setup | None:
        previous = self.history[-1] if self.history else None
        setup = None
        for side in (LONG, SHORT):
            found = self._step(side, candle, previous, trend, atr)
            setup = setup or found
        self.history.append(candle)
        while self.history and self.history[0].start_ms < candle.start_ms - self.impulse_ms:
            self.history.popleft()
        return setup

    def _step(self, side: int, candle: Candle, previous: Candle | None, trend: int, atr: float) -> Setup | None:
        high, low, close = _oriented(candle, side)
        state = self._state.get(side)
        if state is None:
            self._find_impulse(side, candle, high, atr)
            return None
        size = state.extreme - state.low
        pull = low if state.pull is None else min(state.pull, low)
        if (state.extreme - pull) / size > self.cfg.retrace_max:
            self._drop(side, state)  # repli trop profond : l'impulsion est cassée
            return None
        if state.pull is not None and previous is not None:
            depth = (state.extreme - state.pull) / size
            if depth >= self.cfg.retrace_min and close > _oriented(previous, side)[0]:
                self._drop(side, state)
                return self._setup(side, state, pull, depth, candle, trend, atr)
        if high > state.extreme:  # nouveau sommet avant un repli suffisant : l'impulsion continue
            state.extreme, state.extreme_ms, state.pull = high, candle.start_ms, None
            return None
        if candle.start_ms - state.extreme_ms > self.pullback_ms:
            self._drop(side, state)  # pas de reprise à temps
            return None
        state.pull = pull
        return None

    def _find_impulse(self, side: int, candle: Candle, high: float, atr: float) -> None:
        if not atr or atr <= 0:
            return
        cutoff = max(candle.start_ms - self.impulse_ms, self._floor[side] + 1)
        window = [c for c in self.history if c.start_ms >= cutoff]
        if not window:
            return
        # Point de départ : le plus bas de la fenêtre ; à égalité, le plus récent (début réel du mouvement).
        low, latest = min((_oriented(c, side)[1], -c.start_ms) for c in window)
        start_ms = -latest
        if high - low >= self.cfg.impulse_atr * atr:
            self._state[side] = _Impulse(start_ms, low, high, candle.start_ms)

    def _drop(self, side: int, state: _Impulse) -> None:
        del self._state[side]
        self._floor[side] = state.extreme_ms

    def _setup(self, side: int, state: _Impulse, pull: float, depth: float, candle: Candle, trend: int,
               atr: float) -> Setup:  # fmt: skip
        size = state.extreme - state.low
        seconds = (state.extreme_ms + self.candle_ms - state.start_ms) / 1000
        return Setup(
            PULLBACK,
            side,
            state.extreme * side,
            pull * side,
            candle,
            key=f"P{side:+d}@{state.start_ms}-{state.extreme_ms}",
            reason=f"impulsion de {size:.2f} $ ({size / atr:.1f} ATR M1) en {seconds:.0f} s, "
            f"repli de {depth * 100:.0f} %, reprise",
            features={
                "impulse_atr": size / atr,
                "impulse_s": seconds,
                "depth": depth,
                "trend_aligned": float(trend * side),  # 1 dans le sens de la tendance M1, -1 contre, 0 indécise
            },
        )


class TwoCandleDetector:
    """« Deux bougies » (idée de l'utilisateur). Décidé à la clôture d'une bougie (bougies de `minutes` minutes,
    heure serveur, construites à partir des bougies de 5 s en prix médians), entrée tout de suite ; stop au-delà des
    deux dernières bougies (au-dessus de leur plus haut pour une vente), qui doivent se suivre (pas de trou).
    - mode « reprise » (v1) : baissière puis haussière -> vente ; haussière puis baissière -> achat ;
    - mode « suivre » (v2) : haussière -> achat, baissière -> vente (le sens change avec les bougies).
    """

    code = TWO_CANDLES

    def __init__(self, config: ScalpingConfig) -> None:
        self.bucket_ms = config.two_candles.minutes * 60_000
        self.follow = config.two_candles.mode == "suivre"
        self.candle_ms = config.candle_seconds * 1000
        self._start: int | None = None  # bougie en cours de construction
        self._ohlc: list[float] = []
        self._done: deque[tuple[int, float, float, float, float]] = deque(maxlen=2)  # (début, o, h, l, c)

    def on_candle(self, candle: Candle, trend: int, atr: float) -> Setup | None:
        start = candle.start_ms // self.bucket_ms * self.bucket_ms
        setup = None
        if self._start is not None and start != self._start:
            setup = self._finish(candle)  # la bougie précédente n'a pas eu sa dernière tranche de 5 s
        if self._start is None:
            self._start, self._ohlc = start, [candle.open, candle.high, candle.low, candle.close]
        else:
            first, high, low, _ = self._ohlc
            self._ohlc = [first, max(high, candle.high), min(low, candle.low), candle.close]
        if candle.start_ms + self.candle_ms >= start + self.bucket_ms:  # dernière tranche : bougie close
            setup = self._finish(candle) or setup
        return setup

    def _finish(self, trigger: Candle) -> Setup | None:
        self._done.append((self._start, *self._ohlc))
        self._start, self._ohlc = None, []
        if len(self._done) < 2:
            return None
        (t1, o1, h1, l1, c1), (t2, o2, h2, l2, c2) = self._done
        if t2 - t1 != self.bucket_ms:
            return None  # bougies non consécutives
        if self.follow:
            if c2 > o2:
                side, structure = LONG, min(l1, l2)
                text = "bougie haussière : achat dans le sens des bougies, stop sous les deux dernières"
            elif c2 < o2:
                side, structure = SHORT, max(h1, h2)
                text = "bougie baissière : vente dans le sens des bougies, stop au-dessus des deux dernières"
            else:
                return None
        elif c1 < o1 and c2 > o2:
            side, structure = SHORT, max(h1, h2)
            text = "bougie baissière puis haussière : vente, stop au-dessus des deux bougies"
        elif c1 > o1 and c2 < o2:
            side, structure = LONG, min(l1, l2)
            text = "bougie haussière puis baissière : achat, stop sous les deux bougies"
        else:
            return None
        # Niveau clé : l'autre extrême des deux bougies (objectif en mode « bougie »).
        level = max(h1, h2) if side == LONG else min(l1, l2)
        return Setup(TWO_CANDLES, side, level, structure, trigger, key=f"R{side:+d}@{t2}", reason=text,
                     features={"body1": abs(c1 - o1), "body2": abs(c2 - o2)})  # fmt: skip


def make_detectors(config: ScalpingConfig, only: tuple[str, ...] | None = None) -> list:
    """Détecteurs des stratégies actives (ou de celles demandées, pour le rejeu)."""
    active = {BREAKOUT: config.breakout.enabled, PULLBACK: config.pullback.enabled,
              TWO_CANDLES: config.two_candles.enabled}  # fmt: skip
    codes = (BREAKOUT, PULLBACK, TWO_CANDLES)
    wanted = [code for code in codes if (active[code] if only is None else code in only)]
    classes = {BREAKOUT: BreakoutDetector, PULLBACK: PullbackDetector, TWO_CANDLES: TwoCandleDetector}
    return [classes[code](config) for code in wanted]


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
    tps: tuple[float, ...] = ()  # plusieurs positions : un objectif chacune, du plus proche au plus loin (tp = dernier)


def day_direction(bars: pd.DataFrame, config: ScalpingConfig) -> np.ndarray:
    """Sens du mouvement du jour pour chaque barre M1 (supposée close) : +1, -1, ou 0 s'il est trop faible.

    Mouvement = clôture - ouverture du jour de cotation (heure serveur) ; seuil = direction_min_move_atr x l'ATR
    journalier des jours précédents (connu dès l'ouverture). ATR = moyenne SIMPLE du true range des N derniers
    jours : identique dès que N + 1 jours sont chargés, donc le même au rejeu et en démo (une moyenne de Wilder
    dépendrait de la longueur de l'historique chargé).
    """
    if bars.empty:
        return np.zeros(0, dtype=int)
    day = bars["time_server"].to_numpy() // 86_400
    day_open = pd.Series(bars["open"].to_numpy(dtype="float64")).groupby(day).transform("first").to_numpy()
    move = bars["close"].to_numpy(dtype="float64") - day_open
    days = trading_days(bars)
    tr = true_range(days["high"], days["low"], days["close"])
    tr.iloc[0] = np.nan  # premier jour chargé : clôture de la veille inconnue
    known = tr.rolling(config.direction_atr_days, min_periods=config.direction_atr_days).mean().shift(1)
    atr = known.reindex(day).to_numpy()
    with np.errstate(invalid="ignore"):
        strong = np.abs(move) >= config.direction_min_move_atr * atr  # NaN (pas assez de jours) : False
    return np.where(strong, np.sign(move), 0).astype(int)


def pips(price_distance: float, config: ScalpingConfig) -> str:
    """Distance de prix en pips (« 25.3 pips »), l'unité de l'utilisateur."""
    return f"{price_distance / config.pip_size:.1f} pips"


def plan_trade(
    setup: Setup,
    bid: float,
    ask: float,
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
    buffer = config.stop_buffer_points * point
    two = setup.strategy == TWO_CANDLES
    if config.fixed_stop_pips is not None:
        # Stop fixe en pips depuis le prix d'entrée (vente à 4000, 30 pips : stop à 4003).
        sl = round(entry - side * config.fixed_stop_pips * config.pip_size, digits)
    else:
        # La structure est en prix médian : le stop d'un achat se déclenche au bid (médian - spread / 2).
        sl = round(setup.structure - side * (spread / 2 + buffer), digits)
    distance = round((entry - sl) * side, digits)
    broker_minimum = (stops_level_points + freeze_level_points) * point + spread
    if two and config.fixed_stop_pips is None:
        # Deux bougies : stop trop proche élargi à min_stop_pips (pas de refus).
        widest = max(config.two_candles.min_stop_pips * config.pip_size, broker_minimum)
        if distance < widest:
            distance = round(widest, digits)
            sl = round(entry - side * distance, digits)
    minimum = broker_minimum if two else max(config.min_stop_points * point, broker_minimum)
    if distance < minimum - 1e-9:
        return f"stop trop proche : {pips(distance, config)} < {pips(minimum, config)}"
    if distance > config.max_stop_points * point + 1e-9:
        return f"stop trop loin : {pips(distance, config)} > {pips(config.max_stop_points * point, config)}"
    targets: tuple[float, ...]
    if two and config.two_candles.target_mode == "bougie":
        # Objectif juste au-delà des deux bougies (prix médian : le bid d'un achat doit le dépasser d'un demi-spread),
        # borné entre min_target_pips et le dernier objectif de target_pips.
        low = config.two_candles.min_target_pips * config.pip_size
        high = config.two_candles.target_pips[-1] * config.pip_size
        targets = (round(min(max((setup.level - entry) * side, low), high), digits),)
    elif two:
        targets = tuple(round(t * config.pip_size, digits) for t in config.two_candles.target_pips)
    elif config.fixed_target_pips is not None:
        targets = (round(config.fixed_target_pips * config.pip_size, digits),)
    else:
        targets = (round(config.target_ratio * distance, digits),)
    cost = spread + 2 * config.expected_slippage_points * point + commission_per_oz
    if targets[0] < config.min_target_cost_ratio * cost:
        return (f"objectif trop faible face au coût : {pips(targets[0], config)} < {config.min_target_cost_ratio:g} x "
                f"{pips(cost, config)}")  # fmt: skip
    tps = tuple(round(entry + side * t, digits) for t in targets)
    return Plan(side, entry, sl, tps[-1], distance, targets[-1], cost, spread, tps if two else ())


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
    mfe: float = 0.0  # meilleure excursion (prix par once) au prix de sortie possible (bid à l'achat)
    mae: float = 0.0  # pire excursion (négative)
    # Plusieurs positions (deux bougies) : un objectif par position, parts du lot ; le stop et la durée sont communs.
    tps: tuple[float, ...] = ()
    weights: tuple[float, ...] = ()
    leg_exits: list[float] = field(default_factory=list)  # sorties des positions déjà fermées à leur objectif

    @classmethod
    def open(cls, tag: str, plan: Plan, bid: float, ask: float, time_ms: int, max_hold_s: int, slip: float,
             volume: float = 0.0, fee: float = 0.0, weights: tuple[float, ...] = ()) -> SimTrade:  # fmt: skip
        entry = ask + slip if plan.side == LONG else bid - slip
        tps = plan.tps[: len(weights)] if plan.tps and weights else plan.tps
        weights = tuple(weights) if tps else ()
        if tps and not weights:
            weights = (1.0,) * len(tps)
        tp = tps[-1] if tps else plan.tp
        return cls(tag, plan.side, entry, plan.sl, tp, time_ms, time_ms + max_hold_s * 1000, slip, volume, fee,
                   tps=tps, weights=weights)  # fmt: skip

    def on_tick(self, time_ms: int, bid: float, ask: float) -> bool:
        """Met à jour le trade ; True s'il vient d'être fermé (toutes ses positions)."""
        if self.exit_price is not None:
            return False
        excursion = (bid - self.entry) if self.side == LONG else (self.entry - ask)
        if excursion > self.mfe:
            self.mfe = excursion
        elif excursion < self.mae:
            self.mae = excursion
        # Prix de sortie possible : bid pour un achat, ask pour une vente.
        price = bid if self.side == LONG else ask
        if (price - self.sl) * self.side <= 0:
            worst = min(self.sl, bid) if self.side == LONG else max(self.sl, ask)
            return self._close(time_ms, worst - self.side * self.slip, "stop")
        if self.tps:
            while len(self.leg_exits) < len(self.tps) and (price - self.tps[len(self.leg_exits)]) * self.side >= 0:
                self.leg_exits.append(self.tps[len(self.leg_exits)])
            if len(self.leg_exits) == len(self.tps):
                return self._close(time_ms, self.tp, "objectif")
        elif (price - self.tp) * self.side >= 0:
            return self._close(time_ms, self.tp, "objectif")
        if time_ms >= self.deadline_ms:
            return self._close(time_ms, price - self.side * self.slip, "durée max")
        return False

    def _average(self, price: float) -> float:
        """Prix de sortie moyen : positions fermées à leur objectif, les autres à `price` (pondéré par les parts)."""
        if not self.tps:
            return price
        exits = self.leg_exits + [price] * (len(self.tps) - len(self.leg_exits))
        return sum(w * x for w, x in zip(self.weights, exits)) / sum(self.weights)

    def _close(self, time_ms: int, price: float, reason: str) -> bool:
        self.exit_price, self.exit_ms, self.reason = self._average(price), time_ms, reason
        return True

    @property
    def move(self) -> float:
        """Résultat par once (USD), spread, glissement et commission inclus."""
        return 0.0 if self.exit_price is None else (self.exit_price - self.entry) * self.side - self.fee

    def move_at(self, bid: float, ask: float) -> float:
        """Résultat latent par once si le trade était fermé à ces prix (avec glissement et commission)."""
        exit_price = self._average(bid - self.slip if self.side == LONG else ask + self.slip)
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
