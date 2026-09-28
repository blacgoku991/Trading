"""Garde-fous non négociables sur le type de compte (CLAUDE.md §3, règles 1 et 2)."""

from __future__ import annotations

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import AccountState


class TradingRefused(Exception):
    """Le bot refuse d'envoyer des ordres sur ce compte."""


def ensure_demo_account(account: AccountState) -> None:
    """Ordres de test : compte DÉMO uniquement, sans exception possible."""
    if account.trade_mode != C.ACCOUNT_TRADE_MODE_DEMO:
        raise TradingRefused("opération réservée aux comptes DÉMO : ce compte n'est pas un compte démo")


def ensure_trading_permitted(account: AccountState, *, live_trading_env: bool, real_money_flag: bool) -> None:
    """Règle 1 : hors démo, trading refusé sauf LIVE_TRADING=true ET --i-understand-real-money."""
    if account.trade_mode == C.ACCOUNT_TRADE_MODE_DEMO:
        return
    if live_trading_env and real_money_flag:
        return
    raise TradingRefused(
        "compte non démo : trading refusé sans LIVE_TRADING=true dans .env "
        "ET l'argument --i-understand-real-money"
    )
