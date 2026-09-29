"""Ordre de test, compte DÉMO uniquement (CLAUDE.md §7).

Ouvre 0,01 lot à l'achat avec SL et TP côté serveur, resserre le SL (jamais l'inverse,
règle 4), ferme la position, puis lit les deals pour relever la commission réelle.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import AccountState, Broker, BrokerError, Deal, Position, SymbolSpec
from goldbot.config import TestOrderConfig
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.execution.orders import (
    UNCERTAIN,
    OrderRejected,
    describe_retcode,
    filling_candidates,
    round_to_tick,
    select_filling,
    send_market_order,
)
from goldbot.risk.guards import TradingRefused, ensure_demo_account

_EXIT_ENTRIES = (C.DEAL_ENTRY_OUT, C.DEAL_ENTRY_OUT_BY)


class TestOrderRefused(Exception):
    """Conditions non réunies : aucun ordre n'a été envoyé."""

    __test__ = False


class TestOrderFailed(Exception):
    """Échec pendant l'ordre de test ; le message dit si une position peut rester ouverte."""

    __test__ = False


@dataclass
class TestOrderReport:
    __test__ = False

    symbol: str
    volume: float
    filling: int
    requested_price: float
    entry_price: float
    spread_points: float
    sl_initial: float
    tp: float
    sl_tightened: float | None = None
    exit_price: float | None = None
    deals: list[Deal] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    point: float = 0.01

    @property
    def entry_slippage_points(self) -> float:
        """Achat : positif = exécuté plus cher que le prix demandé."""
        return (self.entry_price - self.requested_price) / self.point

    def _sum(self, attribute: str, entries: tuple[int, ...] | None = None) -> float:
        return sum(getattr(d, attribute) for d in self.deals if entries is None or d.entry in entries)

    @property
    def commission_in(self) -> float:
        return self._sum("commission", (C.DEAL_ENTRY_IN,))

    @property
    def commission_out(self) -> float:
        return self._sum("commission", _EXIT_ENTRIES)

    @property
    def commission_per_lot_round_trip(self) -> float:
        """Coût de commission aller-retour pour 1 lot (positif = coût)."""
        cost = -(self.commission_in + self.commission_out) / self.volume
        return cost + 0.0  # -0.0 + 0.0 == 0.0 : évite d'afficher « -0.00 »

    @property
    def net_result(self) -> float:
        return sum(d.profit + d.commission + d.swap + d.fee for d in self.deals)


def _find_position(
    broker: Broker, symbol: str, magic: int, comment: str, order: int, sleep: Callable[[float], None]
) -> Position | None:
    for _ in range(10):
        mine = [
            p
            for p in broker.positions(symbol)
            if p.magic == magic and (p.comment == comment or p.ticket == order or p.identifier == order)
        ]
        if mine:
            return max(mine, key=lambda p: p.time_msc)
        sleep(0.5)
    return None


def _exit_deals(broker: Broker, position_ticket: int, sleep: Callable[[float], None]) -> list[Deal]:
    deals: list[Deal] = []
    for _ in range(10):
        deals = broker.deals_for_position(position_ticket)
        if any(d.entry in _EXIT_ENTRIES for d in deals):
            break
        sleep(0.5)
    return deals


