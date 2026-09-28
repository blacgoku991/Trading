"""Vérifications de démarrage : terminal, compte, symbole, heure serveur. Fonctions pures, testables."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import AccountState, SymbolSpec, TerminalState, Tick
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule, format_offset, measured_offset

# « Unlimited » dans les options du terminal donne une très grande valeur ; en dessous, on prévient.
MIN_MAXBARS = 5_000_000
MAX_PING_MS = 200
# Au-delà de cet écart, heure du tick et règle serveur ne concordent pas.
MAX_CLOCK_RESIDUAL_S = 120


class Level(Enum):
    OK = "OK"
    INFO = "INFO"
    WARN = "ATTENTION"
    ERROR = "ERREUR"


@dataclass(frozen=True)
class Finding:
    level: Level
    message: str


def check_terminal(terminal: TerminalState) -> list[Finding]:
    findings = [
        Finding(Level.OK, "Terminal connecté au serveur de trading")
        if terminal.connected
        else Finding(Level.ERROR, "Terminal NON connecté au serveur de trading (vérifie la connexion dans MT5)"),
        Finding(Level.OK, "Bouton Algo Trading activé")
        if terminal.trade_allowed
        else Finding(
            Level.ERROR,
            "Bouton Trading Algo désactivé : tout ordre serait refusé (code 10027). Clique dessus pour le "
            "passer au vert, et décoche « Désactiver le trading algorithmique lors du changement de compte » "
            "(Outils > Options > Expert Consultants)",
        ),
        Finding(Level.OK, "Trading via l'API Python autorisé dans le terminal")
        if not terminal.tradeapi_disabled
        else Finding(
            Level.ERROR,
            "Option « Désactiver le trading algorithmique via les API Python externes » "
            "(Disable algorithmic trading via external Python API) cochée : "
            "à décocher dans Outils > Options > Expert Consultants",
        ),
    ]
    if terminal.maxbars < MIN_MAXBARS:
        findings.append(
            Finding(
                Level.WARN,
                f"« Max bars in chart » = {terminal.maxbars} : choisis « Unlimited » "
                "(Outils > Options > Graphiques) puis redémarre le terminal",
            )
        )
    else:
        findings.append(Finding(Level.OK, f"Max bars in chart = {terminal.maxbars}"))
    if terminal.ping_last < 0:
        findings.append(Finding(Level.WARN, "Latence vers le serveur inconnue"))
    elif terminal.ping_last / 1000 > MAX_PING_MS:
        findings.append(Finding(Level.WARN, f"Latence élevée vers le serveur : {terminal.ping_last / 1000:.0f} ms"))
    else:
        findings.append(Finding(Level.OK, f"Latence vers le serveur : {terminal.ping_last / 1000:.0f} ms"))
    return findings


def check_account(account: AccountState, *, live_trading: bool, expected_login: int | None = None) -> list[Finding]:
    findings = []
    if account.trade_mode == C.ACCOUNT_TRADE_MODE_DEMO:
        findings.append(Finding(Level.OK, "Compte DÉMO"))
    elif account.trade_mode == C.ACCOUNT_TRADE_MODE_REAL:
        findings.append(
            Finding(
                Level.WARN,
                "Compte RÉEL : le bot refusera de trader sans LIVE_TRADING=true "
                "ET l'argument --i-understand-real-money",
            )
        )
    elif account.trade_mode == C.ACCOUNT_TRADE_MODE_CONTEST:
        findings.append(Finding(Level.WARN, "Compte CONCOURS : traité comme non-démo"))
    else:
        findings.append(Finding(Level.ERROR, f"Type de compte inconnu (trade_mode={account.trade_mode})"))
    if live_trading:
        findings.append(Finding(Level.WARN, "LIVE_TRADING=true dans .env"))

    if expected_login is not None and account.login != expected_login:
        findings.append(Finding(Level.ERROR, "Le terminal est connecté à un autre compte que MT5_LOGIN"))
    if not account.trade_allowed:
        findings.append(
            Finding(Level.ERROR, "Trading refusé sur ce compte (connexion avec le mot de passe investisseur ?)")
        )
    if not account.trade_expert:
        findings.append(Finding(Level.ERROR, "Le serveur interdit le trading automatique sur ce compte"))

    if account.margin_mode == C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
        findings.append(
            Finding(Level.OK, "Compte en hedging : les positions du bot restent séparées des trades manuels")
        )
    elif account.margin_mode == C.ACCOUNT_MARGIN_MODE_RETAIL_NETTING:
        findings.append(
            Finding(
                Level.WARN,
                "Compte en NETTING : une seule position par symbole, le magic ne suffit plus à isoler "
                "tes trades manuels sur l'or (ils fusionneraient avec ceux du bot)",
            )
        )
    else:
        findings.append(Finding(Level.ERROR, f"Mode de marge non géré (margin_mode={account.margin_mode})"))

    if account.currency != "USD":
        findings.append(
            Finding(Level.INFO, f"Devise du compte {account.currency} : le risque sera converti via order_calc_profit")
        )
    return findings


def check_symbol(spec: SymbolSpec, *, test_volume: float) -> list[Finding]:
    findings = [Finding(Level.OK, f"Symbole {spec.name} ouvert au trading")]
    if spec.volume_min > test_volume:
        findings.append(Finding(Level.WARN, f"Volume minimal {spec.volume_min} lot > {test_volume} lot"))
    if not spec.filling_mode & (C.SYMBOL_FILLING_FOK | C.SYMBOL_FILLING_IOC):
        findings.append(Finding(Level.WARN, "Aucun mode FOK/IOC annoncé : le mode RETURN sera tenté"))
    if spec.trade_exemode in (C.SYMBOL_TRADE_EXECUTION_MARKET, C.SYMBOL_TRADE_EXECUTION_EXCHANGE):
        findings.append(
            Finding(Level.INFO, "Exécution au marché : « deviation » sans effet, le slippage sera mesuré")
        )
    return findings


@dataclass(frozen=True)
class ServerTimeCheck:
    findings: list[Finding]
    expected_offset_s: float
    measured_offset_s: float
    residual_s: float
    market_open: bool


def check_server_time(
    tick: Tick, now_utc: datetime, rule: ServerTimeRule, schedule: MarketSchedule
) -> ServerTimeCheck:
    """Compare l'heure du dernier tick à la règle serveur (mesurable seulement marché ouvert)."""
    expected = rule.utc_offset_at(now_utc).total_seconds()
    measured = measured_offset(tick.time_msc / 1000, now_utc)
    residual = measured - expected
    market_open = schedule.is_open(rule.to_server(now_utc))
    if not market_open:
        findings = [
            Finding(
                Level.INFO,
                f"Marché fermé : décalage non mesurable (tick périmé), règle appliquée {format_offset(expected)}",
            )
        ]
    elif abs(residual) <= MAX_CLOCK_RESIDUAL_S:
        findings = [
            Finding(
                Level.OK,
                f"Heure serveur conforme à la règle : {format_offset(expected)} (écart {residual:+.0f} s)",
            )
        ]
    else:
        findings = [
            Finding(
                Level.ERROR,
                f"Décalage mesuré {format_offset(measured)} au lieu de {format_offset(expected)} "
                f"(écart {residual:+.0f} s) : horloge du PC, règle serveur ou flux figé ?",
            )
        ]
    return ServerTimeCheck(findings, expected, measured, residual, market_open)
