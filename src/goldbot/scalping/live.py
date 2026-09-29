"""Expérience de scalping en direct, compte DÉMO uniquement (séparée du bot principal).

Toutes les ~0,5 s : nouveaux ticks -> bougies de 5 s -> signaux des stratégies actives (engine.py) ->
plan -> règles d'entrée communes avec le rejeu (policy.py) -> conditions du compte (marge, volume,
saturation du broker) -> envoi au broker, stop et objectif côté serveur dans la requête. Puis gestion des
positions : sortie forcée à la durée maximale (même en perte), stop manquant reposé, positions disparues
relevées dans les deals, doublons et positions inconnues fermés.
Une simulation parallèle rejoue chaque signal accepté avec un glissement supplémentaire (bilan séparé).
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

import pandas as pd

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError, Deal, Position, SymbolSpec
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.config import ScalpingConfig, Settings
from goldbot.data.history import bars_frame
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.execution.orders import (
    UNCERTAIN,
    OrderRejected,
    filling_candidates,
    find_entry_deal,
    select_filling,
    send_market_order,
)
from goldbot.indicators.core import atr as atr_series
from goldbot.risk.sizing import position_size
from goldbot.scalping.engine import (
    BREAKOUT,
    LONG,
    PULLBACK,
    STRATEGY_NAMES,
    Candle,
    CandleBuilder,
    Plan,
    Setup,
    SimTrade,
    ema_trend,
    make_detectors,
    plan_trade,
)
from goldbot.scalping.learning import LearningBook
from goldbot.scalping.policy import EntryPolicy, Exposure, split_volume
from goldbot.scalping.store import (
    CLOSED,
    FAILED,
    NOT_SENT,
    OPEN,
    REFUSED,
    SENDING,
    SENT,
    Order,
    ScalpStore,
    params_hash,
)

log = logging.getLogger("goldbot.scalp")
_EPOCH = datetime(1970, 1, 1)
WARMUP_S = 120  # ticks relus au démarrage pour remplir les fenêtres des détecteurs (sans trader dessus)
CLOCK_GRACE_MS = 1000  # une bougie n'est close par l'horloge qu'une seconde après sa fin (ticks en retard)
CLOSE_RETRY_MS = 5000  # délai entre deux tentatives de clôture d'une même position
BROKER_PAUSE_MS = 60_000  # après « trop de requêtes » (10024), plus d'envoi pendant 60 s
DUPLICATES = "doublons_evites"
LEARNER = "apprentissage"  # état de l'apprentissage dans la table meta (JSON)
_DEAL_REASONS = {
    C.DEAL_REASON_SL: "stop",
    C.DEAL_REASON_TP: "objectif",
    C.DEAL_REASON_SO: "stop out",
    C.DEAL_REASON_CLIENT: "clôture manuelle",
    C.DEAL_REASON_MOBILE: "clôture manuelle",
    C.DEAL_REASON_WEB: "clôture manuelle",
}


def strategy_label(code: str, config: ScalpingConfig, version: int, digest: str) -> str:
    """« cassure v1 + apprentissage v1 · 1a2b3c4d » : nom, versions et empreinte des réglages."""
    learning = f" + apprentissage v{config.learning.version}" if config.learning.enabled else ""
    cadence = f" + cadence v{config.cadence.version}" if config.cadence.enabled else ""
    return f"{STRATEGY_NAMES[code]} v{version}{learning}{cadence} · {digest}"


def strategy_versions(config: ScalpingConfig) -> dict[str, tuple[int, str, dict[str, object]]]:
    """Code -> (version, empreinte, réglages) des stratégies actives : leurs règles et les réglages communs."""
    common = config.model_dump(exclude={"breakout", "pullback", "experiment_days"})
    versions = {}
    for code, section in ((BREAKOUT, config.breakout), (PULLBACK, config.pullback)):
        if section.enabled:
            params = {"strategy": STRATEGY_NAMES[code], "rules": section.model_dump(), "common": common}
            versions[code] = (section.version, params_hash(params), params)
    return versions


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
        self.rule = ServerTimeRule.from_config(settings.server_time)
        self.schedule = MarketSchedule.from_config(settings.market_hours)
        self.zone = settings.bot.display_timezone
        self.builder = CandleBuilder(self.cfg.candle_seconds)
        self.detectors = make_detectors(self.cfg)
        self.versions = strategy_versions(self.cfg)
        self.labels = {code: strategy_label(code, self.cfg, v, h) for code, (v, h, _) in self.versions.items()}
        self.labels["VERIF"] = "vérification"
        self.policy = EntryPolicy(self.cfg)
        self.cursor_ms: int | None = None
        self._at_cursor: set[tuple[int, float, float]] = set()
        self.trading_from_ms: int | None = None  # pas de trade sur les ticks de préchauffage
        self.trend = 0
        self.atr = 0.0
        self._context_minute: int | None = None
        # Trades simulés en cours : tag -> (trade, exposition, envoyé au broker ?, stratégie).
        self.shadow: dict[str, tuple[SimTrade, Exposure, bool, str]] = {}
        self.pause_until_ms = -(2**62)
        self.day: int | None = None
        self.day_start_ms = 0
        account = broker.account()
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
        # Apprentissage à chaque trade (même code que le rejeu), état repris après un redémarrage.
        self.book: LearningBook | None = None
        self._learning_dirty = False
        self._best_said: dict[str, str] = {}
        if self.cfg.learning.enabled:
            self.book = LearningBook(self.cfg, point=spec.point, digits=spec.digits, fee_per_oz=self.fee_per_oz)
            saved = store.meta(LEARNER)
            if saved:
                self.book.learner.load(json.loads(saved))
                for key in self.book.learner.state:
                    best = self.book.learner.best(key)
                    if best:
                        self._best_said[key] = best[0]
        # Cadence liée au bénéfice : reconstruite à partir des trades fermés enregistrés (reprise après redémarrage).
        self._cadence_fed: set[str] = set()  # trades déjà donnés à la cadence (par identifiant, jamais deux fois)
        self._update_cadence(None)

    # --- affichage ----------------------------------------------------------------------------

    def _clock(self, server_ms: int) -> str:
        utc = self.rule.server_to_utc(_EPOCH + timedelta(milliseconds=server_ms))
        return pd.Timestamp(utc).tz_convert(self.zone).strftime("%H:%M:%S")

    def _money(self, value: float) -> str:
        return f"{value:+.2f} {self.currency}"

    def _to_account(self, dollars_per_oz: float, volume: float) -> float:
        return dollars_per_oz * volume * self.spec.trade_contract_size * self.value_per_dollar_oz

    # --- boucle -------------------------------------------------------------------------------

    def server_now_ms(self) -> int:
        return int(self.rule.server_epoch(self.now_utc()) * 1000)

    def _is_open(self, server_ms: int) -> bool:
        return self.schedule.is_open(_EPOCH + timedelta(milliseconds=server_ms))

    def step(self) -> None:
        self.supervisor.ensure_connected()
        now_ms = self.server_now_ms()
        self._new_day(now_ms)
        self._refresh_context(now_ms)
        if self.trading_from_ms is None:
            self.trading_from_ms = now_ms
        for time_ms, bid, ask in self._new_ticks(now_ms):
            self._advance_shadow(time_ms, bid, ask)
            self._advance_learning(time_ms, bid, ask)
            if not self._is_open(time_ms):
                continue
            for candle in self.builder.add(time_ms, bid, ask):
                self._on_candle(candle, now_ms)
        for candle in self.builder.close_until(now_ms - CLOCK_GRACE_MS):
            self._on_candle(candle, now_ms)
        if self.shadow or (self.book and self.book.sims):  # sans nouveau tick, la durée maximale passe quand même
            tick = self.broker.tick(self.spec.name)
            self._advance_shadow(now_ms, tick.bid, tick.ask)
            self._advance_learning(now_ms, tick.bid, tick.ask)
        if self._learning_dirty:
            self.store.set_meta(LEARNER, json.dumps(self.book.learner.to_dict()))
            self._learning_dirty = False
        self._manage_positions(now_ms)

    def _trade_results(self) -> list[tuple[str, float]]:
        """(identifiant, résultat net) de chaque trade fermé de l'expérience, dans l'ordre de clôture.

        Démo : ordres fermés regroupés par signal, une fois toutes ses parts fermées ; --simulation : trades simulés.
        """
        if self.local_only:
            return [(str(row["tag"]), float(row["pnl"])) for row in self.store.closed_sims()]
        pending = {order.tag for order in self.store.orders(OPEN, SENDING)}  # trade fractionné pas encore fini
        totals: dict[str, float] = {}
        last: dict[str, int] = {}
        for row in self.store.closed_orders():
            if row["tag"] in pending:
                continue
            totals[row["tag"]] = totals.get(row["tag"], 0.0) + float(row["real_pnl"])
            last[row["tag"]] = int(row["closed_ms"])
        return [(tag, totals[tag]) for tag in sorted(totals, key=lambda tag: (last[tag], tag))]

    def _update_cadence(self, now_ms: int | None) -> None:
        """Donne à la cadence chaque trade fermé pas encore compté (une fois chacun) ; annonce les changements."""
        for tag, pnl in self._trade_results():
            if tag in self._cadence_fed:
                continue
            self._cadence_fed.add(tag)
            message = self.policy.cadence.on_close(pnl)
            if message and now_ms is not None:
                self.say(f"{self._clock(now_ms)} {message} -> {self.policy.cadence.describe()}")

    def _context_label(self, key: str) -> str:
        name = STRATEGY_NAMES.get(key[0], key[0])
        return {"+": f"{name}, achats", "-": f"{name}, ventes"}.get(key[1:], name)

    def _advance_learning(self, time_ms: int, bid: float, ask: float) -> None:
        """Trades simulés des variantes : chaque clôture met à jour les scores ; annonce quand la meilleure change."""
        if self.book is None or not self.book.sims:
            return
        updates = self.book.on_tick(time_ms, bid, ask)
        if not updates:
            return
        self._learning_dirty = True
        for key in dict.fromkeys(key for key, _, _ in updates):
            best = self.book.learner.best(key)
            if best is None or self._best_said.get(key) == best[0]:
                continue
            self._best_said[key] = best[0]
            stop = "" if best[1] > 0 or self.cfg.learning.always_trade else " : pas de trade tant que toutes perdent"
            self.say(f"{self._clock(time_ms)} apprentissage [{self._context_label(key)}] : meilleure variante récente "
                     f"{best[0]} ({best[1]:+.2f} R){stop}")  # fmt: skip

    def _new_day(self, now_ms: int) -> None:
        day = now_ms // 86_400_000
        if day != self.day:
            self.day, self.day_start_ms = day, day * 86_400_000
            self.day_start_equity = self.broker.account().equity

    def _refresh_context(self, now_ms: int) -> None:
        """Tendance EMA et ATR sur les barres M1 clôturées, une fois par minute."""
        minute = now_ms // 60_000
        if minute == self._context_minute:
            return
        bars = bars_frame(self.broker.latest_bars(self.spec.name, 300), self.rule)
        closed = bars[(bars["time_server"] + 60) * 1000 <= now_ms]
        b, p = self.cfg.breakout, self.cfg.pullback
        self.trend = ema_trend(tuple(closed["close"].astype(float)), b.ema_fast, b.ema_slow)
        self.atr = 0.0
        if len(closed) > p.atr_period:
            value = atr_series(closed["high"], closed["low"], closed["close"], p.atr_period).iloc[-1]
            self.atr = 0.0 if pd.isna(value) else float(value)
        self._context_minute = minute

    def _new_ticks(self, now_ms: int) -> list[tuple[int, float, float]]:
        start_s = (self.cursor_ms // 1000) if self.cursor_ms is not None else now_ms // 1000 - WARMUP_S
        raw = self.broker.ticks_range(self.spec.name, start_s, now_ms // 1000 + 1, C.COPY_TICKS_ALL)
        fresh = []
        for time_ms, bid, ask in zip(raw["time_msc"].tolist(), raw["bid"].tolist(), raw["ask"].tolist(), strict=True):
            key = (time_ms, bid, ask)
            if bid <= 0 or ask <= 0 or (self.cursor_ms is not None and time_ms < self.cursor_ms):
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
        end_ms = candle.start_ms + self.builder.size_ms
        for detector in self.detectors:
            setup = detector.on_candle(candle, self.trend, self.atr)
            if setup is not None and end_ms > self.trading_from_ms:  # pas de trade sur le préchauffage
                self.consider(setup, now_ms)

    def consider(self, setup: Setup, now_ms: int) -> None:
        """Nouveau signal : décision unique. Le même signal revu (doublon accidentel) est compté, jamais rejoué."""
        if self.store.seen(setup.tag):
            self.store.bump(DUPLICATES)
            return
        self._handle(setup, now_ms)

    def _signal_fields(self, setup: Setup) -> dict[str, object]:
        version, digest, _ = self.versions[setup.strategy]
        return dict(strategy=setup.strategy, version=version, params_hash=digest, key=setup.key,
                    time_ms=setup.candle.start_ms, side=setup.side, level=setup.level, structure=setup.structure,
                    reason=setup.reason)  # fmt: skip

    def _refuse(self, setup: Setup, spread: float, reason: str) -> None:
        self.store.record_signal(setup.tag, status=REFUSED, detail=reason, spread=spread, **self._signal_fields(setup))
        self.say(f"   refus : {reason}")

    def _day_result(self) -> float:
        """Résultat du jour : démo réalisé + latent ; en --simulation, résultat simulé réalisé (comme le rejeu)."""
        if self.local_only:
            return self.store.sim_realized_since(self.day_start_ms)
        return self.store.realized_since(self.day_start_ms) + self._latent()

    def _day_realized(self) -> float:
        """Résultat réalisé du jour (démo, ou simulé en --simulation), sans le latent."""
        if self.local_only:
            return self.store.sim_realized_since(self.day_start_ms)
        return self.store.realized_since(self.day_start_ms)

    def _exposures(self) -> list[Exposure]:
        """Trades ouverts de l'expérience : ordres démo (regroupés par signal) et trades simulés."""
        exposures: dict[str, Exposure] = {}
        for order in self.store.orders(OPEN, SENDING):
            known = exposures.get(order.tag)
            risk = (known.risk if known else 0.0) + (order.risk or 0.0)
            exposures[order.tag] = Exposure(order.tag, order.key or order.tag, order.side, risk)
        for tag, (_, exposure, _, _) in self.shadow.items():
            exposures.setdefault(tag, exposure)
        return list(exposures.values())

    def _handle(self, setup: Setup, now_ms: int) -> None:
        cfg, spec = self.cfg, self.spec
        tick = self.broker.tick(spec.name)
        spread = tick.ask - tick.bid
        side_text = "ACHAT" if setup.side == LONG else "VENTE"
        self.say(f"{self._clock(now_ms)} signal {side_text} [{self.labels[setup.strategy]}] : {setup.reason}, "
                 f"spread {spread:.2f} $")  # fmt: skip
        age_ms = now_ms - (setup.candle.start_ms + self.builder.size_ms)
        if age_ms > self.builder.size_ms:
            self._refuse(setup, spread, f"signal périmé : bougie close depuis {age_ms / 1000:.0f} s")
            return
        if not self._is_open(now_ms + cfg.max_hold_s * 1000):
            self._refuse(setup, spread, f"le marché ferme avant la durée max : {cfg.max_hold_s} s")
            return
        plan = plan_trade(
            setup,
            tick.bid,
            tick.ask,
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
        variant = ""
        if self.book is not None:
            chosen, variant, why = self.book.decide(setup, plan, tick.bid, tick.ask, now_ms)
            if chosen is None:
                self._refuse(setup, spread, f"apprentissage : {why}")
                return
            if variant != self.book.learner.default.name:
                self.say(f"   apprentissage : {why}")
            plan = chosen  # sortie et sens choisis d'après les derniers résultats
        order_type = C.ORDER_TYPE_BUY if plan.side == LONG else C.ORDER_TYPE_SELL
        worst = plan.sl - plan.side * cfg.expected_slippage_points * spec.point
        loss_per_lot = -self.broker.calc_profit(order_type, spec.name, 1.0, plan.entry, worst)
        loss_per_lot += 2 * self.exit_commission_per_lot
        account = self.broker.account()
        volume = position_size(
            account.equity * cfg.risk_per_trade_pct / 100,
            loss_per_lot,
            volume_min=spec.volume_min,
            volume_max=float("inf"),  # au-delà du maximum par ordre : fractionnement en plusieurs ordres
            volume_step=spec.volume_step,
        )
        if volume == 0:
            self._refuse(setup, spread, "lot minimum au-dessus du budget de risque")
            return
        risk = volume * loss_per_lot
        reason = self.policy.refusal(
            now_ms,
            key=setup.key,
            risk=risk,
            equity=account.equity,
            day_result=self._day_result(),
            day_realized=self._day_realized(),
            day_start_equity=self.day_start_equity,
            open_trades=self._exposures(),
            side=plan.side,
        )
        if reason is None and not self.local_only:
            reason = self._account_refusal(order_type, volume, plan.entry, account.equity, account.margin_free, now_ms)
        if reason is not None:
            self._refuse(setup, spread, reason)
            return
        self.policy.accept(now_ms)
        parts = split_volume(volume, spec.volume_max, spec.volume_step)
        split = f" en {len(parts)} ordres (fractionnement)" if len(parts) > 1 else ""
        direction = ("ACHAT" if plan.side == LONG else "VENTE") + (" (signal joué à l'envers)" if plan.side != setup.side
                                                                   else "")  # fmt: skip
        details = (
            f"{direction} {volume:g} lot{split} à {plan.entry:.2f} | spread {plan.spread:.2f} | stop {plan.sl:.2f} "
            f"(-{plan.stop_distance:.2f} $) | objectif {plan.tp:.2f} (+{plan.target:.2f} $) | "
            f"durée max {cfg.max_hold_s} s | risque {self._money(-risk)}"
        )
        self._start_shadow(setup, plan, tick.bid, tick.ask, now_ms, volume, risk, sent=not self.local_only)
        fields = dict(spread=plan.spread, volume=volume, entry=plan.entry, sl=plan.sl, tp=plan.tp, risk=risk,
                      parts=len(parts), variante=variant or None, **self._signal_fields(setup))  # fmt: skip
        if self.local_only:
            self.store.record_signal(setup.tag, status=NOT_SENT, detail="mode --simulation", **fields)
            self.say(f"   non envoyé au broker (mode --simulation), simulé seulement : {details}")
            return
        if not self.store.record_signal(setup.tag, status=SENT, **fields):
            self.store.bump(DUPLICATES)
            return
        self._send(setup, plan, order_type, parts, volume, risk, now_ms, details)

    def _account_refusal(self, order_type: int, volume: float, price: float, equity: float, margin_free: float,
                         now_ms: int) -> str | None:  # fmt: skip
        """Conditions du compte avant l'envoi : broker saturé, marge libre, volume maximal du symbole."""
        if now_ms < self.pause_until_ms:
            return f"broker saturé : pause jusqu'à {self._clock(self.pause_until_ms)}"
        margin = self.broker.calc_margin(order_type, self.spec.name, volume, price)
        left = margin_free - margin
        if left < self.cfg.min_free_margin_pct / 100 * equity:
            return f"marge libre insuffisante : {left:.2f} {self.currency} après l'ordre"
        if self.spec.volume_limit > 0:
            position_type = C.POSITION_TYPE_BUY if order_type == C.ORDER_TYPE_BUY else C.POSITION_TYPE_SELL
            same_side = sum(p.volume for p in self.broker.positions(self.spec.name) if p.type == position_type)
            if same_side + volume > self.spec.volume_limit:
                return f"volume maximal du symbole atteint : {same_side + volume:g} > {self.spec.volume_limit:g} lots"
        return None

    def _start_shadow(self, setup: Setup, plan: Plan, bid: float, ask: float, now_ms: int, volume: float,
                      risk: float, sent: bool) -> None:  # fmt: skip
        slip = self.cfg.extra_slippage_points * self.spec.point
        trade = SimTrade.open(setup.tag, plan, bid, ask, now_ms, self.cfg.max_hold_s, slip, volume, self.fee_per_oz)
        exposure = Exposure(setup.tag, setup.key, plan.side, risk)
        self.shadow[setup.tag] = (trade, exposure, sent, setup.strategy)
        self.store.record_sim(setup.tag, setup.strategy, now_ms, plan.side, trade.entry, trade.sl, trade.tp, volume,
                              sent)  # fmt: skip

    def _advance_shadow(self, time_ms: int, bid: float, ask: float) -> None:
        for tag, (trade, _, sent, strategy) in list(self.shadow.items()):
            if trade.on_tick(time_ms, bid, ask):
                pnl = self._to_account(trade.move, trade.volume)
                fees = -self._to_account(trade.fee, trade.volume)
                self.store.close_sim(tag, trade.exit_price, trade.reason, pnl, fees, time_ms)
                del self.shadow[tag]
                if not sent:  # --simulation : les trades simulés sont les trades de l'essai (comme au rejeu)
                    pause = self.policy.on_exit(trade.side, trade.reason, trade.open_ms, trade.exit_ms, pnl)
                    if pause:
                        self.say(f"{self._clock(time_ms)} {pause}")
                    self._update_cadence(time_ms)
                if not sent:
                    self.say(f"{self._clock(time_ms)} sortie simulée {tag} [{self.labels.get(strategy, strategy)}] "
                             f"({trade.reason}) : {self._money(pnl)}")  # fmt: skip

    def _send(self, setup: Setup, plan: Plan, order_type: int, parts: list[float], volume: float, risk: float,
              now_ms: int, details: str) -> None:  # fmt: skip
        spec = self.spec
        fillings: list[int] | None = None
        opened: list[Position] = []
        for part, part_volume in enumerate(parts, 1):
            comment = f"{setup.tag}#{part}"
            if not self.store.record_order(setup.tag, part, comment=comment, side=plan.side, volume=part_volume,
                                           sl=plan.sl, tp=plan.tp, risk=risk * part_volume / volume,
                                           spread=plan.spread, time_ms=now_ms):  # fmt: skip
                self.store.bump(DUPLICATES)
                continue
            request = {
                "action": C.TRADE_ACTION_DEAL,
                "symbol": spec.name,
                "volume": part_volume,
                "type": order_type,
                "sl": plan.sl,
                "tp": plan.tp,
                "deviation": 20,
                "magic": self.cfg.magic,
                "comment": comment,
                "type_time": C.ORDER_TIME_GTC,
            }
            try:
                if fillings is None:
                    filling, _ = select_filling(self.broker, {**request, "price": plan.entry}, spec.filling_mode)
                    candidates = filling_candidates(spec.filling_mode)
                    fillings = candidates[candidates.index(filling) :]
                send_market_order(self.broker, request, fillings, sleep=self.sleep)
            except OrderRejected as exc:
                if exc.operation == "order_send" and exc.retcode in UNCERTAIN:
                    # Peut-être exécuté : reste « envoi », rapproché des positions au passage suivant (jamais renvoyé).
                    self.say(f"   réponse incertaine du broker ({exc}) : vérification au prochain passage")
                    break
                self.store.update_order(setup.tag, part, status=FAILED, detail=str(exc))
                if exc.retcode == C.TRADE_RETCODE_TOO_MANY_REQUESTS:
                    self.pause_until_ms = now_ms + BROKER_PAUSE_MS
                    self.say(
                        f"   le broker signale trop de requêtes : plus d'envoi pendant {BROKER_PAUSE_MS // 1000} s"
                    )
                else:
                    self.say(f"   ordre refusé par le broker : {exc}")
                break
            except BrokerError as exc:
                self.say(f"   liaison perdue pendant l'envoi ({exc}) : vérification au prochain passage")
                break
            position = self._find(comment)
            if position is None:
                self.say("   ordre envoyé, position pas encore visible : vérification au prochain passage")
                continue
            self._mark_open(setup.tag, part, position)
            opened.append(position)
        if opened:
            price = sum(p.price_open * p.volume for p in opened) / sum(p.volume for p in opened)
            self.say(f"   entrée démo [{self.labels[setup.strategy]}] : "
                     f"{details.replace(f'{plan.entry:.2f}', f'{price:.2f}', 1)}")  # fmt: skip
        statuses = {self.store.order(setup.tag, part).get("status") for part in range(1, len(parts) + 1)}
        if statuses <= {FAILED, None}:
            self.store.update_signal(setup.tag, status=FAILED)

    def _find(self, comment: str) -> Position | None:
        for _ in range(6):
            for position in self.broker.positions(self.spec.name):
                if position.magic == self.cfg.magic and position.comment == comment:
                    return position
            self.sleep(0.25)
        return None

    def _mark_open(self, tag: str, part: int, position: Position, hold_s: int | None = None) -> None:
        open_ms = int(position.time_msc)
        try:  # commission déjà prélevée à l'entrée, pour le résultat latent (0 sur un compte Standard)
            deals = self.broker.deals_for_position(position.ticket)
            entry_fees = sum(d.commission + d.fee for d in deals if d.entry == C.DEAL_ENTRY_IN)
        except BrokerError:
            entry_fees = 0.0
        hold_ms = (self.cfg.max_hold_s if hold_s is None else hold_s) * 1000
        self.store.update_order(tag, part, status=OPEN, position=position.ticket, open_price=position.price_open,
                                open_ms=open_ms, deadline_ms=open_ms + hold_ms, volume=position.volume,
                                entry_fees=entry_fees)  # fmt: skip

    def _mark_open_from_deal(self, tag: str, part: int, deal: Deal) -> None:
        """Ordre retrouvé par son deal d'entrée : ouvert (la sortie éventuelle est lue ensuite dans les deals)."""
        open_ms = int(deal.time_msc)
        self.store.update_order(tag, part, status=OPEN, position=deal.position_id, open_price=deal.price,
                                open_ms=open_ms, deadline_ms=open_ms + self.cfg.max_hold_s * 1000, volume=deal.volume,
                                entry_fees=deal.commission + deal.fee)  # fmt: skip

    # --- positions --------------------------------------------------------------------------------

    def _mine(self) -> dict[int, Position]:
        return {p.ticket: p for p in self.broker.positions(self.spec.name) if p.magic == self.cfg.magic}

    def _manage_positions(self, now_ms: int) -> None:
        mine = self._mine()
        by_comment: dict[str, list[Position]] = {}
        for position in mine.values():
            by_comment.setdefault(position.comment, []).append(position)
        for order in self.store.orders(SENDING):
            matches = by_comment.get(order.comment, [])
            if matches:
                self._mark_open(order.tag, order.part, matches[0])
                continue
            # Réponse perdue ou position déjà fermée avant d'être vue : l'historique des deals le dit.
            try:
                deal = find_entry_deal(self.broker, magic=self.cfg.magic, comment=order.comment,
                                       sent_s=order.time_ms // 1000, now_s=now_ms // 1000)  # fmt: skip
            except BrokerError as exc:
                self.say(f"   historique des deals illisible ({exc}) : ordre {order.comment} revu au prochain passage")
                continue
            if deal is not None:
                self._mark_open_from_deal(order.tag, order.part, deal)
                self.say(f"{self._clock(now_ms)} ordre {order.comment} retrouvé dans l'historique des deals "
                         f"(position {deal.position_id}, entrée à {deal.price:.2f}) : réponse du broker perdue")  # fmt: skip
            elif now_ms - order.time_ms > 120_000:
                self.store.update_order(order.tag, order.part, status=FAILED,
                                        detail="envoi incertain, aucune position ni aucun deal trouvés")  # fmt: skip
        for order in self.store.orders(OPEN):
            position = mine.pop(order.position, None)
            if position is None:
                self._record_exit(order, now_ms)
            elif position.sl == 0.0:
                self._restore_stop(order, position, now_ms)
            elif now_ms >= order.deadline_ms:
                self._close(position, order, now_ms, "durée max")
        if mine:
            known = self.store.known_comments()
            for position in mine.values():
                reason = "doublon accidentel" if position.comment in known else "position inconnue de l'état"
                self._close(position, None, now_ms, reason)

    def _exit_price(self, position: Position) -> float:
        tick = self.broker.tick(self.spec.name)
        return tick.bid if position.type == C.POSITION_TYPE_BUY else tick.ask

    def _fees(self, entry_fees: float, volume: float) -> float:
        """Commissions et frais de l'entrée (lus dans les deals) + commission de sortie attendue."""
        return entry_fees - self.exit_commission_per_lot * volume

    def _close(self, position: Position, order: Order | None, now_ms: int, reason: str) -> None:
        """Clôture au marché ; la sortie est enregistrée au passage suivant, une fois la position disparue."""
        if not self._is_open(now_ms):
            return  # marché fermé : le stop serveur reste en place, nouvel essai à la réouverture
        last = self._close_attempts.get(position.ticket)
        if last is not None and now_ms - last < CLOSE_RETRY_MS:
            return
        first_try = last is None
        self._close_attempts[position.ticket] = now_ms
        if order is None and first_try:
            self.say(f"{self._clock(now_ms)} position {position.ticket} ({position.comment}) : {reason}, fermeture")
            if reason == "doublon accidentel":
                self.store.bump(DUPLICATES)
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
        if order is not None:
            self.store.update_order(order.tag, order.part, est_pnl=estimate, exit_reason=reason)

    def _restore_stop(self, order: Order, position: Position, now_ms: int) -> None:
        last = self._stop_attempts.get(position.ticket)
        if not self._is_open(now_ms) or (last is not None and now_ms - last < CLOSE_RETRY_MS):
            return  # marché fermé ou essai récent
        self._stop_attempts[position.ticket] = now_ms
        result = self.broker.order_send(
            {
                "action": C.TRADE_ACTION_SLTP,
                "symbol": position.symbol,
                "position": position.ticket,
                "sl": order.sl,
                "tp": order.tp,
                "magic": self.cfg.magic,
            }
        )
        if result.retcode == C.TRADE_RETCODE_DONE:
            self.say(f"{self._clock(now_ms)} stop manquant reposé sur {position.ticket} à {order.sl:.2f}")
        else:
            self.say(f"{self._clock(now_ms)} stop impossible à reposer sur {position.ticket} : fermeture")
            self._close(position, order, now_ms, "stop manquant")

    def _record_exit(self, order: Order, now_ms: int) -> None:
        deals = self.broker.deals_for_position(order.position)
        exits = [d for d in deals if d.entry in (C.DEAL_ENTRY_OUT, C.DEAL_ENTRY_OUT_BY)]
        if not exits:
            return  # historique pas encore à jour : relu au prochain passage
        profit = sum(d.profit for d in deals)
        commission = sum(d.commission for d in deals)
        swap = sum(d.swap for d in deals)
        fee = sum(d.fee for d in deals)
        real = profit + commission + swap + fee
        price = sum(d.price * d.volume for d in exits) / sum(d.volume for d in exits)
        row = self.store.order(order.tag, order.part)
        # Clôture par le bot : motif et estimation notés juste avant l'envoi. Sinon : stop, objectif…
        reason = row.get("exit_reason") or _DEAL_REASONS.get(exits[-1].reason, "clôture externe")
        estimate = row.get("est_pnl")
        if estimate is None:
            order_type = C.ORDER_TYPE_BUY if order.side == LONG else C.ORDER_TYPE_SELL
            level = order.sl if reason == "stop" else order.tp
            booked = sum(d.commission + d.fee for d in deals if d.entry == C.DEAL_ENTRY_IN)
            estimate = self.broker.calc_profit(order_type, self.spec.name, order.volume, order.open_price, level)
            estimate += self._fees(booked, order.volume)
        entries = [d.time_msc for d in deals if d.entry == C.DEAL_ENTRY_IN]
        open_ms = min(entries) if entries else order.open_ms
        exit_ms = max(d.time_msc for d in exits)
        self.store.update_order(order.tag, order.part, status=CLOSED, exit_price=price, exit_reason=reason,
                                exit_ms=exit_ms, open_ms=open_ms, est_pnl=estimate, profit=profit,
                                commission=commission, swap=swap, fee=fee, real_pnl=real, closed_ms=now_ms)  # fmt: skip
        if open_ms:
            pause = self.policy.on_exit(order.side, reason, open_ms, exit_ms, real)
            if pause:
                self.say(f"{self._clock(now_ms)} {pause}")
        self._update_cadence(now_ms)
        held = f" après {(exit_ms - open_ms) / 1000:.0f} s" if open_ms else ""
        self.say(
            f"{self._clock(now_ms)} sortie {order.label} [{self.labels.get(order.strategy, order.strategy)}] "
            f"({reason}){held} à {price:.2f} : estimé {self._money(estimate)}, exécuté {self._money(real)} "
            f"(écart {self._money(real - estimate)}, frais {self._money(commission + swap + fee)})"
        )
        self.say(f"   {self.running_total()}")

    # --- résultats ----------------------------------------------------------------------------------

    def _latent(self, positions: dict[int, Position] | None = None) -> float:
        """Résultat latent des positions ouvertes de l'expérience (commission d'entrée comprise)."""
        positions = self._mine() if positions is None else positions
        entry_fees = {o.position: o.entry_fees or 0.0 for o in self.store.orders(OPEN)}
        return sum(p.profit + p.swap + entry_fees.get(p.ticket, 0.0) for p in positions.values())

    def running_total(self) -> str:
        positions = self._mine()
        realized = self.store.realized_since(0)
        latent = self._latent(positions)
        return (
            f"essai démo : réalisé {self._money(realized)} | latent {self._money(latent)} "
            f"({len(positions)} position(s) ouverte(s)) | total {self._money(realized + latent)}"
        )

    def status_line(self) -> str:
        now_ms = self.server_now_ms()
        tick = self.broker.tick(self.spec.name)
        counts = self.store.status_counts()
        trend = {1: "haussière", -1: "baissière"}.get(self.trend, "indécise")
        market = "marché ouvert" if self._is_open(now_ms) else "marché fermé"
        return (
            f"{self._clock(now_ms)} en marche ({market}) | tendance M1 {trend} | ATR M1 {self.atr:.2f} $ | "
            f"spread {tick.ask - tick.bid:.2f} $ | signaux : {counts.get(SENT, 0)} envoyés, "
            f"{counts.get(NOT_SENT, 0)} simulés, {counts.get(REFUSED, 0)} refusés | {self.running_total()} | "
            f"{self.policy.cadence.describe()}"
        )

    def _block(self, label: str, trades: dict[str, dict[str, float]], durations: list[float], reasons: Counter,
               open_count: int, latent: float, spread_cost: float | None = None) -> list[str]:  # fmt: skip
        results = [t["pnl"] for t in trades.values()]
        gains = sum(r for r in results if r > 0)
        losses = sum(r for r in results if r <= 0)
        fees = sum(t["fees"] for t in trades.values())
        wins = sum(1 for r in results if r > 0)
        rate = f"{wins / len(results) * 100:.0f} %" if results else "-"
        spread = f" ; spread payé {spread_cost:.2f}, déjà dans les prix" if spread_cost is not None else ""
        lasting = (
            f"durée moyenne {sum(durations) / len(durations):.0f} s (de {min(durations):.0f} à {max(durations):.0f} s)"
            if durations
            else "durée -"
        )
        exits = ", ".join(f"{reason} {count}" for reason, count in reasons.most_common()) or "-"
        return [
            f"  [{label}] trades fermés {len(results)} (gagnants {rate}) | gains réalisés {gains:+.2f} | "
            f"pertes réalisées {losses:+.2f} | net réalisé {gains + losses:+.2f} (dont frais {fees:+.2f}{spread})",
            f"      positions ouvertes {open_count} (latent {latent:+.2f}) | résultat total {gains + losses + latent:+.2f}"
            f" | {lasting} | sorties : {exits}",
        ]

    def reports(self) -> list[str]:
        account = self.broker.account()
        tick = self.broker.tick(self.spec.name)
        positions = self._mine()
        open_orders = [o for o in self.store.orders(OPEN) if o.strategy != "VERIF"]
        closed = self.store.closed_orders()
        start = float(self.store.meta("start_equity") or account.equity)
        codes = sorted({*self.versions, *(r["strategy"] for r in closed), *(o.strategy for o in open_orders)})
        lines = [f"=== Bilan 1 : exécutions démo (montants en {self.currency}) ==="]
        for code in [*codes, None]:
            rows = [r for r in closed if code is None or r["strategy"] == code]
            trades: dict[str, dict[str, float]] = {}
            for r in rows:
                trade = trades.setdefault(r["tag"], {"pnl": 0.0, "fees": 0.0})
                trade["pnl"] += r["real_pnl"]
                trade["fees"] += (r["commission"] or 0.0) + (r["swap"] or 0.0) + (r["fee"] or 0.0)
            durations = [(r["exit_ms"] - r["open_ms"]) / 1000 for r in rows if r["exit_ms"] and r["open_ms"]]
            reasons = Counter(r["exit_reason"] for r in rows)
            spread_cost = sum(self._to_account((r["spread"] or 0.0), r["volume"]) for r in rows)
            orders = [o for o in open_orders if code is None or o.strategy == code]
            latent = self._latent({o.position: positions[o.position] for o in orders if o.position in positions})
            label = self.labels.get(code, code) if code is not None else "total"
            lines += self._block(label, trades, durations, reasons, len(orders), latent, spread_cost)
        split = len({r["tag"] for r in closed if r["part"] > 1})
        lines.append(
            f"  doublons accidentels évités : {self.store.meta(DUPLICATES) or 0} | trades fractionnés : {split}"
        )
        lines.append(f"  valeur du compte (equity, compte entier) : {account.equity:.2f} {self.currency} "
                     f"(départ {start:.2f})")  # fmt: skip
        lines.append(f"  {self.policy.cadence.describe()}")
        lines.append(
            f"=== Bilan 2 : simulation avec glissement supplémentaire (+{self.cfg.extra_slippage_points:g} points), "
            "tous les signaux acceptés ==="
        )
        sims, open_sims = self.store.closed_sims(), self.store.open_sims()
        slip = self.cfg.extra_slippage_points * self.spec.point
        sim_total = 0.0
        for code in [*sorted({*codes, *(s["strategy"] for s in sims)}), None]:
            rows = [s for s in sims if code is None or s["strategy"] == code]
            trades = {s["tag"]: {"pnl": s["pnl"], "fees": s["fees"] or 0.0} for s in rows}
            durations = [(s["closed_ms"] - s["time_ms"]) / 1000 for s in rows]
            pending = [s for s in open_sims if code is None or s["strategy"] == code]
            latent = 0.0
            for s in pending:
                exit_price = tick.bid - slip if s["side"] == LONG else tick.ask + slip
                latent += self._to_account((exit_price - s["entry"]) * s["side"] - self.fee_per_oz, s["volume"])
            label = self.labels.get(code, code) if code is not None else "total"
            lines += self._block(
                label, trades, durations, Counter(s["exit_reason"] for s in rows), len(pending), latent
            )
            if code is None:
                sim_total = sum(t["pnl"] for t in trades.values()) + latent
        lines.append(f"  valeur simulée du compte : {start + sim_total:.2f} {self.currency} (départ {start:.2f})")
        if self.book is not None:
            learner = self.book.learner
            lines.append(
                f"=== Apprentissage : score récent des variantes (demi-vie {learner.half_life:g} trades simulés, "
                f"jugées après {learner.min_trades}) ==="
            )
            if not learner.state:
                lines.append("  pas encore de trade simulé fermé")
            for key in sorted(learner.state):
                counts = {name: int(score[2]) for name, score in learner.state[key].items()}
                best = learner.best(key)
                base = learner.scores(key).get(learner.default.name)
                best_text = f"meilleure : {best[0]} ({best[1]:+.2f} R)" if best else "pas encore assez de trades"
                base_text = f" | règles de départ {base:+.2f} R" if base is not None else ""
                lines.append(
                    f"  [{self._context_label(key)}] {best_text}{base_text} | {max(counts.values())} signaux simulés"
                )
        return lines

    def close_all(self, reason: str) -> int:
        """Ferme les positions de l'expérience (arrêt du bot) ; renvoie le nombre de positions encore ouvertes."""
        now_ms = self.server_now_ms()
        by_ticket = {o.position: o for o in self.store.orders(OPEN)}
        for position in self._mine().values():
            self._close_attempts.pop(position.ticket, None)
            self._close(position, by_ticket.get(position.ticket), now_ms, reason)
        self.sleep(1.0)
        self._manage_positions(self.server_now_ms())  # sorties relevées dans les deals
        return len(self._mine())
