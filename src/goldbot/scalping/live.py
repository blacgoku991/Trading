"""Expérience de scalping en direct, compte DÉMO uniquement (séparée du bot principal).

Toutes les ~0,5 s : nouveaux ticks -> bougies de 5 s -> signal (engine.py) -> plan -> contrôles du
compte -> envoi au broker avec stop et objectif côté serveur, ou simulation locale si la cadence
d'envoi n'est pas encore vérifiée auprès d'Axi. Puis gestion des positions : sortie forcée à la durée
maximale (même en perte), stop manquant reposé, positions disparues relevées dans les deals.
Une simulation parallèle rejoue chaque signal accepté avec un glissement supplémentaire ; les limites
(positions, risque cumulé) portent sur l'ensemble des trades acceptés, envoyés ou simulés.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError, Position, SymbolSpec
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.config import Settings
from goldbot.data.history import bars_frame
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.execution.orders import (
    UNCERTAIN,
    OrderRejected,
    filling_candidates,
    select_filling,
    send_market_order,
)
from goldbot.risk.sizing import position_size
from goldbot.scalping.engine import (
    LONG,
    Candle,
    CandleBuilder,
    Plan,
    Setup,
    SimTrade,
    ema_trend,
    plan_trade,
)
from goldbot.scalping.store import CLOSED, FAILED, NOT_SENT, OPEN, REFUSED, SENDING, DemoTrade, ScalpStore
from goldbot.scalping.pullback import make_detector
from goldbot.scalping.learning import SignalModel, signal_features

log = logging.getLogger("goldbot.scalp")
_EPOCH = datetime(1970, 1, 1)
WARMUP_S = 120  # ticks relus au démarrage pour remplir la fenêtre des 60 s (sans trader dessus)
CLOCK_GRACE_MS = 1000  # une bougie n'est close par l'horloge qu'une seconde après sa fin (ticks en retard)
CLOSE_RETRY_MS = 5000  # délai entre deux tentatives de clôture d'une même position
_DEAL_REASONS = {
    C.DEAL_REASON_SL: "stop",
    C.DEAL_REASON_TP: "objectif",
    C.DEAL_REASON_SO: "stop out",
    C.DEAL_REASON_CLIENT: "clôture manuelle",
    C.DEAL_REASON_MOBILE: "clôture manuelle",
    C.DEAL_REASON_WEB: "clôture manuelle",
}


class DemoAccountChanged(Exception):
    """Le compte / type de compte a changé pendant l'exécution : arrêt sans aucun ordre."""