def run_test_order(
    broker: Broker,
    *,
    spec: SymbolSpec,
    account: AccountState,
    magic: int,
    config: TestOrderConfig,
    rule: ServerTimeRule,
    schedule: MarketSchedule,
    now_utc: datetime,
    sleep: Callable[[float], None] = time.sleep,
) -> TestOrderReport:
    try:
        ensure_demo_account(account)
    except TradingRefused as exc:
        raise TestOrderRefused(str(exc)) from exc
    if not schedule.is_open(rule.to_server(now_utc)):
        raise TestOrderRefused("marché fermé : relance l'ordre de test pendant les heures de cotation")
    volume = config.volume
    if not spec.volume_min <= volume <= spec.volume_max:
        raise TestOrderRefused(f"volume {volume} hors des limites du symbole ({spec.volume_min}-{spec.volume_max})")
    if account.margin_mode != C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING and broker.positions(spec.name):
        raise TestOrderRefused(
            "compte en netting avec une position ouverte sur ce symbole : l'ordre de test s'y mélangerait"
        )

    tick = broker.tick(spec.name)
    spread = tick.ask - tick.bid
    tick_size = spec.trade_tick_size or spec.point  # garde-fou si le broker annonçait 0
    # Distance minimale imposée par le broker (stops level + freeze level), plus le spread et une marge.
    min_distance = (spec.trade_stops_level + spec.trade_freeze_level) * spec.point + spread + 2 * tick_size
    distance = max(config.stop_distance, 2 * min_distance)
    sl = round_to_tick(tick.bid - distance, tick_size, spec.digits, "down")
    tp = round_to_tick(tick.ask + distance, tick_size, spec.digits, "up")
    comment = f"goldbot-test-{int(now_utc.timestamp()) % 1_000_000:06d}"
    request = {
        "action": C.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": float(volume),
        "type": C.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": sl,
        "tp": tp,
        "deviation": config.deviation_points,
        "magic": magic,
        "comment": comment,
        "type_time": C.ORDER_TIME_GTC,
    }
    filling, _ = select_filling(broker, request, spec.filling_mode)
    candidates = filling_candidates(spec.filling_mode)
    fillings = candidates[candidates.index(filling) :]

    order_ticket = 0
    try:
        opened = send_market_order(broker, request, fillings, sleep=sleep)
        order_ticket = opened.order
    except OrderRejected as exc:
        if exc.retcode not in UNCERTAIN:
            raise TestOrderFailed(f"ouverture refusée : {exc}") from exc
        opened = None
    except BrokerError:
        opened = None  # état incertain : on cherche la position avant de conclure

    position = _find_position(broker, spec.name, magic, comment, order_ticket, sleep)
    if position is None:
        if opened is None:
            raise TestOrderFailed(
                "ouverture incertaine et aucune position trouvée : vérifie l'onglet Trade de MT5"
            )
        raise TestOrderFailed(
            "ordre exécuté mais position introuvable : vérifie l'onglet Trade de MT5 (le SL serveur est en place)"
        )

    report = TestOrderReport(
        symbol=spec.name,
        volume=position.volume,
        filling=fillings[0],
        requested_price=tick.ask,
        entry_price=position.price_open,
        spread_points=spread / spec.point,
        sl_initial=position.sl,
        tp=position.tp,
        point=spec.point,
    )
    try:
        # Resserrer le SL (le rapprocher du prix) en respectant la distance minimale du broker.
        fresh = broker.tick(spec.name)
        target = position.sl + distance / 2
        new_sl = round_to_tick(min(target, fresh.bid - min_distance), tick_size, spec.digits, "down")
        if new_sl > position.sl:
            modified = broker.order_send(
                {
                    "action": C.TRADE_ACTION_SLTP,
                    "symbol": spec.name,
                    "position": position.ticket,
                    "sl": new_sl,
                    "tp": position.tp,  # toujours renvoyer le TP, sinon il serait supprimé
                    "magic": magic,
                }
            )
            if modified.retcode == C.TRADE_RETCODE_DONE:
                report.sl_tightened = new_sl
            else:
                report.notes.append(f"modification du SL refusée : {describe_retcode(modified.retcode)}")
        else:
            report.notes.append("SL non resserré : le prix est trop proche du SL")
    finally:
        close_request = {
            "action": C.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": position.volume,
            "type": C.ORDER_TYPE_SELL,
            "position": position.ticket,
            "deviation": config.deviation_points,
            "magic": magic,
            "comment": comment,
            "type_time": C.ORDER_TIME_GTC,
        }
        try:
            closed = send_market_order(broker, close_request, fillings, sleep=sleep)
        except (OrderRejected, BrokerError) as exc:
            raise TestOrderFailed(
                f"clôture impossible ({exc}) : ferme la position à la main dans MT5 (le SL serveur est en place)"
            ) from exc
    report.deals = _exit_deals(broker, position.ticket, sleep)
    exits = [d for d in report.deals if d.entry in _EXIT_ENTRIES]
    if exits:
        # Le prix réel est dans les deals : sur le compte démo Axi, la réponse d'order_send
        # à la clôture au marché contenait price = 0.0 (docs/RESEARCH.md, annexe D).
        report.exit_price = sum(d.price * d.volume for d in exits) / sum(d.volume for d in exits)
    else:
        report.exit_price = closed.price or None
        report.notes.append("deal de sortie pas encore visible dans l'historique : commission de sortie incomplète")
    if report.commission_in or report.commission_out:
        # Deux deals arrondis au centime : erreur maximale de 0,01 sur le total, rapportée à 1 lot.
        report.notes.append(
            f"si le broker arrondit les commissions au centime, la commission par lot est précise "
            f"à ±{0.01 / report.volume:.2f} près avec {report.volume:g} lot"
        )
    return report
