"""Lancement du bot en direct (logique de scripts/run_live.py). Windows, terminal MT5 ouvert.

Garde-fous : compte DÉMO par défaut (réel seulement avec LIVE_TRADING=true ET --i-understand-real-money),
compte en hedging exigé (le magic isole alors les trades manuels), bouton Algo Trading actif,
une seule instance à la fois. Mode --simulation : tout est calculé, aucun ordre n'est envoyé.
"""

from __future__ import annotations

import argparse
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from goldbot import __version__
from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, Secrets, Settings, load_secrets, load_settings
from goldbot.indicators.sessions import local_to_utc
from goldbot.live.lock import InstanceLock
from goldbot.live.runner import LiveRunner, summary_line
from goldbot.monitoring.logging_setup import setup_logging
from goldbot.risk.guards import TradingRefused, ensure_trading_permitted
from goldbot.state.store import StateStore
from goldbot.strategies.catalog import session_portfolio

log = logging.getLogger("goldbot.live")

EXIT_OK = 0
EXIT_NO_CONNECTION = 2
EXIT_REFUSED = 3
EXIT_CONFIG = 4
EXIT_LOCKED = 5

POLL_SECONDS = 2.0
HEARTBEAT = pd.Timedelta(minutes=30)


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _schedule_lines(settings: Settings, today: datetime) -> list[str]:
    zone = ZoneInfo(settings.bot.display_timezone)
    lines = []
    for key, config in settings.strategies.session_momentum.items():
        if not config.enabled:
            continue
        day = pd.Timestamp(today.astimezone(ZoneInfo(config.zone)).date()).to_datetime64().astype("datetime64[D]")
        signal = local_to_utc([day], config.signal_time, config.zone)[0].tz_convert(zone)
        exit_ = local_to_utc([day], config.exit_time, config.zone)[0].tz_convert(zone)
        lines.append(f"  {key} : signal vers {signal:%H:%M}, sortie à {exit_:%H:%M} ({settings.bot.display_timezone})")
    return lines


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    broker_factory: Callable[[Settings, Secrets], Broker] = MT5Broker.from_settings,
    now_utc: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
    max_steps: int | None = None,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="run_live.py", description="Bot de trading de l'or en direct (démo).")
    parser.add_argument("--simulation", action="store_true", help="calcule tout mais n'envoie aucun ordre")
    parser.add_argument(
        "--i-understand-real-money",
        action="store_true",
        help="exigé (avec LIVE_TRADING=true dans .env) pour trader sur un compte réel",
    )
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--env", type=Path, default=root / ".env")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
        secrets = load_secrets(args.env)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG
    setup_logging(
        log_dir=_resolve(root, settings.paths.logs),
        level=settings.logging.level,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
        display_timezone=settings.bot.display_timezone,
        secrets=secrets.sensitive_values(),
    )
    now_utc = now_utc or (lambda: datetime.now(UTC))
    lock = InstanceLock(root / "data" / "live.lock")
    if not lock.acquire():
        echo("ERREUR : un autre bot tourne déjà sur ce PC (fenêtre PowerShell ouverte ailleurs ?).")
        return EXIT_LOCKED
    broker = broker_factory(settings, secrets)
    supervisor = ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=sleep)
    mode = "SIMULATION (aucun ordre envoyé)" if args.simulation else "DÉMO"
    echo(f"=== Bot de trading de l'or - goldbot {__version__} - mode {mode} ===")
    echo("Connexion au terminal MT5 en cours...")
    try:
        try:
            supervisor.connect()
        except BrokerError as exc:
            echo(f"ERREUR : connexion au terminal MT5 impossible : {exc}")
            return EXIT_NO_CONNECTION
        account = broker.account()
        if not args.simulation:
            ensure_trading_permitted(
                account, live_trading_env=secrets.live_trading, real_money_flag=args.i_understand_real_money
            )
            if account.trade_mode == C.ACCOUNT_TRADE_MODE_REAL:
                mode = "RÉEL"
            if not broker.terminal().trade_allowed:
                raise TradingRefused("bouton Algo Trading désactivé dans MT5 : active-le (il doit être vert)")
        if account.margin_mode != C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise TradingRefused("compte en netting : le magic ne suffit plus à isoler tes trades manuels")
        spec, _ = resolve_gold_symbol(broker, settings.symbol)
        strategy = session_portfolio(settings.strategies.session_momentum)
        store = StateStore(root / "data" / ("live_simulation.sqlite" if args.simulation else "live.sqlite"))
        runner = LiveRunner(
            broker,
            supervisor,
            strategy,
            store,
            settings=settings,
            spec=spec,
            simulate=args.simulation,
            now_utc=now_utc,
            sleep=sleep,
        )
        echo(f"Compte {mode}, equity {account.equity:.2f} {account.currency}, symbole {spec.name}.")
        risk = settings.risk
        echo(
            f"Risque : {risk.risk_per_trade_pct:g} % par trade, perte max {risk.max_daily_loss_pct:g} % par jour, "
            f"arrêt total à -{risk.max_drawdown_pct:g} % depuis le plus haut."
        )
        echo(f"Stratégies : {', '.join(s.name for s in strategy.strategies)}")
        for line in _schedule_lines(settings, now_utc()):
            echo(line)
        echo("Le bot tourne. Laisse cette fenêtre ouverte ; Ctrl+C pour l'arrêter (les SL restent sur le serveur).")
        steps, last_heartbeat = 0, pd.Timestamp(now_utc())
        while max_steps is None or steps < max_steps:
            try:
                runner.step()
            except BrokerError as exc:
                log.warning("passage interrompu (%s) : nouvel essai dans quelques secondes", exc)
            now = pd.Timestamp(now_utc())
            if now - last_heartbeat >= HEARTBEAT:
                last_heartbeat = now
                try:
                    positions = len(runner._mine())
                    echo(f"{now.tz_convert(settings.bot.display_timezone):%H:%M} toujours en marche : "
                         + summary_line(broker.account(), runner.limits, positions))  # fmt: skip
                except BrokerError as exc:
                    log.warning("état du compte illisible : %s", exc)
            steps += 1
            sleep(POLL_SECONDS)
        return EXIT_OK
    except (TradingRefused, SymbolSelectionError) as exc:
        echo(f"REFUS : {exc}")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        echo("Arrêt demandé : les positions ouvertes gardent leur SL sur le serveur. Relance le bot pour leur sortie.")
        return EXIT_OK
    finally:
        broker.shutdown()
        lock.release()
