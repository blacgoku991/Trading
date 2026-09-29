"""Impulsion, repli, reprise : candidat expérimental, sans résultat promis.

L'impulsion dépasse la volatilité des bougies précédentes et suit la tendance M1.
On attend ensuite un vrai repli, puis une clôture au-delà de la bougie précédente.
Une impulsion produit au plus un signal. Aucune bougie inachevée n'est consultée.
"""

from collections import deque
from statistics import median

from goldbot.config import ScalpingConfig
from goldbot.scalping.engine import BreakoutDetector, Candle, Setup


class PullbackDetector:
    def __init__(self, config: ScalpingConfig):
        self.cfg = config
        self.history: deque[Candle] = deque()
        self.impulse: tuple[int, float, float, int] | None = None
        self.retraced = False

    def on_candle(self, candle: Candle, trend: int) -> Setup | None:
        cfg = self.cfg
        # Un trou de données invalide la séquence ; pas de reprise après une fermeture.
        if self.history and candle.start_ms - self.history[-1].start_ms > 2 * cfg.candle_seconds * 1000:
            self.history.clear()
            self.impulse = None
            self.retraced = False
        while self.history and self.history[0].start_ms < candle.start_ms - cfg.breakout_lookback_s * 1000:
            self.history.popleft()
        setup = None
        if self.impulse is not None:
            side, origin, extreme, started = self.impulse
            amplitude = (extreme - origin) * side
            depth = (extreme - (candle.low if side > 0 else candle.high)) * side / amplitude
            expired = candle.start_ms - started > cfg.pullback_expiry_s * 1000
            if trend != side or expired or depth > cfg.pullback_max_fraction:
                self.impulse = None
                self.retraced = False
            else:
                previous = self.history[-1]
                level = previous.high if side > 0 else previous.low
                if self.retraced and (candle.close - level) * side > 0 and (candle.close - candle.open) * side > 0:
                    setup = Setup(side, level, candle, f"PB-{candle.start_ms}-{'L' if side > 0 else 'S'}")
                    self.impulse = None
                    self.retraced = False
                elif depth >= cfg.pullback_min_fraction:
                    self.retraced = True
                elif not self.retraced:
                    extreme = max(extreme, candle.high) if side > 0 else min(extreme, candle.low)
                    self.impulse = (side, origin, extreme, started)
        elif len(self.history) >= cfg.min_window_candles and trend in (-1, 1):
            noise = max(median(c.high - c.low for c in self.history), candle.max_spread, 1e-8)
            body = (candle.close - candle.open) * trend
            if body >= cfg.pullback_impulse_ratio * noise:
                origin = candle.low if trend > 0 else candle.high
                extreme = candle.high if trend > 0 else candle.low
                self.impulse = (trend, origin, extreme, candle.start_ms)
                self.retraced = False
        self.history.append(candle)
        return setup


def make_detector(config: ScalpingConfig) -> BreakoutDetector | PullbackDetector:
    return PullbackDetector(config) if config.strategy == "pullback" else BreakoutDetector(config)
