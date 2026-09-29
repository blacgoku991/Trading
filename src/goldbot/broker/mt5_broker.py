"""Connecteur réel MetaTrader 5. Windows uniquement : le package officiel est importé à la demande."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import numpy as np

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import (
    RATES_DTYPE,
    TICKS_DTYPE,
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

if TYPE_CHECKING:
    from goldbot.config import Secrets, Settings


def verify_constants(module: Any) -> list[str]:
    """Compare le miroir mt5_constants au package installé ; renvoie la liste des écarts."""
    problems = []
    for name in sorted(C.OFFICIAL_NAMES):
        if not hasattr(module, name):
            problems.append(f"{name} absent du package")
        elif getattr(module, name) != getattr(C, name):
            problems.append(f"{name} : package={getattr(module, name)!r}, miroir={getattr(C, name)!r}")
    return problems


class MT5Broker(Broker):
    """Broker réel via le package officiel MetaTrader5 (terminal ouvert sur la même machine)."""

    def __init__(
        self,
        *,
        login: int,
        password: str,
        server: str,
        path: str | None,
        timeout_ms: int,
        portable: bool = False,
    ) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        self._timeout_ms = timeout_ms
        self._portable = portable
        self._mt5: Any = None

    @classmethod
    def from_settings(cls, settings: Settings, secrets: Secrets) -> MT5Broker:
        return cls(
            login=secrets.login,
            password=secrets.password,
            server=secrets.server,
            path=secrets.terminal_path,
            timeout_ms=settings.mt5.timeout_ms,
            portable=settings.mt5.portable,
        )

    def __repr__(self) -> str:  # aucun secret dans la représentation
        return f"MT5Broker(path={self._path!r})"

    def _module(self) -> Any:
        if self._mt5 is None:
            try:
                import MetaTrader5  # import paresseux : le package n'existe que sous Windows
            except ImportError as exc:
                # Code non réessayable : une reconnexion n'y changerait rien.
                raise BrokerError(
                    "import MetaTrader5",
                    C.RES_E_UNSUPPORTED,
                    "package MetaTrader5 introuvable (il ne s'installe que sous Windows)",
                ) from exc
            self._mt5 = MetaTrader5
        return self._mt5

    def _error(self, operation: str) -> BrokerError:
        code, description = self._module().last_error()
        return BrokerError(operation, code, description)

    def _required(self, operation: str, result: Any) -> Any:
        if result is None:
            raise self._error(operation)
        return result

    def _listing(self, operation: str, result: Any) -> list[Any]:
        # Selon la doc, None peut signifier « rien trouvé » : on ne lève que si last_error() signale un échec.
        if result is None:
            error = self._error(operation)
            if error.code == C.RES_S_OK:
                return []
            raise error
        return list(result)

    def connect(self) -> None:
        mt5 = self._module()
        # 1. S'attacher au terminal tel qu'il est, sans lui faire refaire l'authentification.
        options: dict[str, Any] = {"timeout": self._timeout_ms}
        if self._portable:
            options["portable"] = True
        attached = mt5.initialize(self._path, **options) if self._path else mt5.initialize(**options)
        if not attached:
            error = self._error("initialize")  # lire last_error() avant shutdown()
            mt5.shutdown()
            raise error
        # 2. Se connecter au compte de .env seulement si le terminal n'y est pas déjà.
        account = mt5.account_info()
        on_expected_account = account is not None and account.login == self._login
        if not on_expected_account and not mt5.login(self._login, password=self._password, server=self._server):
            error = self._error("login")
            mt5.shutdown()
            raise error
        problems = verify_constants(mt5)
        if problems:
            mt5.shutdown()
            raise BrokerError(
                "verify_constants",
                C.RES_E_INVALID_VERSION,
                "le package MetaTrader5 installé ne correspond pas au miroir goldbot : " + "; ".join(problems[:5]),
            )

    def shutdown(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()

    def version(self) -> tuple[int, int, str]:
        return tuple(self._required("version", self._module().version()))

    def terminal(self) -> TerminalState:
        info = self._required("terminal_info", self._module().terminal_info())
        return TerminalState.from_mt5(info._asdict())

    def account(self) -> AccountState:
        info = self._required("account_info", self._module().account_info())
        return AccountState.from_mt5(info._asdict())

    def symbols(self, group: str) -> list[SymbolSpec]:
        found = self._listing("symbols_get", self._module().symbols_get(group=group))
        return [SymbolSpec.from_mt5(item._asdict()) for item in found]

    def select_symbol(self, name: str, enable: bool = True) -> None:
        if not self._module().symbol_select(name, enable):
            raise self._error(f"symbol_select({name})")

    def symbol(self, name: str) -> SymbolSpec:
        info = self._required(f"symbol_info({name})", self._module().symbol_info(name))
        return SymbolSpec.from_mt5(info._asdict())

    def tick(self, name: str) -> Tick:
        tick = self._required(f"symbol_info_tick({name})", self._module().symbol_info_tick(name))
        return Tick.from_mt5(tick._asdict())

    def positions(self, symbol: str | None = None) -> list[Position]:
        mt5 = self._module()
        found = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        return [Position.from_mt5(item._asdict()) for item in self._listing("positions_get", found)]

    def order_check(self, request: Mapping[str, Any]) -> CheckResult:
        result = self._required("order_check", self._module().order_check(dict(request)))
        return CheckResult.from_mt5(result._asdict())

    def order_send(self, request: Mapping[str, Any]) -> TradeResult:
        result = self._required("order_send", self._module().order_send(dict(request)))
        return TradeResult.from_mt5(result._asdict())

    def deals_for_position(self, position_id: int) -> list[Deal]:
        found = self._listing("history_deals_get", self._module().history_deals_get(position=position_id))
        return [Deal.from_mt5(item._asdict()) for item in found]

    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float:
        margin = self._module().order_calc_margin(order_type, symbol, volume, price)
        return float(self._required("order_calc_margin", margin))

    def calc_profit(
        self, order_type: int, symbol: str, volume: float, price_open: float, price_close: float
    ) -> float:
        profit = self._module().order_calc_profit(order_type, symbol, volume, price_open, price_close)
        return float(self._required("order_calc_profit", profit))

    def _history(self, operation: str, result: Any, dtype: np.dtype) -> np.ndarray:
        if result is None:
            error = self._error(operation)
            if error.code == C.RES_S_OK:
                return np.empty(0, dtype)
            raise error
        return result

    def rates_range(self, symbol: str, timeframe: int, start: int, end: int) -> np.ndarray:
        found = self._module().copy_rates_range(symbol, timeframe, _as_datetime(start), _as_datetime(end))
        return self._history(f"copy_rates_range({symbol})", found, RATES_DTYPE)

    def ticks_range(self, symbol: str, start: int, end: int, flags: int) -> np.ndarray:
        found = self._module().copy_ticks_range(symbol, _as_datetime(start), _as_datetime(end), flags)
        return self._history(f"copy_ticks_range({symbol})", found, TICKS_DTYPE)


def _as_datetime(epoch_s: int) -> datetime:
    """Borne de copy_*_range. Comme dans les exemples officiels : datetime avec tzinfo UTC.

    MT5 compare ses horodatages (heure serveur encodée en epoch) à cet epoch : passer
    l'epoch serveur revient donc à demander l'heure serveur voulue.
    """
    return datetime.fromtimestamp(epoch_s, tz=UTC)
