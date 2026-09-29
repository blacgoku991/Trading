"""Briques d'envoi d'ordres : arrondi au tick, mode de remplissage, retcodes, reprises limitées."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, CheckResult, Deal, TradeResult

# Libellés des codes retour (docs/RESEARCH.md §1.9).
RETCODE_TEXT = {
    C.ORDER_CHECK_OK: "vérification OK",
    C.TRADE_RETCODE_REQUOTE: "requote",
    C.TRADE_RETCODE_REJECT: "requête rejetée",
    C.TRADE_RETCODE_CANCEL: "requête annulée",
    C.TRADE_RETCODE_PLACED: "ordre placé",
    C.TRADE_RETCODE_DONE: "exécuté",
    C.TRADE_RETCODE_DONE_PARTIAL: "exécution partielle",
    C.TRADE_RETCODE_ERROR: "erreur de traitement",
    C.TRADE_RETCODE_TIMEOUT: "annulé : délai dépassé",
    C.TRADE_RETCODE_INVALID: "requête invalide",
    C.TRADE_RETCODE_INVALID_VOLUME: "volume invalide",
    C.TRADE_RETCODE_INVALID_PRICE: "prix invalide",
    C.TRADE_RETCODE_INVALID_STOPS: "SL/TP invalides (trop proches ou mal placés)",
    C.TRADE_RETCODE_TRADE_DISABLED: "trading désactivé",
    C.TRADE_RETCODE_MARKET_CLOSED: "marché fermé",
    C.TRADE_RETCODE_NO_MONEY: "fonds insuffisants",
    C.TRADE_RETCODE_PRICE_CHANGED: "prix modifié",
    C.TRADE_RETCODE_PRICE_OFF: "pas de cotation",
    C.TRADE_RETCODE_INVALID_EXPIRATION: "expiration invalide",
    C.TRADE_RETCODE_ORDER_CHANGED: "état de l'ordre modifié",
    C.TRADE_RETCODE_TOO_MANY_REQUESTS: "requêtes trop fréquentes",
    C.TRADE_RETCODE_NO_CHANGES: "aucune modification",
    C.TRADE_RETCODE_SERVER_DISABLES_AT: "trading automatique désactivé par le serveur",
    C.TRADE_RETCODE_CLIENT_DISABLES_AT: "trading automatique désactivé dans le terminal (bouton Algo Trading)",
    C.TRADE_RETCODE_LOCKED: "requête verrouillée",
    C.TRADE_RETCODE_FROZEN: "ordre ou position gelé (freeze level)",
    C.TRADE_RETCODE_INVALID_FILL: "mode de remplissage non supporté",
    C.TRADE_RETCODE_CONNECTION: "pas de connexion au serveur de trading",
    C.TRADE_RETCODE_ONLY_REAL: "réservé aux comptes réels",
    C.TRADE_RETCODE_LIMIT_ORDERS: "limite d'ordres en attente atteinte",
    C.TRADE_RETCODE_LIMIT_VOLUME: "limite de volume atteinte",
    C.TRADE_RETCODE_INVALID_ORDER: "type d'ordre invalide ou interdit",
    C.TRADE_RETCODE_POSITION_CLOSED: "position déjà fermée",
    C.TRADE_RETCODE_INVALID_CLOSE_VOLUME: "volume de clôture trop grand",
    C.TRADE_RETCODE_CLOSE_ORDER_EXIST: "un ordre de clôture existe déjà",
    C.TRADE_RETCODE_LIMIT_POSITIONS: "limite de positions atteinte",
    C.TRADE_RETCODE_REJECT_CANCEL: "activation de l'ordre en attente refusée",
    C.TRADE_RETCODE_LONG_ONLY: "positions longues uniquement",
    C.TRADE_RETCODE_SHORT_ONLY: "positions courtes uniquement",
    C.TRADE_RETCODE_CLOSE_ONLY: "clôture uniquement",
    C.TRADE_RETCODE_FIFO_CLOSE: "clôture FIFO imposée",
    C.TRADE_RETCODE_HEDGE_PROHIBITED: "positions opposées interdites",
}

FILLED = frozenset({C.TRADE_RETCODE_DONE, C.TRADE_RETCODE_DONE_PARTIAL})
# Nouveau prix puis nouvel essai.
RETRY_WITH_FRESH_PRICE = frozenset(
    {C.TRADE_RETCODE_REQUOTE, C.TRADE_RETCODE_PRICE_CHANGED, C.TRADE_RETCODE_PRICE_OFF}
)
# L'ordre a peut-être été exécuté : réconcilier avant tout renvoi, jamais de renvoi aveugle.
UNCERTAIN = frozenset({C.TRADE_RETCODE_TIMEOUT, C.TRADE_RETCODE_CONNECTION, C.TRADE_RETCODE_ERROR})


# Plage de recherche d'un ordre dont la réponse s'est perdue : large, car la date passée à MT5 et l'heure serveur des
# deals peuvent être décalées (docs/RESEARCH.md §1.6) ; le magic et le commentaire font le tri.
LOST_REPLY_MARGIN_S = 86_400


def find_entry_deals(broker: Broker, *, magic: int, comments: set[str], sent_s: int, now_s: int) -> dict[str, Deal]:
    """Deals d'entrée des ordres dont la réponse s'est perdue, par commentaire : même magic, même commentaire (à
    confirmer sur le terminal par run_scalp.py --verification). Un seul appel pour tous les ordres en attente, depuis
    le plus ancien envoi (sent_s, epoch serveur). Un ordre absent du résultat n'a pas été exécuté."""
    found: dict[str, Deal] = {}
    for deal in broker.deals_between(sent_s - LOST_REPLY_MARGIN_S, now_s + LOST_REPLY_MARGIN_S):
        if deal.magic == magic and deal.comment in comments and deal.entry == C.DEAL_ENTRY_IN:
            found.setdefault(deal.comment, deal)
    return found


