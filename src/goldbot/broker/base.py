"""Interface commune des brokers : MT5Broker (réel), SimBroker (backtest), FakeBroker (tests).

Les structures ne gardent que les champs utilisés, sous leur nom MT5. Tous ces noms
existent dans le package officiel MetaTrader5 5.0.6231 (docs/RESEARCH.md, annexe A.3).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any

from goldbot.broker import mt5_constants as C

# Explications des échecs les plus courants de initialize() (docs/RESEARCH.md §1.2).
_HINTS = {
    C.RES_E_AUTH_FAILED: "vérifie MT5_LOGIN, MT5_PASSWORD et le nom EXACT de MT5_SERVER",
    C.RES_E_INVALID_VERSION: "mets à jour ensemble le terminal MT5 et le package MetaTrader5",
    C.RES_E_AUTO_TRADING_DISABLED: (
        "décoche « Désactiver le trading algorithmique via les API Python externes » "
        "(Outils > Options > Expert Consultants)"
    ),
    C.RES_E_INTERNAL_FAIL_INIT: (
        "terminal introuvable ou impossible à lancer : vérifie MT5_PATH (terminal64.exe)"
    ),
    C.RES_E_INTERNAL_FAIL_TIMEOUT: (
        "le terminal ne répond pas : ouvre-le, connecte-toi une fois à la main, "
        "autorise le trading algorithmique"
    ),
    C.RES_E_INTERNAL_FAIL_CONNECT: "liaison perdue avec le terminal : est-il toujours ouvert ?",
}


class BrokerError(Exception):
    """Échec d'un appel broker, avec le code et la description de last_error()."""

    def __init__(self, operation: str, code: int, description: str) -> None:
        self.operation = operation
        self.code = code
        self.description = description
        self.hint = _HINTS.get(code, "")
        message = f"{operation} : échec ({code}) {description}"
        if self.hint:
            message += f" -> {self.hint}"
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        """Vrai si une reconnexion peut régler le problème (liaison IPC, échec générique)."""
        return self.code in C.IPC_ERROR_CODES or self.code == C.RES_E_FAIL


def _select(cls: type, data: Mapping[str, Any]) -> dict[str, Any]:
    """Extrait d'une structure MT5 (via _asdict()) les champs déclarés par la dataclass."""
    return {f.name: data[f.name] for f in fields(cls) if f.init and f.name != "raw"}


class _FromMt5:
    @classmethod
    def from_mt5(cls, data: Mapping[str, Any]):  # noqa: ANN206
        return cls(**_select(cls, data))


@dataclass(frozen=True)
class TerminalState(_FromMt5):
    """terminal_info(). ping_last est en microsecondes."""

    connected: bool  # connecté au serveur de trading
    trade_allowed: bool  # bouton Algo Trading
    tradeapi_disabled: bool  # option « Disable algorithmic trading via external Python API »
    maxbars: int  # réglage « Max bars in chart »
    ping_last: int
    build: int
    company: str
    path: str
    data_path: str
    commondata_path: str


@dataclass(frozen=True)
class AccountState(_FromMt5):
    """account_info(). login et server sont des secrets : exclus du repr, jamais affichés."""

    login: int = field(repr=False)
    server: str = field(repr=False)
    company: str
    currency: str
    trade_mode: int  # ACCOUNT_TRADE_MODE_*
    margin_mode: int  # ACCOUNT_MARGIN_MODE_*
    leverage: int
    trade_allowed: bool
    trade_expert: bool
    margin_so_mode: int  # ACCOUNT_STOPOUT_MODE_*
    margin_so_call: float
    margin_so_so: float
    balance: float
    equity: float
    margin: float
    margin_free: float
    margin_level: float
    fifo_close: bool
    limit_orders: int
    currency_digits: int


@dataclass(frozen=True)
class SymbolSpec(_FromMt5):
    """symbol_info(). raw garde la structure complète pour le dump JSON."""

    name: str
    path: str
    description: str
    visible: bool
    select: bool
    trade_mode: int  # SYMBOL_TRADE_MODE_*
    trade_exemode: int  # SYMBOL_TRADE_EXECUTION_*
    trade_calc_mode: int  # SYMBOL_CALC_MODE_*
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_tick_value_profit: float
    trade_tick_value_loss: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    volume_limit: float
    trade_stops_level: int  # en points
    trade_freeze_level: int  # en points
    filling_mode: int  # masque SYMBOL_FILLING_*
    expiration_mode: int  # masque SYMBOL_EXPIRATION_*
    order_mode: int  # masque SYMBOL_ORDER_*
    order_gtc_mode: int  # SYMBOL_ORDERS_*
    swap_mode: int  # SYMBOL_SWAP_MODE_*
    swap_long: float
    swap_short: float
    swap_rollover3days: int  # DAY_OF_WEEK_* du triple swap
    spread: int  # en points
    spread_float: bool
    currency_base: str
    currency_profit: str
    currency_margin: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_mt5(cls, data: Mapping[str, Any]) -> SymbolSpec:
        return cls(**_select(cls, data), raw=dict(data))


@dataclass(frozen=True)
class Tick(_FromMt5):
    """symbol_info_tick(). time (s) et time_msc (ms) sont en heure SERVEUR encodée en epoch."""

    time: int
    time_msc: int
    bid: float
    ask: float


@dataclass(frozen=True)
class Position(_FromMt5):
    ticket: int
    identifier: int
    symbol: str
    type: int  # POSITION_TYPE_*
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float
    magic: int
    comment: str
    time_msc: int


@dataclass(frozen=True)
class Deal(_FromMt5):
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: int  # DEAL_TYPE_*
    entry: int  # DEAL_ENTRY_*
    reason: int  # DEAL_REASON_*
    volume: float
    price: float
    commission: float
    swap: float
    fee: float
    profit: float
    magic: int
    comment: str
    time_msc: int


@dataclass(frozen=True)
class TradeResult(_FromMt5):
    """order_send(). Succès : retcode TRADE_RETCODE_DONE (10009)."""

    retcode: int
    deal: int
    order: int
    volume: float
    price: float
    bid: float
    ask: float
    comment: str
    request_id: int
    retcode_external: int


@dataclass(frozen=True)
class CheckResult(_FromMt5):
    """order_check(). Succès : retcode ORDER_CHECK_OK (0), pas 10009."""

    retcode: int
    balance: float
    equity: float
    profit: float
    margin: float
    margin_free: float
    margin_level: float
    comment: str


class Broker(ABC):
    """Interface commune. Toute méthode lève BrokerError en cas d'échec."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    @abstractmethod
    def version(self) -> tuple[int, int, str]:
        """Version du terminal : (version, build, date)."""

    @abstractmethod
    def terminal(self) -> TerminalState: ...

    @abstractmethod
    def account(self) -> AccountState: ...

    @abstractmethod
    def symbols(self, group: str) -> list[SymbolSpec]: ...

    @abstractmethod
    def select_symbol(self, name: str, enable: bool = True) -> None: ...

    @abstractmethod
    def symbol(self, name: str) -> SymbolSpec: ...

    @abstractmethod
    def tick(self, name: str) -> Tick: ...

    @abstractmethod
    def positions(self, symbol: str | None = None) -> list[Position]: ...

    @abstractmethod
    def order_check(self, request: Mapping[str, Any]) -> CheckResult: ...

    @abstractmethod
    def order_send(self, request: Mapping[str, Any]) -> TradeResult: ...

    @abstractmethod
    def deals_for_position(self, position_id: int) -> list[Deal]: ...

    @abstractmethod
    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float: ...

    @abstractmethod
    def calc_profit(
        self, order_type: int, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float: ...