class ScalpRunner:
    def __init__(
        self,
        broker: Broker,
        supervisor: ConnectionSupervisor,
        store: ScalpStore,
        *,
        settings: Settings,
        spec: SymbolSpec,
        local_only: bool,
        now_utc: Callable[[], datetime],
        say: Callable[[str], None],
        sleep: Callable[[float], None] = time.sleep,
        model: SignalModel | None = None,
        filter_model: bool = False,
    ) -> None:
        self.broker = broker
        self.supervisor = supervisor
        self.store = store
        self.settings = settings
        self.cfg = settings.scalping
        self.spec = spec
        self.local_only = local_only
        self.now_utc = now_utc
        self.say = say
        self.sleep = sleep
        self.model = model
        self.filter_model = filter_model
        if filter_model and (model is None or not model.approved):
            raise ValueError("modèle non admissible au filtre démo")
        self.rule = ServerTimeRule.from_config(settings.server_time)
        self.schedule = MarketSchedule.from_config(settings.market_hours)
        self.zone = settings.bot.display_timezone
        self.builder = CandleBuilder(self.cfg.candle_seconds)
        self.detector = make_detector(self.cfg)
        self.recent: deque = deque()
        self.cursor_ms: int | None = None
        self._at_cursor: set[tuple[int, float, float]] = set()
        self.trading_from_ms: int | None = None  # pas de trade sur les ticks de préchauffage
        self.trend = 0
        self._trend_minute: int | None = None
        self.shadow: dict[str, tuple[SimTrade, float, bool]] = {}  # tag -> (trade simulé, risque, envoyé ?)
        self.last_entry_ms = store.latest_entry_ms()
        self.last_broker_order_ms = int(store.meta("last_broker_order_ms") or -(2**62))
        self.day: int | None = None
        self.day_start_ms = 0
        account = broker.account()
        self.account_identity = (account.login, account.server)
        self.currency = account.currency
        self.day_start_equity = account.equity
        self._close_attempts: dict[int, int] = {}
        self._stop_attempts: dict[int, int] = {}
        tick = broker.tick(spec.name)
        # Valeur, dans la devise du compte, d'un mouvement de 1 $ sur 1 once (conversion faite par le terminal).
        one_lot = broker.calc_profit(C.ORDER_TYPE_BUY, spec.name, 1.0, tick.ask, tick.ask + 1.0)
        self.value_per_dollar_oz = one_lot / spec.trade_contract_size
        # Commission (config, USD par lot et par côté) : en prix par once aller-retour, et par lot à la sortie.
        commission = settings.backtest.commission_per_lot_side
        self.fee_per_oz = 2 * commission / spec.trade_contract_size
        self.exit_commission_per_lot = commission * self.value_per_dollar_oz
        self.store.set_meta("fee_per_oz", str(self.fee_per_oz))

    # --- affichage ----------------------------------------------------------------------------

    def _clock(self, server_ms: int) -> str:
        utc = self.rule.server_to_utc(_EPOCH + timedelta(milliseconds=server_ms))
        return pd.Timestamp(utc).tz_convert(self.zone).strftime("%H:%M:%S")

    def _money(self, value: float) -> str:
        return f"{value:+.2f} {self.currency}"

    # --- boucle -------------------------------------------------------------------------------

    def server_now_ms(self) -> int:
        return int(self.rule.server_epoch(self.now_utc()) * 1000)

    def _is_open(self, server_ms: int) -> bool:
        return self.schedule.is_open(_EPOCH + timedelta(milliseconds=server_ms))

    def step(self) -> None:
        self.supervisor.ensure_connected()
        self._assert_demo_account()
        now_ms = self.server_now_ms()
        self._new_day(now_ms)
        self._refresh_trend(now_ms)
        if self.trading_from_ms is None:
            self.trading_from_ms = now_ms
        for time_ms, bid, ask in self._new_ticks(now_ms):
            self._advance_shadow(time_ms, bid, ask)
            if not self._is_open(time_ms):
                continue
            for candle in self.builder.add(time_ms, bid, ask):
                self._on_candle(candle, now_ms)
        for candle in self.builder.close_until(now_ms - CLOCK_GRACE_MS):
            self._on_candle(candle, now_ms)
        self._manage_positions(now_ms)

    def _new_day(self, now_ms: int) -> None:
        day = now_ms // 86_400_000
        if day != self.day:
            self.day, self.day_start_ms = day, day * 86_400_000
            stored_day = self.store.meta("risk_day")
            if stored_day == str(day):
                self.day_start_equity = float(self.store.meta("day_start_equity"))
            else:
                self.day_start_equity = self.broker.account().equity
                self.store.set_meta("risk_day", str(day))
                self.store.set_meta("day_start_equity", str(self.day_start_equity))

    def _assert_demo_account(self) -> None:
        account = self.broker.account()
        if (account.trade_mode != C.ACCOUNT_TRADE_MODE_DEMO
                or account.margin_mode != C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING
                or (account.login, account.server) != self.account_identity):
            raise DemoAccountChanged("compte changé ou non démo : arrêt immédiat, aucun nouvel ordre")

    def _refresh_trend(self, now_ms: int) -> None:
        minute = now_ms // 60_000
        if minute == self._trend_minute:
            return
        raw = self.broker.latest_bars(self.spec.name, 300)
        bars = bars_frame(raw, self.rule)
        closed = bars[(bars["time_server"] + 60) * 1000 <= now_ms]
        self.trend = ema_trend(tuple(closed["close"].astype(float)), self.cfg.ema_fast, self.cfg.ema_slow)
        self._trend_minute = minute

    def _new_ticks(self, now_ms: int) -> list[tuple[int, float, float]]:
        start_s = (self.cursor_ms // 1000) if self.cursor_ms is not None else now_ms // 1000 - WARMUP_S
        raw = self.broker.ticks_range(self.spec.name, start_s, now_ms // 1000 + 1, C.COPY_TICKS_ALL)
        fresh = []
        for time_ms, bid, ask in zip(raw["time_msc"].tolist(), raw["bid"].tolist(), raw["ask"].tolist(), strict=True):
            key = (time_ms, bid, ask)
            if bid <= 0 or ask < bid or (self.cursor_ms is not None and time_ms < self.cursor_ms):
                continue
            if time_ms == self.cursor_ms and key in self._at_cursor:
                continue
            fresh.append(key)
        if fresh:
            last = fresh[-1][0]
            if last != self.cursor_ms:
                self.cursor_ms, self._at_cursor = last, set()
            self._at_cursor.update(k for k in fresh if k[0] == last)
        return fresh

    # --- signaux --------------------------------------------------------------------------------

    def _on_candle(self, candle: Candle, now_ms: int) -> None:
        self.recent.append(candle)
        while self.recent and self.recent[0].start_ms < candle.start_ms - self.cfg.breakout_lookback_s * 1000:
            self.recent.popleft()
        setup = self.detector.on_candle(candle, self.trend)
        end_ms = candle.start_ms + self.builder.size_ms
        if setup is None or end_ms <= self.trading_from_ms or self.store.seen(setup.tag):
            return  # pas de signal, ou bougie du préchauffage, ou déjà traité
        self._handle(setup, now_ms)

    def _refuse(self, setup: Setup, spread: float, reason: str) -> None:
        self.store.record(setup.tag, setup.candle.start_ms, setup.side, REFUSED, detail=reason, spread=spread,
                          level=setup.level)  # fmt: skip
        self.say(f"   refus : {reason}")

    def _handle(self, setup: Setup, now_ms: int) -> None:
        cfg, spec = self.cfg, self.spec
        tick = self.broker.tick(spec.name)
        spread = tick.ask - tick.bid
        side_text = "ACHAT" if setup.side == LONG else "VENTE"
        trend_text = "haussière" if self.trend > 0 else "baissière"
        self.say(
            f"{self._clock(now_ms)} signal {side_text} ({cfg.strategy}) : niveau {setup.level:.2f} confirmé "
            f"(tendance M1 {trend_text}), spread {spread:.2f} $"
        )
        age_ms = now_ms - (setup.candle.start_ms + self.builder.size_ms)
        if age_ms > self.builder.size_ms:
            self._refuse(setup, spread, f"signal périmé (bougie close depuis {age_ms / 1000:.0f} s)")
            return
        if not self._is_open(now_ms + cfg.max_hold_s * 1000):
            self._refuse(setup, spread, f"le marché ferme avant la durée max ({cfg.max_hold_s} s)")
            return
        if now_ms - tick.time_msc > max(5000, cfg.candle_seconds * 1000) or tick.time_msc > now_ms + 2000:
            self._refuse(setup, spread, "cotation périmée ou horloge désynchronisée")
            return
        plan = plan_trade(
            setup,
            tick.bid,
            tick.ask,
            list(self.recent),
            cfg,
            point=spec.point,
            stops_level_points=spec.trade_stops_level,
            freeze_level_points=spec.trade_freeze_level,
            digits=spec.digits,
            commission_per_oz=self.fee_per_oz,
        )
        if isinstance(plan, str):
            self._refuse(setup, spread, plan)
            return
        features = signal_features(setup, list(self.recent), plan)
        score = float(self.model.probability(features)) if self.model else None
        self.store.record_features(setup.tag, features, score, self.model.digest if self.model else None)
        if score is not None:
            self.say(f"   score statistique {score:.0%} (estimation, pas une certitude)")
            if self.filter_model and score < self.model.payload["threshold"]:
                self._refuse(setup, spread, "score inférieur au seuil du modèle figé")
                return
        order_type = C.ORDER_TYPE_BUY if setup.side == LONG else C.ORDER_TYPE_SELL
        worst = plan.sl - setup.side * cfg.expected_slippage_points * spec.point
        loss_per_lot = -self.broker.calc_profit(order_type, spec.name, 1.0, plan.entry, worst)
        loss_per_lot += 2 * self.exit_commission_per_lot
        account = self.broker.account()
        volume = position_size(
            account.equity * cfg.risk_per_trade_pct / 100,
            loss_per_lot,
            volume_min=spec.volume_min,
            volume_max=spec.volume_max,
            volume_step=spec.volume_step,
        )
        if volume == 0:
            self._refuse(setup, spread, "lot minimum au-dessus du budget de risque")
            return
        risk = volume * loss_per_lot
        # Perte du jour : résultat démo réalisé + latent ; en --simulation, résultat simulé réalisé (comme le rejeu).
        if self.local_only:
            today, label = self.store.sim_realized_since(self.day_start_ms), "simulation"
        else:
            mine = [p for p in self.broker.positions(spec.name) if p.magic == cfg.magic]
            today = self.store.realized_since(self.day_start_ms) + sum(p.profit + p.swap for p in mine)
            label = "démo, latent compris"
        if today <= -cfg.daily_loss_pct / 100 * self.day_start_equity:
            self._refuse(setup, spread, f"perte du jour atteinte ({cfg.daily_loss_pct:g} %) : {label} "
                                        f"{self._money(today)}")  # fmt: skip
            return
        # Positions de la stratégie : envoyées au broker (état) ou simulées localement (en mémoire).
        active = {t.tag: t.risk or 0.0 for t in self.store.demo_trades(OPEN, SENDING)}
        for tag, (_, shadow_risk, _) in self.shadow.items():
            active.setdefault(tag, shadow_risk)
        if len(active) >= cfg.max_open_positions:
            self._refuse(setup, spread, f"{cfg.max_open_positions} positions déjà ouvertes")
            return
        if sum(active.values()) + risk > cfg.max_total_risk_pct / 100 * account.equity:
            self._refuse(setup, spread, f"risque cumulé au maximum ({cfg.max_total_risk_pct:g} %)")
            return
        if now_ms - self.last_entry_ms < cfg.min_seconds_between_entries * 1000:
            self._refuse(setup, spread, f"moins de {cfg.min_seconds_between_entries:g} s depuis la dernière entrée")
            return
        if self.store.recent_entry_count(now_ms - 60_000) >= cfg.max_entries_per_minute:
            self._refuse(setup, spread, "plafond d'entrées sur 60 secondes atteint")
            return
        self.last_entry_ms = now_ms
        details = (
            f"{volume:g} lot à {plan.entry:.2f} | spread {plan.spread:.2f} | stop {plan.sl:.2f} "
            f"(-{plan.stop_distance:.2f} $) | objectif {plan.tp:.2f} (+{plan.target:.2f} $) | "
            f"durée max {cfg.max_hold_s} s | risque {self._money(-risk)}"
        )
        since_order = now_ms - self.last_broker_order_ms
        cadence_ok = cfg.cadence_verified or since_order >= cfg.broker_min_seconds_between_orders * 1000
        sent = not self.local_only and cadence_ok
        self._start_shadow(setup, plan, tick.bid, tick.ask, now_ms, volume, risk, sent)
        fields = dict(level=setup.level, spread=plan.spread, volume=volume, entry=plan.entry, sl=plan.sl, tp=plan.tp,
                      risk=risk)  # fmt: skip
        if not sent:
            why = "mode --simulation" if self.local_only else "plafond d'envoi configuré"
            self.store.record(setup.tag, setup.candle.start_ms, setup.side, NOT_SENT, detail=why, **fields)
            self.say(f"   non envoyé au broker ({why}), simulé seulement : {details}")
            return
        self._send(setup, plan, order_type, volume, now_ms, fields, details)

    def _start_shadow(self, setup: Setup, plan: Plan, bid: float, ask: float, now_ms: int, volume: float,
                      risk: float, sent: bool) -> None:  # fmt: skip
        slip = self.cfg.extra_slippage_points * self.spec.point
        trade = SimTrade.open(setup.tag, plan, bid, ask, now_ms, self.cfg.max_hold_s, slip, volume, self.fee_per_oz)
        self.shadow[setup.tag] = (trade, risk, sent)
        self.store.record_sim(setup.tag, now_ms, setup.side, trade.entry, trade.sl, trade.tp, volume, sent)

    def _advance_shadow(self, time_ms: int, bid: float, ask: float) -> None:
        for tag, (trade, _, sent) in list(self.shadow.items()):
            if trade.on_tick(time_ms, bid, ask):
                pnl = trade.move * trade.volume * self.spec.trade_contract_size * self.value_per_dollar_oz
                self.store.close_sim(tag, trade.exit_price, trade.reason, pnl, time_ms)
                del self.shadow[tag]
                if not sent:
                    self.say(f"{self._clock(time_ms)} sortie simulée {tag} ({trade.reason}) : {self._money(pnl)}")

    def _send(self, setup: Setup, plan: Plan, order_type: int, volume: float, now_ms: int,
              fields: dict[str, object], details: str) -> None:  # fmt: skip
        self._assert_demo_account()
        spec = self.spec
        request = {
            "action": C.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": volume,
            "type": order_type,
            "sl": plan.sl,
            "tp": plan.tp,
            "deviation": 20,
            "magic": self.cfg.magic,
            "comment": setup.tag,
            "type_time": C.ORDER_TIME_GTC,
        }
        if not self.store.record(setup.tag, setup.candle.start_ms, setup.side, SENDING, **fields):
            return
        self.last_broker_order_ms = now_ms
        self.store.set_meta("last_broker_order_ms", str(now_ms))
        try:
            filling, _ = select_filling(self.broker, {**request, "price": plan.entry}, spec.filling_mode)
            candidates = filling_candidates(spec.filling_mode)
            send_market_order(self.broker, request, candidates[candidates.index(filling) :], sleep=self.sleep)
        except OrderRejected as exc:
            if exc.operation == "order_send" and exc.retcode in UNCERTAIN:
                # Peut-être exécuté : reste « envoi », rapproché des positions au passage suivant (jamais renvoyé).
                self.say(f"   réponse incertaine du broker ({exc}) : vérification au prochain passage")
                return
            self.store.update(setup.tag, status=FAILED, detail=str(exc))
            self.say(f"   ordre refusé par le broker : {exc}")
            return
        except BrokerError as exc:
            self.say(f"   liaison perdue pendant l'envoi ({exc}) : vérification au prochain passage")
            return
        position = self._find(setup.tag)
        if position is None:
            self.say("   ordre envoyé, position pas encore visible : vérification au prochain passage")
            return
        self._mark_open(setup.tag, position)
        self.say(f"   entrée démo : {details.replace(f'{plan.entry:.2f}', f'{position.price_open:.2f}', 1)}")

    def _find(self, tag: str) -> Position | None:
        for _ in range(6):
            for position in self.broker.positions(self.spec.name):
                if position.magic == self.cfg.magic and position.comment == tag:
                    return position
            self.sleep(0.25)
        return None

    def _mark_open(self, tag: str, position: Position) -> None:
        open_ms = int(position.time_msc)
        self.store.update(tag, status=OPEN, position=position.ticket, open_price=position.price_open,
                          deadline_ms=open_ms + self.cfg.max_hold_s * 1000)  # fmt: skip

    # --- positions --------------------------------------------------------------------------------

    def _manage_positions(self, now_ms: int) -> None:
        if self.local_only:
            return  # --simulation n'envoie jamais d'ordre, même si le même magic existe sur le compte
        self._assert_demo_account()
        mine = {p.ticket: p for p in self.broker.positions(self.spec.name) if p.magic == self.cfg.magic}
        for trade in self.store.demo_trades(SENDING):
            match = next((p for p in mine.values() if p.comment == trade.tag), None)
            if match is not None:
                self._mark_open(trade.tag, match)
            elif now_ms - trade.time_ms > 120_000:
                self.store.update(trade.tag, status=FAILED, detail="envoi incertain, aucune position trouvée")
        for trade in self.store.demo_trades(OPEN):
            position = mine.pop(trade.position, None)
            if position is None:
                self._record_exit(trade, now_ms)
            elif position.sl == 0.0:
                self._restore_stop(trade, position, now_ms)
            elif now_ms >= trade.deadline_ms:
                self._close(position, trade, now_ms, "durée max")
        for position in mine.values():
            self._close(position, None, now_ms, "position inconnue de l'état")

    def _exit_price(self, position: Position) -> float:
        tick = self.broker.tick(self.spec.name)
        return tick.bid if position.type == C.POSITION_TYPE_BUY else tick.ask

    def _fees(self, deals_in: float, volume: float) -> float:
        """Commissions et frais de l'entrée (lus dans les deals) + commission de sortie attendue."""
        return deals_in - self.exit_commission_per_lot * volume

    def _close(self, position: Position, trade: DemoTrade | None, now_ms: int, reason: str) -> None:
        """Clôture au marché ; la sortie est enregistrée au passage suivant, une fois la position disparue."""
        self._assert_demo_account()
        if not self._is_open(now_ms):
            return  # marché fermé : le stop serveur reste en place, nouvel essai à la réouverture
        last = self._close_attempts.get(position.ticket)
        if last is not None and now_ms - last < CLOSE_RETRY_MS:
            return
        first_try = last is None
        self._close_attempts[position.ticket] = now_ms
        if trade is None and first_try:
            self.say(f"{self._clock(now_ms)} position {position.ticket} de l'expérience inconnue de l'état : fermeture")
        order_type = C.ORDER_TYPE_BUY if position.type == C.POSITION_TYPE_BUY else C.ORDER_TYPE_SELL
        booked = sum(
            d.commission + d.fee for d in self.broker.deals_for_position(position.ticket) if d.entry == C.DEAL_ENTRY_IN
        )
        gross = self.broker.calc_profit(
            order_type, position.symbol, position.volume, position.price_open, self._exit_price(position)
        )
        estimate = gross + position.swap + self._fees(booked, position.volume)
        request = {
            "action": C.TRADE_ACTION_DEAL,
            "symbol": position.symbol,
            "volume": position.volume,
            "type": C.ORDER_TYPE_SELL if order_type == C.ORDER_TYPE_BUY else C.ORDER_TYPE_BUY,
            "position": position.ticket,
            "deviation": 20,
            "magic": self.cfg.magic,
            "comment": position.comment,
            "type_time": C.ORDER_TIME_GTC,
        }
        try:
            send_market_order(self.broker, request, filling_candidates(self.spec.filling_mode), sleep=self.sleep)
        except (OrderRejected, BrokerError) as exc:
            if first_try:
                self.say(f"   clôture de {position.ticket} impossible ({exc}) : le stop serveur reste en place, "
                         f"nouvel essai toutes les {CLOSE_RETRY_MS // 1000} s")  # fmt: skip
            log.info("clôture de %s impossible : %s", position.ticket, exc)
            return
        if trade is not None:
            self.store.update(trade.tag, est_pnl=estimate, exit_reason=reason)

    def _restore_stop(self, trade: DemoTrade, position: Position, now_ms: int) -> None:
        self._assert_demo_account()
        last = self._stop_attempts.get(position.ticket)
        if not self._is_open(now_ms) or (last is not None and now_ms - last < CLOSE_RETRY_MS):
            return  # marché fermé ou essai récent
        self._stop_attempts[position.ticket] = now_ms
        result = self.broker.order_send(
            {
                "action": C.TRADE_ACTION_SLTP,
                "symbol": position.symbol,
                "position": position.ticket,
                "sl": trade.sl,
                "tp": trade.tp,
                "magic": self.cfg.magic,
            }
        )
        if result.retcode == C.TRADE_RETCODE_DONE:
            self.say(f"{self._clock(now_ms)} stop manquant reposé sur {position.ticket} à {trade.sl:.2f}")
        else:
            self.say(f"{self._clock(now_ms)} stop impossible à reposer sur {position.ticket} : fermeture")
            self._close(position, trade, now_ms, "stop manquant")

    def _record_exit(self, trade: DemoTrade, now_ms: int) -> None:
        deals = self.broker.deals_for_position(trade.position)
        exits = [d for d in deals if d.entry in (C.DEAL_ENTRY_OUT, C.DEAL_ENTRY_OUT_BY)]
        if not exits:
            return  # historique pas encore à jour : relu au prochain passage
        real = sum(d.profit + d.commission + d.swap + d.fee for d in deals)
        price = sum(d.price * d.volume for d in exits) / sum(d.volume for d in exits)
        row = self.store.signal(trade.tag)
        # Clôture par le bot : motif et estimation notés juste avant l'envoi. Sinon : stop, objectif…
        reason = row.get("exit_reason") or _DEAL_REASONS.get(exits[-1].reason, "clôture externe")
        estimate = row.get("est_pnl")
        if estimate is None:
            order_type = C.ORDER_TYPE_BUY if trade.side == LONG else C.ORDER_TYPE_SELL
            level = trade.sl if reason == "stop" else trade.tp
            booked = sum(d.commission + d.fee for d in deals if d.entry == C.DEAL_ENTRY_IN)
            estimate = self.broker.calc_profit(order_type, self.spec.name, trade.volume, trade.open_price, level)
            estimate += self._fees(booked, trade.volume)
        self.store.update(trade.tag, status=CLOSED, exit_price=price, exit_reason=reason, real_pnl=real,
                          est_pnl=estimate, closed_ms=now_ms)  # fmt: skip
        entries = [d.time_msc for d in deals if d.entry == C.DEAL_ENTRY_IN]
        held = f" après {(max(d.time_msc for d in exits) - min(entries)) / 1000:.0f} s" if entries else ""
        self.say(
            f"{self._clock(now_ms)} sortie {trade.tag} ({reason}){held} à {price:.2f} : estimé "
            f"{self._money(estimate)}, exécuté {self._money(real)} (écart {self._money(real - estimate)})"
        )

    def close_all(self, reason: str) -> int:
        """Ferme les positions de l'expérience (arrêt du bot) ; renvoie le nombre de positions encore ouvertes."""
        now_ms = self.server_now_ms()
        tick = self.broker.tick(self.spec.name)
        for tag, (trade, _, _) in list(self.shadow.items()):
            price = tick.bid - trade.slip if trade.side > 0 else tick.ask + trade.slip
            trade._close(now_ms, price, reason)
            pnl = trade.move * trade.volume * self.spec.trade_contract_size * self.value_per_dollar_oz
            self.store.close_sim(tag, price, reason, pnl, now_ms)
            del self.shadow[tag]
        if self.local_only:
            return 0
        self._assert_demo_account()
        trades = {t.position: t for t in self.store.demo_trades(OPEN)}
        for position in [p for p in self.broker.positions(self.spec.name) if p.magic == self.cfg.magic]:
            self._close_attempts.pop(position.ticket, None)
            self._close(position, trades.get(position.ticket), now_ms, reason)
        self.sleep(1.0)
        self._manage_positions(self.server_now_ms())  # sorties relevées dans les deals
        return len([p for p in self.broker.positions(self.spec.name) if p.magic == self.cfg.magic])

    # --- bilans -------------------------------------------------------------------------------------

    def status_line(self) -> str:
        now_ms = self.server_now_ms()
        tick = self.broker.tick(self.spec.name)
        counts = self.store.status_counts()
        trend = {1: "haussière", -1: "baissière"}.get(self.trend, "indécise")
        market = "marché ouvert" if self._is_open(now_ms) else "marché fermé"
        sent = sum(counts.get(status, 0) for status in (OPEN, CLOSED, SENDING, FAILED))
        return (
            f"{self._clock(now_ms)} en marche ({market}) | tendance M1 {trend} | spread {tick.ask - tick.bid:.2f} $ | "
            f"signaux : {sent} envoyés, {counts.get(NOT_SENT, 0)} simulés, {counts.get(REFUSED, 0)} refusés | "
            f"positions démo ouvertes : {counts.get(OPEN, 0)}"
        )

    def _sim_value(self, side: int, entry: float, volume: float, bid: float, ask: float) -> float:
        """Résultat latent d'un trade simulé s'il était fermé maintenant (glissement et commission inclus)."""
        slip = self.cfg.extra_slippage_points * self.spec.point
        exit_price = bid - slip if side == LONG else ask + slip
        move = (exit_price - entry) * side - self.fee_per_oz
        return move * volume * self.spec.trade_contract_size * self.value_per_dollar_oz

    def reports(self) -> list[str]:
        account = self.broker.account()
        tick = self.broker.tick(self.spec.name)
        positions = [p for p in self.broker.positions(self.spec.name) if p.magic == self.cfg.magic]
        demo = self.store.closed_demo_results()
        sims = self.store.sim_results()
        open_sims = self.store.open_sims()
        sim_latent = sum(self._sim_value(side, entry, volume, tick.bid, tick.ask) for side, entry, volume in open_sims)
        start = float(self.store.meta("start_equity") or account.equity)
        currency = self.currency

        def block(title: str, results: list[float], open_count: int, open_value: float, value: float) -> list[str]:
            gains = sum(r for r in results if r > 0)
            losses = sum(r for r in results if r <= 0)
            wins = sum(1 for r in results if r > 0)
            rate = f"{wins / len(results):.0%}" if results else "-"
            return [
                title,
                f"  trades fermés : {len(results)} (gagnants {rate})",
                f"  gains fermés {gains:+.2f} {currency} | pertes fermées {losses:+.2f} {currency} | "
                f"net {gains + losses:+.2f} {currency}",
                f"  positions ouvertes : {open_count} (latent {open_value:+.2f} {currency})",
                f"  valeur du compte : {value:.2f} {currency} (départ {start:.2f})",
            ]

        floating = sum(p.profit + p.swap for p in positions)
        lines = block("=== Bilan 1 : exécutions démo (valeur = equity du compte entier) ===", demo, len(positions),
                      floating, account.equity)  # fmt: skip
        lines += block(
            f"=== Bilan 2 : simulation avec glissement supplémentaire (+{self.cfg.extra_slippage_points:g} points), "
            "tous les signaux acceptés ===",
            sims,
            len(open_sims),
            sim_latent,
            start + sum(sims) + sim_latent,
        )
        return lines
