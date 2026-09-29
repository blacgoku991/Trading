"""Boucle live (CLAUDE.md §11) : la stratégie du backtest, décidée à la clôture de chaque barre M1.

À chaque passage (quelques secondes) :
1. liaison avec le terminal (reconnexion si besoin) et jour de cotation ;
2. réconciliation : positions du bot (magic) contre l'état SQLite ; SL manquant -> reposé, sinon
   position fermée ; position inconnue -> fermée ; position disparue -> clôture relevée dans les deals ;
3. limites de risque sur l'equity (flottant inclus) : perte journalière, drawdown maximal ;
4. sorties horaires ;
5. si une nouvelle barre M1 est clôturée : mises à jour du stop des positions ouvertes (resserré seulement,
   jamais éloigné), puis intentions de la stratégie sur les barres clôturées, exécution des nouvelles
   (idempotence : un signal = un ordre, jamais deux).
Les positions manuelles (sans le magic du bot) ne sont jamais touchées.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from goldbot.backtest.engine import SESSIONS
from goldbot.broker import mt5_constants as C
from goldbot.broker.base import AccountState, Broker, BrokerError, Position, SymbolSpec
from goldbot.broker.supervisor import ConnectionSupervisor, FeedState, evaluate_feed
from goldbot.config import Settings
from goldbot.data.history import bars_frame
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.execution.orders import (
    UNCERTAIN,
    OrderRejected,
    filling_candidates,
    round_to_tick,
    select_filling,
    send_market_order,
)
from goldbot.indicators.sessions import next_session_start
from goldbot.risk.limits import RiskLimits
from goldbot.risk.sizing import position_size
from goldbot.state.store import CLOSED, FAILED, OPEN, SENDING, SIMULATED, SKIPPED, SignalRow, StateStore
from goldbot.strategies.base import LONG, OrderIntent, StopUpdate, Strategy

log = logging.getLogger("goldbot.live")
_MINUTE = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class LiveOptions:
    bars_window: int = 40_000  # barres M1 lues à chaque clôture (ATR journalier sur 14 jours de cotation)
    max_signal_age_s: float = 180.0  # signal plus vieux (bot relancé en retard) : ignoré
    slippage_points: float = 5.0  # marge de glissement dans le calcul de la taille
    deviation_points: int = 20
    close_retry_s: float = 60.0  # délai entre deux tentatives de clôture d'une même position
    deals_wait_s: float = 300.0  # délai maximal pour voir le deal de sortie d'une position disparue


class LiveRunner:
    def __init__(
        self,
        broker: Broker,
        supervisor: ConnectionSupervisor,
        strategy: Strategy,
        store: StateStore,
        *,
        settings: Settings,
        spec: SymbolSpec,
        simulate: bool,
        options: LiveOptions | None = None,
        now_utc: Callable[[], datetime],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.broker = broker
        self.supervisor = supervisor
        self.strategy = strategy
        self.store = store
        self.settings = settings
        self.spec = spec
        self.simulate = simulate
        self.options = options or LiveOptions()
        self.now_utc = now_utc
        self.sleep = sleep
        self.magic = settings.bot.magic
        self.rule = ServerTimeRule.from_config(settings.server_time)
        self.schedule = MarketSchedule.from_config(settings.market_hours)
        self.last_bar_time: pd.Timestamp | None = None
        self._close_attempts: dict[int, pd.Timestamp] = {}
        self._missing_since: dict[str, pd.Timestamp] = {}
        self._halt_logged = False
        equity = broker.account().equity
        self.limits = RiskLimits.start(settings.risk, equity)
        if store.load_risk(self.limits):
            log.info("compteurs de risque repris de l'état sauvegardé")

    # --- boucle -----------------------------------------------------------------------------------

    def step(self) -> None:
        now = pd.Timestamp(self.now_utc())
        self.supervisor.ensure_connected()
        account = self.broker.account()
        server_day = int(self.rule.server_epoch(now.to_pydatetime()) // 86_400)
        self.limits.new_day(server_day, account.equity)
        self._reconcile(now)
        self._check_equity(self.broker.account().equity, now)
        self._time_exits(now)
        self._new_signals(now)
        self.store.save_risk(self.limits)

    # --- positions --------------------------------------------------------------------------------

    def _mine(self) -> dict[int, Position]:
        return {p.ticket: p for p in self.broker.positions(self.spec.name) if p.magic == self.magic}

    def _reconcile(self, now: pd.Timestamp) -> None:
        positions = self._mine()
        for row in self.store.with_status(SENDING):
            match = [p for p in positions.values() if p.comment == row.tag]
            if match:
                position = match[0]
                self.store.update(row.tag, status=OPEN, position=position.ticket, volume=position.volume,
                                  entry_price=position.price_open)  # fmt: skip
                self.limits.on_entry()
                log.warning("signal %s : position %d retrouvée après un envoi incertain", row.tag, position.ticket)
            elif now - row.signal_time > pd.Timedelta(minutes=10):
                self.store.update(row.tag, status=FAILED, detail="envoi incertain, aucune position trouvée")
                log.error("signal %s : envoi incertain et aucune position trouvée", row.tag)
        for row in self.store.with_status(OPEN):
            position = positions.pop(row.position, None)
            if position is None:
                self._record_close(row, now)
            elif position.sl == 0.0:
                self._restore_sl(row, position)
        for position in positions.values():
            # Position du bot inconnue de l'état (état perdu ou effacé) : on ne garde rien qu'on ne maîtrise pas.
            log.error("position %d du bot inconnue de l'état : fermeture par sécurité", position.ticket)
            self._close_position(position, "position inconnue de l'état", now)

    def _restore_sl(self, row: SignalRow, position: Position) -> None:
        """Règle 3 : une position sans SL reçoit son SL tout de suite, sinon elle est fermée."""
        request = {
            "action": C.TRADE_ACTION_SLTP,
            "symbol": self.spec.name,
            "position": position.ticket,
            "sl": row.sl,
            "tp": row.tp or 0.0,
            "magic": self.magic,
        }
        result = self.broker.order_send(request)
        if result.retcode == C.TRADE_RETCODE_DONE:
            log.warning("position %d sans SL : SL reposé à %.2f", position.ticket, row.sl)
            return
        log.error("position %d sans SL et SL impossible à reposer (%d) : fermeture", position.ticket, result.retcode)
        self._close_position(position, "SL impossible à reposer", pd.Timestamp(self.now_utc()))

    def _record_close(self, row: SignalRow, now: pd.Timestamp) -> None:
        deals = self.broker.deals_for_position(row.position)
        exits = [d for d in deals if d.entry in (C.DEAL_ENTRY_OUT, C.DEAL_ENTRY_OUT_BY)]
        if not exits:
            first_seen = self._missing_since.setdefault(row.tag, now)
            if now - first_seen < pd.Timedelta(seconds=self.options.deals_wait_s):
                return  # historique pas encore à jour : on réessaiera au prochain passage
            self.store.update(row.tag, status=CLOSED, detail="position fermée, deal de sortie introuvable")
            log.error("trade %s fermé mais deal de sortie introuvable : résultat inconnu", row.tag)
            return
        pnl = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
        price = sum(d.price * d.volume for d in exits) / sum(d.volume for d in exits)
        reason = {C.DEAL_REASON_SL: "SL", C.DEAL_REASON_TP: "TP"}.get(exits[-1].reason, "clôture")
        self.store.update(row.tag, status=CLOSED, exit_price=price, pnl=pnl, detail=reason)
        self.limits.on_exit(pnl, now, lambda moment: next_session_start(moment, SESSIONS))
        log.info("trade %s fermé (%s) à %.2f : résultat %+.2f %s", row.tag, reason, price, pnl, self._currency())

    def _close_position(self, position: Position, reason: str, now: pd.Timestamp) -> bool:
        last = self._close_attempts.get(position.ticket)
        if last is not None and now - last < pd.Timedelta(seconds=self.options.close_retry_s):
            return False
        self._close_attempts[position.ticket] = now
        request = {
            "action": C.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": C.ORDER_TYPE_SELL if position.type == C.POSITION_TYPE_BUY else C.ORDER_TYPE_BUY,
            "position": position.ticket,
            "deviation": self.options.deviation_points,
            "magic": self.magic,
            "comment": position.comment,
            "type_time": C.ORDER_TIME_GTC,
        }
        try:
            send_market_order(self.broker, request, filling_candidates(self.spec.filling_mode), sleep=self.sleep)
        except (OrderRejected, BrokerError) as exc:
            log.error("clôture de la position %d impossible (%s) : le SL serveur reste en place", position.ticket, exc)
            return False
        log.info("position %d fermée : %s", position.ticket, reason)
        return True

    def _close_all(self, reason: str, now: pd.Timestamp) -> None:
        for position in self._mine().values():
            self._close_position(position, reason, now)

    def _check_equity(self, equity: float, now: pd.Timestamp) -> None:
        crossed = self.limits.on_equity(equity)
        if crossed:
            log.warning("%s (equity %.2f %s) : fermeture de toutes les positions du bot", crossed[0], equity,
                        self._currency())  # fmt: skip
            self._close_all(crossed[0], now)

    def _time_exits(self, now: pd.Timestamp) -> None:
        positions = self._mine()
        for row in self.store.with_status(OPEN):
            if row.exit_at is not None and now >= row.exit_at and row.position in positions:
                self._close_position(positions[row.position], "sortie horaire", now)

    # --- signaux ----------------------------------------------------------------------------------

    def _closed_bars(self, now: pd.Timestamp) -> pd.DataFrame:
        raw = self.broker.latest_bars(self.spec.name, self.options.bars_window)
        bars = bars_frame(raw, self.rule).dropna(subset=["time"])
        return bars[bars["time"] + _MINUTE <= now].reset_index(drop=True)

    def _new_signals(self, now: pd.Timestamp) -> None:
        if self.last_bar_time is not None and now.floor("min") - _MINUTE <= self.last_bar_time:
            return  # aucune nouvelle barre ne peut être clôturée : on ne relit l'historique qu'une fois par minute
        bars = self._closed_bars(now)
        if bars.empty:
            return
        last = bars["time"].iloc[-1]
        if self.last_bar_time is not None and last <= self.last_bar_time:
            return
        self.last_bar_time = last
        self._manage_stops(self.strategy.stop_updates(bars), now)
        if self.limits.halted:
            if not self._halt_logged:
                log.error("drawdown maximal atteint : plus aucune entrée, relance manuelle requise")
                self._halt_logged = True
            return
        oldest = now - pd.Timedelta(seconds=self.options.max_signal_age_s)
        for intent in self.strategy.intents(bars):
            if intent.time + _MINUTE >= oldest and not self.store.seen(intent.tag):
                self._execute(intent, now)

    def _manage_stops(self, updates: list[StopUpdate], now: pd.Timestamp) -> None:
        """Dernière mise à jour de chaque position ouverte : stop resserré (jamais éloigné, règle 4) ou sortie."""
        if not updates:
            return
        latest = {update.tag: update for update in updates}  # triées par date : la plus récente l'emporte
        positions = self._mine()
        spec = self.spec
        for row in self.store.with_status(OPEN):
            update, position = latest.get(row.tag), positions.get(row.position)
            if update is None or position is None:
                continue
            if update.exit:
                self._close_position(position, f"sortie de la stratégie ({update.reason})", now)
                continue
            side = LONG if position.type == C.POSITION_TYPE_BUY else -LONG
            # Arrondi vers la position (jamais au-delà du stop demandé), au pas de cotation.
            sl = round_to_tick(update.sl, spec.trade_tick_size or spec.point, spec.digits,
                               "down" if side == LONG else "up")  # fmt: skip
            if (sl - position.sl) * side <= 0:
                continue  # n'éloigne jamais le SL, et pas de requête inutile
            tick = self.broker.tick(spec.name)
            price = tick.bid if side == LONG else tick.ask
            if (price - sl) * side <= (spec.trade_stops_level + spec.trade_freeze_level) * spec.point:
                self._close_position(position, "stop suiveur dépassé", now)
                continue
            if self.simulate:
                log.info("SIMULATION : SL de la position %d déplacé à %.2f (%s)", position.ticket, sl, update.reason)
                continue
            request = {
                "action": C.TRADE_ACTION_SLTP,
                "symbol": spec.name,
                "position": position.ticket,
                "sl": sl,
                "tp": position.tp,
                "magic": self.magic,
            }
            result = self.broker.order_send(request)
            if result.retcode == C.TRADE_RETCODE_DONE:
                self.store.update(row.tag, sl=sl)
                log.info("position %d : SL resserré de %.2f à %.2f (%s)", position.ticket, position.sl, sl,
                         update.reason)  # fmt: skip
            else:
                log.warning("position %d : SL non modifié (%d), l'ancien SL serveur reste en place", position.ticket,
                            result.retcode)  # fmt: skip

    def _skip(self, intent: OrderIntent, status: str, reason: str) -> None:
        self.store.record(intent, status, reason)
        log.info("signal %s non exécuté : %s", intent.tag, reason)

    def _execute(self, intent: OrderIntent, now: pd.Timestamp) -> None:
        log.info("signal %s : %s", intent.tag, intent.reason)
        tick = self.broker.tick(self.spec.name)
        feed = evaluate_feed(tick, now.to_pydatetime(), self.rule, self.schedule, self.settings.feed)
        if feed.state in (FeedState.STALE, FeedState.CLOSED):
            self._skip(intent, SKIPPED, f"flux de prix {feed.state.value}")
            return
        block = self.limits.entry_block(now, len(self._mine()))
        if block is not None:
            self._skip(intent, SKIPPED, block)
            return
        side = intent.side
        entry = tick.ask if side == LONG else tick.bid
        spec = self.spec
        sl = round_to_tick(intent.sl, spec.trade_tick_size or spec.point, spec.digits)
        tp = None if intent.tp is None else round_to_tick(intent.tp, spec.trade_tick_size or spec.point, spec.digits)
        min_distance = (spec.trade_stops_level + spec.trade_freeze_level) * spec.point + (tick.ask - tick.bid)
        if (entry - sl) * side <= min_distance:
            self._skip(intent, SKIPPED, "SL trop proche du prix actuel")
            return
        order_type = C.ORDER_TYPE_BUY if side == LONG else C.ORDER_TYPE_SELL
        worst_exit = sl - side * self.options.slippage_points * spec.point
        loss_per_lot = -self.broker.calc_profit(order_type, spec.name, 1.0, entry, worst_exit)
        account = self.broker.account()
        volume = position_size(
            self.limits.risk_money(account.equity),
            loss_per_lot,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
        )
        if volume == 0:
            self._skip(intent, SKIPPED, "volume sous le minimum du broker pour ce risque")
            return
        if self.broker.calc_margin(order_type, spec.name, volume, entry) > 0.5 * account.margin_free:
            self._skip(intent, SKIPPED, "marge libre insuffisante")
            return
        if self.simulate:
            self._skip(intent, SIMULATED, f"SIMULATION : {volume:g} lot à {entry:.2f}, SL {sl:.2f}")
            return
        request = {
            "action": C.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": volume,
            "type": order_type,
            "sl": sl,
            "tp": tp or 0.0,
            "deviation": self.options.deviation_points,
            "magic": self.magic,
            "comment": intent.tag,
            "type_time": C.ORDER_TIME_GTC,
        }
        if not self.store.record(intent, SENDING):
            return  # déjà traité (jamais deux ordres pour le même signal)
        try:
            filling, _ = select_filling(self.broker, {**request, "price": entry}, spec.filling_mode)
            candidates = filling_candidates(spec.filling_mode)
            send_market_order(self.broker, request, candidates[candidates.index(filling) :], sleep=self.sleep)
        except OrderRejected as exc:
            if exc.retcode in UNCERTAIN:
                log.warning("signal %s : envoi incertain (%s), vérification au prochain passage", intent.tag, exc)
            else:
                self.store.update(intent.tag, status=FAILED, detail=str(exc))
                log.error("signal %s refusé : %s", intent.tag, exc)
            return
        except BrokerError as exc:
            log.warning("signal %s : liaison perdue pendant l'envoi (%s), vérification au prochain passage",
                        intent.tag, exc)  # fmt: skip
            return
        for _ in range(10):
            match = [p for p in self._mine().values() if p.comment == intent.tag]
            if match:
                position = match[0]
                self.store.update(intent.tag, status=OPEN, position=position.ticket, volume=position.volume,
                                  entry_price=position.price_open)  # fmt: skip
                self.limits.on_entry()
                exit_at = "-"
                if intent.exit_at is not None:
                    exit_at = intent.exit_at.tz_convert(self.settings.bot.display_timezone).strftime("%H:%M")
                log.info(
                    "trade %s ouvert : %s %g lot à %.2f, SL %.2f, sortie prévue à %s",
                    intent.tag,
                    "achat" if side == LONG else "vente",
                    position.volume,
                    position.price_open,
                    position.sl,
                    exit_at,
                )
                return
            self.sleep(0.5)
        log.warning("signal %s : ordre exécuté mais position pas encore visible, vérification ensuite", intent.tag)

    def _currency(self) -> str:
        return self.broker.account().currency


def summary_line(account: AccountState, limits: RiskLimits, open_positions: int) -> str:
    return (
        f"equity {account.equity:.2f} {account.currency}, positions ouvertes {open_positions}, "
        f"trades du jour {limits.trades_today}, début de journée {limits.day_start_equity:.2f}"
    )
