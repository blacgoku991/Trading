"""Broker factice en mémoire pour les tests unitaires (aucune dépendance à MT5).

Il reproduit le strict nécessaire de MT5 : ouverture et clôture au marché, modification
SL/TP, deals avec commission, erreurs injectables. Les valeurs par défaut des
fabriques sont plausibles mais fictives.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import (
    AccountState,
    Broker,
    BrokerError,
    CheckResult,
    Deal,
    Position,
    SymbolSpec,
    TerminalState,
    Tick,
    TradeResult,
)


def make_terminal(**overrides: Any) -> TerminalState:
    values = dict(
        connected=True,
        trade_allowed=True,
        tradeapi_disabled=False,
        maxbars=100_000_000,
        ping_last=35_000,
        build=5300,
        company="Fake Broker Ltd",
        path=r"C:\MT5\terminal64.exe",
        data_path=r"C:\MT5\data",
        commondata_path=r"C:\MT5\common",
    )
    values.update(overrides)
    return TerminalState(**values)


def make_account(**overrides: Any) -> AccountState:
    values = dict(
        login=12345678,
        server="Fake-Demo",
        company="Fake Broker Ltd",
        currency="USD",
        trade_mode=C.ACCOUNT_TRADE_MODE_DEMO,
        margin_mode=C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING,
        leverage=100,
        trade_allowed=True,
        trade_expert=True,
        margin_so_mode=C.ACCOUNT_STOPOUT_MODE_PERCENT,
        margin_so_call=100.0,
        margin_so_so=50.0,
        balance=10_000.0,
        equity=10_000.0,
        margin=0.0,
        margin_free=10_000.0,
        margin_level=0.0,
        fifo_close=False,
        limit_orders=0,
        currency_digits=2,
    )
    values.update(overrides)
    return AccountState(**values)


def make_symbol(name: str = "XAUUSD", **overrides: Any) -> SymbolSpec:
    values = dict(
        name=name,
        path=f"Metals\\{name}",
        description="Gold vs US Dollar",
        visible=True,
        select=True,
        trade_mode=C.SYMBOL_TRADE_MODE_FULL,
        trade_exemode=C.SYMBOL_TRADE_EXECUTION_MARKET,
        trade_calc_mode=C.SYMBOL_CALC_MODE_CFD,
        digits=2,
        point=0.01,
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        trade_tick_value_profit=1.0,
        trade_tick_value_loss=1.0,
        trade_contract_size=100.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        volume_limit=0.0,
        trade_stops_level=0,
        trade_freeze_level=0,
        filling_mode=C.SYMBOL_FILLING_FOK | C.SYMBOL_FILLING_IOC,
        expiration_mode=C.SYMBOL_EXPIRATION_GTC | C.SYMBOL_EXPIRATION_DAY,
        order_mode=C.SYMBOL_ORDER_MARKET | C.SYMBOL_ORDER_STOP | C.SYMBOL_ORDER_SL | C.SYMBOL_ORDER_TP,
        order_gtc_mode=C.SYMBOL_ORDERS_GTC,
        swap_mode=C.SYMBOL_SWAP_MODE_POINTS,
        swap_long=-60.0,
        swap_short=25.0,
        swap_rollover3days=C.DAY_OF_WEEK_WEDNESDAY,
        spread=15,
        spread_float=True,
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
    )
    values.update(overrides)
    return SymbolSpec(**values, raw=dict(values))


def make_tick(bid: float, ask: float, time_msc: int) -> Tick:
    return Tick(time=time_msc // 1000, time_msc=time_msc, bid=bid, ask=ask)


def match_group(names: list[str], group: str) -> list[str]:
    """Filtre façon symbols_get(group=) : conditions appliquées dans l'ordre, « ! » exclut."""
    selected: list[str] = []
    for condition in (part.strip() for part in group.split(",")):
        if not condition:
            continue
        if condition.startswith("!"):
            selected = [name for name in selected if not fnmatch.fnmatchcase(name, condition[1:])]
        else:
            selected += [n for n in names if fnmatch.fnmatchcase(n, condition) and n not in selected]
    return selected


