"""Surveillance de la connexion : reconnexion avec backoff exponentiel, détection du flux figé."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError, TerminalState, Tick
from goldbot.config import FeedConfig, ReconnectConfig
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule

log = logging.getLogger("goldbot.supervisor")


def backoff_delays(config: ReconnectConfig) -> Iterator[float]:
    """Attentes entre deux essais : base, 2 x base, 4 x base… plafonnées (max_attempts - 1 valeurs)."""
    delay = config.base_delay_s
    for _ in range(config.max_attempts - 1):
        yield delay
        delay = min(delay * 2, config.max_delay_s)


class FeedState(Enum):
    FRESH = "frais"
    STALE = "figé"
    GRACE = "tolérance après ouverture"
    CLOSED = "marché fermé"


@dataclass(frozen=True)
class FeedStatus:
    state: FeedState
    tick_age_s: float
    server_time: datetime


def evaluate_feed(
    tick: Tick, now_utc: datetime, rule: ServerTimeRule, schedule: MarketSchedule, config: FeedConfig
) -> FeedStatus:
    """Flux figé = tick trop vieux alors que le marché cote (flux mort sans déconnexion)."""
    server_now = rule.to_server(now_utc)
    age = rule.server_epoch(now_utc) - tick.time_msc / 1000
    if not schedule.is_open(server_now):
        state = FeedState.CLOSED
    elif age <= config.stale_after_s:
        state = FeedState.FRESH
    elif schedule.seconds_since_open(server_now) < config.grace_after_open_s:
        state = FeedState.GRACE
    else:
        state = FeedState.STALE
    return FeedStatus(state, age, server_now)


class ConnectionSupervisor:
    """Connexion et reconnexion au terminal, avec attente exponentielle entre les essais."""

    def __init__(
        self, broker: Broker, config: ReconnectConfig, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._broker = broker
        self._config = config
        self._sleep = sleep

    def connect(self) -> None:
        """Se connecte ; réessaie les échecs transitoires, lève immédiatement les autres."""
        delays = backoff_delays(self._config)
        attempt = 0
        while True:
            attempt += 1
            try:
                self._broker.connect()
            except BrokerError as error:
                delay = next(delays, None) if error.retryable else None
                if delay is None:
                    raise
                log.warning(
                    "connexion MT5 impossible (%s) ; essai %d/%d dans %.0f s",
                    error,
                    attempt + 1,
                    self._config.max_attempts,
                    delay,
                )
                self._sleep(delay)
            else:
                if attempt > 1:
                    log.info("connexion MT5 établie à l'essai %d", attempt)
                return

    def ensure_connected(self) -> TerminalState:
        """Vérifie la liaison ; reconnecte si l'IPC est perdue, attend si le terminal est coupé du serveur."""
        try:
            state = self._broker.terminal()
        except BrokerError as error:
            if not error.retryable:
                raise
            log.warning("liaison avec le terminal perdue (%s) : reconnexion", error)
            self._shutdown_quietly()
            self.connect()
            state = self._broker.terminal()
        if state.connected:
            return state
        log.warning("terminal déconnecté du serveur de trading : attente de sa reconnexion")
        for delay in backoff_delays(self._config):
            self._sleep(delay)
            state = self._broker.terminal()
            if state.connected:
                log.info("terminal reconnecté au serveur de trading")
                return state
        raise BrokerError("terminal_info", C.RES_E_FAIL, "terminal toujours déconnecté du serveur de trading")

    def _shutdown_quietly(self) -> None:
        try:
            self._broker.shutdown()
        except BrokerError as error:
            log.debug("shutdown ignoré : %s", error)