def describe_retcode(code: int) -> str:
    return f"{code} ({RETCODE_TEXT.get(code, 'code inconnu')})"


class OrderRejected(Exception):
    def __init__(self, operation: str, retcode: int, comment: str) -> None:
        self.operation = operation
        self.retcode = retcode
        self.comment = comment
        super().__init__(f"{operation} refusé : {describe_retcode(retcode)}, commentaire du serveur : {comment!r}")


_ROUNDING = {"nearest": ROUND_HALF_EVEN, "down": ROUND_FLOOR, "up": ROUND_CEILING}


def round_to_tick(price: float, tick_size: float, digits: int, mode: str = "nearest") -> float:
    """Arrondit un prix au multiple de tick_size, sans erreur de flottant (via Decimal)."""
    ticks = (Decimal(str(price)) / Decimal(str(tick_size))).to_integral_value(rounding=_ROUNDING[mode])
    return round(float(ticks * Decimal(str(tick_size))), digits)


def filling_candidates(filling_mask: int) -> list[int]:
    """Modes à essayer, d'après le masque SYMBOL_FILLING_* du symbole ; RETURN en dernier recours."""
    candidates = []
    if filling_mask & C.SYMBOL_FILLING_FOK:
        candidates.append(C.ORDER_FILLING_FOK)
    if filling_mask & C.SYMBOL_FILLING_IOC:
        candidates.append(C.ORDER_FILLING_IOC)
    candidates.append(C.ORDER_FILLING_RETURN)
    return candidates


def select_filling(broker: Broker, request: Mapping[str, Any], filling_mask: int) -> tuple[int, CheckResult]:
    """Premier mode accepté par order_check. Seul un refus 10030 fait passer au mode suivant."""
    last = None
    for filling in filling_candidates(filling_mask):
        result = broker.order_check({**request, "type_filling": filling})
        if result.retcode == C.ORDER_CHECK_OK:
            return filling, result
        if result.retcode != C.TRADE_RETCODE_INVALID_FILL:
            raise OrderRejected("order_check", result.retcode, result.comment)
        last = result
    raise OrderRejected("order_check", last.retcode, "aucun mode de remplissage accepté")


def send_market_order(
    broker: Broker,
    request: Mapping[str, Any],
    fillings: Sequence[int],
    *,
    max_price_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> TradeResult:
    """Envoie un ordre au marché au prix courant.

    Sur 10030, passe au mode de remplissage suivant. Sur requote, prix modifié ou absence de
    cotation, réessaie au plus max_price_retries fois avec un tick frais. Tout autre refus
    lève OrderRejected (voir UNCERTAIN : réconcilier avant tout renvoi).
    """
    fillings = list(fillings)
    retries = 0
    while True:
        tick = broker.tick(request["symbol"])
        price = tick.ask if request["type"] == C.ORDER_TYPE_BUY else tick.bid
        result = broker.order_send({**request, "price": price, "type_filling": fillings[0]})
        if result.retcode in FILLED:
            return result
        if result.retcode == C.TRADE_RETCODE_INVALID_FILL and len(fillings) > 1:
            fillings.pop(0)
            continue
        if result.retcode in RETRY_WITH_FRESH_PRICE and retries < max_price_retries:
            retries += 1
            sleep(0.2 * retries)
            continue
        raise OrderRejected("order_send", result.retcode, result.comment)