class FakeBroker(Broker):
    def __init__(
        self,
        *,
        terminal: TerminalState | None = None,
        account: AccountState | None = None,
        symbols: list[SymbolSpec] | None = None,
        ticks: dict[str, Tick] | None = None,
        commission_per_lot_side: float = 0.0,
    ) -> None:
        self.terminal_state = terminal or make_terminal()
        self.account_state = account or make_account()
        self._symbols = {spec.name: spec for spec in (symbols or [make_symbol()])}
        self.ticks = dict(ticks or {})
        self.commission_per_lot_side = commission_per_lot_side
        # Erreurs injectées, consommées une par appel.
        self.connect_errors: list[BrokerError] = []
        self.terminal_errors: list[BrokerError] = []
        # Retcode de order_check par mode de remplissage (défaut : accepté).
        self.check_retcodes: dict[int, int] = {}
        # Retcodes forcés pour les prochains order_send (DONE = traitement normal).
        self.send_retcodes: list[int] = []
        # Positions ouvertes mais absentes de positions() (bug simulé).
        self.hide_positions = False
        # Prochain order_send exécuté mais réponse perdue (retcode TIMEOUT) : état incertain.
        self.lose_next_reply = False
        # Comme sur le compte démo Axi : price = 0.0 dans la réponse aux ordres au marché.
        self.zero_price_in_results = False
        self.connected = False
        self.connect_calls = 0
        self.shutdown_calls = 0
        self.selected: set[str] = set()
        self.checked: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self._positions: dict[int, Position] = {}
        self._deals: list[Deal] = []
        self._next_ticket = 1000

    # --- connexion -------------------------------------------------------------

    def _require_connection(self, operation: str) -> None:
        if not self.connected:
            raise BrokerError(operation, C.RES_E_INTERNAL_FAIL_CONNECT, "No IPC connection")

    def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        self.connected = True

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.connected = False

    def version(self) -> tuple[int, int, str]:
        self._require_connection("version")
        return (500, self.terminal_state.build, "01 Sep 2026")

    def terminal(self) -> TerminalState:
        if self.terminal_errors:
            raise self.terminal_errors.pop(0)
        self._require_connection("terminal_info")
        return self.terminal_state

    def account(self) -> AccountState:
        self._require_connection("account_info")
        return self.account_state

    # --- symboles ----------------------------------------------------------------

    def symbols(self, group: str) -> list[SymbolSpec]:
        self._require_connection("symbols_get")
        return [self._symbols[name] for name in match_group(list(self._symbols), group)]

    def select_symbol(self, name: str, enable: bool = True) -> None:
        self._require_connection("symbol_select")
        if name not in self._symbols:
            raise BrokerError(f"symbol_select({name})", C.RES_E_NOT_FOUND, "symbol not found")
        (self.selected.add if enable else self.selected.discard)(name)

    def symbol(self, name: str) -> SymbolSpec:
        self._require_connection("symbol_info")
        if name not in self._symbols:
            raise BrokerError(f"symbol_info({name})", C.RES_E_NOT_FOUND, "symbol not found")
        return self._symbols[name]

    def tick(self, name: str) -> Tick:
        self._require_connection("symbol_info_tick")
        if name not in self.ticks:
            raise BrokerError(f"symbol_info_tick({name})", C.RES_E_NOT_FOUND, "no tick")
        return self.ticks[name]

    # --- ordres -----------------------------------------------------------------------

    def positions(self, symbol: str | None = None) -> list[Position]:
        self._require_connection("positions_get")
        if self.hide_positions:
            return []
        return [p for p in self._positions.values() if symbol is None or p.symbol == symbol]

    def order_check(self, request: Mapping[str, Any]) -> CheckResult:
        self._require_connection("order_check")
        self.checked.append(dict(request))
        retcode = self.check_retcodes.get(request.get("type_filling"), C.ORDER_CHECK_OK)
        account = self.account_state
        return CheckResult(
            retcode=retcode,
            balance=account.balance,
            equity=account.equity,
            profit=0.0,
            margin=0.0,
            margin_free=account.margin_free,
            margin_level=0.0,
            comment="Done" if retcode == C.ORDER_CHECK_OK else "Rejected",
        )

    def order_send(self, request: Mapping[str, Any]) -> TradeResult:
        self._require_connection("order_send")
        self.sent.append(dict(request))
        forced = self.send_retcodes.pop(0) if self.send_retcodes else C.TRADE_RETCODE_DONE
        if forced != C.TRADE_RETCODE_DONE:
            return self._result(forced, request["symbol"], 0, 0.0, 0.0, "forced by test")
        if request["action"] == C.TRADE_ACTION_DEAL:
            result = self._deal(request)
        elif request["action"] == C.TRADE_ACTION_SLTP:
            result = self._modify(request)
        else:
            result = self._result(C.TRADE_RETCODE_INVALID, request["symbol"], 0, 0.0, 0.0, "not simulated")
        if self.zero_price_in_results and request["action"] == C.TRADE_ACTION_DEAL:
            result = replace(result, price=0.0)
        if self.lose_next_reply:
            self.lose_next_reply = False
            return replace(result, retcode=C.TRADE_RETCODE_TIMEOUT, comment="reply lost")
        return result

    def _new_ticket(self) -> int:
        self._next_ticket += 1
        return self._next_ticket

    def _result(
        self, retcode: int, symbol: str, ticket: int, volume: float, price: float, comment: str
    ) -> TradeResult:
        tick = self.ticks.get(symbol)
        return TradeResult(
            retcode=retcode,
            deal=ticket,
            order=ticket,
            volume=volume,
            price=price,
            bid=tick.bid if tick else 0.0,
            ask=tick.ask if tick else 0.0,
            comment=comment,
            request_id=ticket,
            retcode_external=0,
        )

    def _deal(self, request: Mapping[str, Any]) -> TradeResult:
        symbol = request["symbol"]
        tick = self.ticks[symbol]
        spec = self._symbols[symbol]
        volume = float(request["volume"])
        order_type = request["type"]
        price = tick.ask if order_type == C.ORDER_TYPE_BUY else tick.bid
        commission = round(-self.commission_per_lot_side * volume, 2)
        ticket = self._new_ticket()
        common = dict(
            ticket=ticket,
            order=ticket,
            symbol=symbol,
            type=order_type,
            reason=C.DEAL_REASON_EXPERT,
            volume=volume,
            price=price,
            commission=commission,
            swap=0.0,
            fee=0.0,
            magic=request.get("magic", 0),
            comment=request.get("comment", ""),
            time_msc=tick.time_msc,
        )
        if "position" in request:
            position = self._positions.get(request["position"])
            if position is None:
                return self._result(C.TRADE_RETCODE_POSITION_CLOSED, symbol, 0, 0.0, 0.0, "Position closed")
            direction = 1 if position.type == C.POSITION_TYPE_BUY else -1
            profit = round((price - position.price_open) * direction * volume * spec.trade_contract_size, 2)
            remaining = round(position.volume - volume, 8)
            if remaining > 0:
                self._positions[position.ticket] = replace(position, volume=remaining)
            else:
                del self._positions[position.ticket]
            self._deals.append(
                Deal(**common, position_id=position.ticket, entry=C.DEAL_ENTRY_OUT, profit=profit)
            )
        else:
            self._positions[ticket] = Position(
                ticket=ticket,
                identifier=ticket,
                symbol=symbol,
                type=C.POSITION_TYPE_BUY if order_type == C.ORDER_TYPE_BUY else C.POSITION_TYPE_SELL,
                volume=volume,
                price_open=price,
                sl=request.get("sl", 0.0),
                tp=request.get("tp", 0.0),
                price_current=price,
                profit=0.0,
                swap=0.0,
                magic=request.get("magic", 0),
                comment=request.get("comment", ""),
                time_msc=tick.time_msc,
            )
            self._deals.append(Deal(**common, position_id=ticket, entry=C.DEAL_ENTRY_IN, profit=0.0))
        return self._result(C.TRADE_RETCODE_DONE, symbol, ticket, volume, price, "Request executed")

    def _modify(self, request: Mapping[str, Any]) -> TradeResult:
        position = self._positions.get(request["position"])
        if position is None:
            return self._result(C.TRADE_RETCODE_POSITION_CLOSED, request["symbol"], 0, 0.0, 0.0, "closed")
        # Comme MT5 (à confirmer en démo) : un champ omis vaut 0, donc supprime le niveau.
        sl, tp = request.get("sl", 0.0), request.get("tp", 0.0)
        if (sl, tp) == (position.sl, position.tp):
            return self._result(C.TRADE_RETCODE_NO_CHANGES, position.symbol, 0, 0.0, 0.0, "No changes")
        self._positions[position.ticket] = replace(position, sl=sl, tp=tp)
        return self._result(C.TRADE_RETCODE_DONE, position.symbol, position.ticket, 0.0, 0.0, "Done")

    def deals_for_position(self, position_id: int) -> list[Deal]:
        self._require_connection("history_deals_get")
        return [deal for deal in self._deals if deal.position_id == position_id]

    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float:
        self._require_connection("order_calc_margin")
        spec = self._symbols[symbol]
        return round(volume * spec.trade_contract_size * price / self.account_state.leverage, 2)

    def calc_profit(
        self, order_type: int, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float:
        self._require_connection("order_calc_profit")
        direction = 1 if order_type == C.ORDER_TYPE_BUY else -1
        spec = self._symbols[symbol]
        return round((price_close - price_open) * direction * volume * spec.trade_contract_size, 2)
