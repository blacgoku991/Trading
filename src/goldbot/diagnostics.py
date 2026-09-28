"""Vérification de la connexion MT5 (logique de scripts/check_connection.py, testable sans MT5)."""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from zoneinfo import ZoneInfo

from goldbot import __version__
from goldbot.broker import mt5_constants as C
from goldbot.broker.base import AccountState, Broker, BrokerError, SymbolSpec, Tick
from goldbot.broker.checks import (
    Finding,
    Level,
    check_account,
    check_server_time,
    check_symbol,
    check_terminal,
)
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor, FeedState, evaluate_feed
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, Secrets, Settings, load_secrets, load_settings
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import InvalidServerTime, ServerTimeRule, format_offset
from goldbot.execution.orders import OrderRejected
from goldbot.execution.test_order import TestOrderFailed, TestOrderRefused, run_test_order
from goldbot.monitoring.logging_setup import SecretRedactor, setup_logging

log = logging.getLogger("goldbot.check")

CHECK_MAX_ATTEMPTS = 3

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_NO_CONNECTION = 2
EXIT_REFUSED = 3
EXIT_CONFIG = 4

_FILLING_FLAGS = {C.SYMBOL_FILLING_FOK: "FOK", C.SYMBOL_FILLING_IOC: "IOC", C.SYMBOL_FILLING_BOC: "BOC"}
_EXPIRATION_FLAGS = {
    C.SYMBOL_EXPIRATION_GTC: "GTC",
    C.SYMBOL_EXPIRATION_DAY: "DAY",
    C.SYMBOL_EXPIRATION_SPECIFIED: "SPECIFIED",
    C.SYMBOL_EXPIRATION_SPECIFIED_DAY: "SPECIFIED_DAY",
}
_ORDER_FLAGS = {
    C.SYMBOL_ORDER_MARKET: "MARKET",
    C.SYMBOL_ORDER_LIMIT: "LIMIT",
    C.SYMBOL_ORDER_STOP: "STOP",
    C.SYMBOL_ORDER_STOP_LIMIT: "STOP_LIMIT",
    C.SYMBOL_ORDER_SL: "SL",
    C.SYMBOL_ORDER_TP: "TP",
    C.SYMBOL_ORDER_CLOSEBY: "CLOSEBY",
}
_ACCOUNT_TYPES = {
    C.ACCOUNT_TRADE_MODE_DEMO: "DÉMO",
    C.ACCOUNT_TRADE_MODE_CONTEST: "CONCOURS",
    C.ACCOUNT_TRADE_MODE_REAL: "RÉEL",
}
_DAYS = ["dimanche", "lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi"]  # DAY_OF_WEEK_SUNDAY = 0


def constant_name(prefix: str, value: int) -> str:
    """Nom officiel d'une valeur d'énumération MT5, sans son préfixe (ex. « POINTS »)."""
    for name in sorted(C.OFFICIAL_NAMES):
        if name.startswith(prefix) and getattr(C, name) == value:
            return name[len(prefix) :]
    return str(value)


def flag_names(mask: int, flags: dict[int, str]) -> str:
    names = [name for bit, name in flags.items() if mask & bit]
    return ", ".join(names) if names else "aucun"


def _yes(value: bool) -> str:
    return "oui" if value else "non"


def _package_version() -> str:
    try:
        return metadata.version("MetaTrader5")
    except metadata.PackageNotFoundError:
        return "non installé"


class Report:
    """Lignes du rapport : affichées au fil de l'eau, secrets masqués, puis enregistrées."""

    def __init__(self, redactor: SecretRedactor, echo: Callable[[str], None] = print) -> None:
        self._redactor = redactor
        self._echo = echo
        self.lines: list[str] = []

    def line(self, text: str = "") -> None:
        text = self._redactor.redact(text)
        self.lines.append(text)
        self._echo(text)

    def section(self, title: str) -> None:
        self.line()
        self.line(f"[{title}]")

    def field(self, label: str, value: object) -> None:
        self.line(f"  {label} {'.' * max(2, 34 - len(label))} {value}")

    def finding(self, finding: Finding) -> None:
        self.line(f"  {finding.level.value:<9} {finding.message}")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def _first_tick(broker: Broker, name: str, sleep: Callable[[float], None]) -> Tick:
    """Juste après symbol_select, le premier tick peut manquer un instant : quelques essais."""
    for _ in range(4):
        try:
            return broker.tick(name)
        except BrokerError:
            sleep(0.5)
    return broker.tick(name)


def _report_symbol(report: Report, spec: SymbolSpec, candidates: list[SymbolSpec], currency: str) -> None:
    report.field("candidats", ", ".join(s.name for s in candidates) or "aucun")
    report.field("retenu", f"{spec.name} ({spec.description})")
    report.field("digits / point / pas de prix", f"{spec.digits} / {spec.point:g} / {spec.trade_tick_size:g}")
    report.field("taille du contrat (1 lot)", f"{spec.trade_contract_size:g}")
    report.field("valeur d'un pas de prix (1 lot)", f"{spec.trade_tick_value:g} {currency}")
    report.field("volume min / max / pas", f"{spec.volume_min:g} / {spec.volume_max:g} / {spec.volume_step:g}")
    report.field("stops level / freeze level", f"{spec.trade_stops_level} / {spec.trade_freeze_level} points")
    report.field("remplissage (masque)", f"{spec.filling_mode} = {flag_names(spec.filling_mode, _FILLING_FLAGS)}")
    report.field("exécution", constant_name("SYMBOL_TRADE_EXECUTION_", spec.trade_exemode))
    report.field(
        "expiration (masque)", f"{spec.expiration_mode} = {flag_names(spec.expiration_mode, _EXPIRATION_FLAGS)}"
    )
    report.field("types d'ordres (masque)", f"{spec.order_mode} = {flag_names(spec.order_mode, _ORDER_FLAGS)}")
    report.field("durée des ordres GTC", constant_name("SYMBOL_ORDERS_", spec.order_gtc_mode))
    report.field(
        "swap : mode / long / short",
        f"{constant_name('SYMBOL_SWAP_MODE_', spec.swap_mode)} / {spec.swap_long:g} / {spec.swap_short:g}",
    )
    day = spec.swap_rollover3days
    report.field("jour du triple swap", _DAYS[day] if 0 <= day < len(_DAYS) else str(day))
    report.field("spread", f"{spec.spread} points ({'flottant' if spec.spread_float else 'fixe'})")
    report.field("mode de calcul", constant_name("SYMBOL_CALC_MODE_", spec.trade_calc_mode))


def _report_test_order(
    report: Report,
    broker: Broker,
    settings: Settings,
    spec: SymbolSpec,
    account: AccountState,
    rule: ServerTimeRule,
    schedule: MarketSchedule,
    now: datetime,
    sleep: Callable[[float], None],
) -> int:
    report.section("Ordre de test (compte DÉMO uniquement)")
    try:
        result = run_test_order(
            broker,
            spec=spec,
            account=account,
            magic=settings.bot.magic,
            config=settings.test_order,
            rule=rule,
            schedule=schedule,
            now_utc=now,
            sleep=sleep,
        )
    except TestOrderRefused as exc:
        report.line(f"  REFUSÉ    {exc}")
        return EXIT_REFUSED
    except (TestOrderFailed, OrderRejected, BrokerError) as exc:
        report.line(f"  ÉCHEC     {exc}")
        return EXIT_PROBLEMS
    digits, currency = spec.digits, account.currency
    report.field("mode de remplissage", constant_name("ORDER_FILLING_", result.filling))
    report.field("prix demandé / obtenu", f"{result.requested_price:.{digits}f} / {result.entry_price:.{digits}f}")
    report.field("slippage à l'entrée", f"{result.entry_slippage_points:+.0f} points")
    report.field("spread à l'entrée", f"{result.spread_points:.0f} points")
    report.field("SL initial / TP", f"{result.sl_initial:.{digits}f} / {result.tp:.{digits}f}")
    tightened = f"{result.sl_tightened:.{digits}f}" if result.sl_tightened is not None else "non"
    report.field("SL resserré", tightened)
    exit_price = f"{result.exit_price:.{digits}f}" if result.exit_price is not None else "?"
    report.field("prix de sortie", exit_price)
    for deal in result.deals:
        report.field(
            f"deal {constant_name('DEAL_ENTRY_', deal.entry)}",
            f"volume {deal.volume:g}, prix {deal.price:.{digits}f}, commission {deal.commission:.2f}, "
            f"frais {deal.fee:.2f}, swap {deal.swap:.2f}, profit {deal.profit:.2f}",
        )
    report.field("commission entrée / sortie", f"{result.commission_in:.2f} / {result.commission_out:.2f} {currency}")
    report.field("commission par lot aller-retour", f"{result.commission_per_lot_round_trip:.2f} {currency}")
    report.field("résultat net du test", f"{result.net_result:.2f} {currency}")
    for note in result.notes:
        report.line(f"  NOTE      {note}")
    return EXIT_OK


def run_connection_check(
    broker: Broker,
    settings: Settings,
    secrets: Secrets,
    *,
    now_utc: Callable[[], datetime],
    report: Report,
    reports_dir: Path,
    stamp: str,
    test_order: bool,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Affiche l'état du terminal, du compte et du symbole ; renvoie un code de sortie."""
    rule = ServerTimeRule.from_config(settings.server_time)
    schedule = MarketSchedule.from_config(settings.market_hours)
    display_tz = ZoneInfo(settings.bot.display_timezone)
    findings: list[Finding] = []
    now = now_utc()

    report.line(f"=== Vérification de la connexion MT5 - goldbot {__version__} ===")
    report.field(
        "heure",
        f"{now.astimezone(display_tz):%Y-%m-%d %H:%M:%S} ({settings.bot.display_timezone}), {now:%H:%M:%S} UTC",
    )
    report.field("package MetaTrader5", _package_version())
    version, build, release = broker.version()
    report.field("terminal MT5", f"version {version}, build {build} ({release})")

    terminal = broker.terminal()
    report.section("Terminal")
    report.field("connecté au serveur", _yes(terminal.connected))
    report.field("bouton Algo Trading", "activé" if terminal.trade_allowed else "DÉSACTIVÉ")
    report.field("API Python bloquée", _yes(terminal.tradeapi_disabled))
    report.field("Max bars in chart", terminal.maxbars)
    report.field("latence (ping)", f"{terminal.ping_last / 1000:.1f} ms")
    report.field("terminal", terminal.path)
    report.field("dossier Common", terminal.commondata_path)
    findings += check_terminal(terminal)

    account = broker.account()
    report.section("Compte (login, serveur et nom masqués)")
    report.field("type", _ACCOUNT_TYPES.get(account.trade_mode, str(account.trade_mode)))
    report.field("société (entité)", account.company)
    report.field("devise", account.currency)
    report.field("levier", f"1:{account.leverage}")
    report.field("mode de marge", constant_name("ACCOUNT_MARGIN_MODE_", account.margin_mode))
    report.field("trading autorisé (compte)", _yes(account.trade_allowed))
    report.field("trading auto autorisé (serveur)", _yes(account.trade_expert))
    unit = "%" if account.margin_so_mode == C.ACCOUNT_STOPOUT_MODE_PERCENT else account.currency
    report.field("appel de marge / stop-out", f"{account.margin_so_call:g} {unit} / {account.margin_so_so:g} {unit}")
    report.field("balance / equity", f"{account.balance:.2f} / {account.equity:.2f} {account.currency}")
    report.field("clôture FIFO imposée", _yes(account.fifo_close))
    report.field("LIVE_TRADING (.env)", "true" if secrets.live_trading else "false")
    findings += check_account(account, live_trading=secrets.live_trading, expected_login=secrets.login)

    report.section("Symbole de l'or")
    spec: SymbolSpec | None = None
    try:
        spec, candidates = resolve_gold_symbol(broker, settings.symbol)
    except (SymbolSelectionError, BrokerError) as exc:
        findings.append(Finding(Level.ERROR, f"Symbole : {exc}"))
        report.line(f"  ERREUR    {exc}")

    if spec is not None:
        _report_symbol(report, spec, candidates, account.currency)
        dump_path = reports_dir / f"symbol_info_{spec.name.replace('.', '_')}_{stamp}.json"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(json.dumps(dict(spec.raw), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        report.field("dump complet (JSON)", dump_path)
        findings += check_symbol(spec, test_volume=settings.test_order.volume)

        tick = _first_tick(broker, spec.name, sleep)
        time_check = check_server_time(tick, now, rule, schedule)
        report.section("Heure serveur")
        report.field(
            "règle",
            f"heure de {settings.server_time.reference_timezone} {settings.server_time.offset_hours:+d} h, "
            f"soit {format_offset(time_check.expected_offset_s)} actuellement",
        )
        report.field("heure serveur actuelle", f"{rule.to_server(now):%Y-%m-%d %H:%M:%S}")
        report.field("marché", "ouvert" if time_check.market_open else "fermé")
        if time_check.market_open:
            report.field(
                "décalage mesuré (tick - PC)",
                f"{time_check.measured_offset_s:+.0f} s ({format_offset(time_check.measured_offset_s)})",
            )
            report.field("écart à la règle", f"{time_check.residual_s:+.0f} s")
        findings += time_check.findings

        feed = evaluate_feed(tick, now, rule, schedule, settings.feed)
        report.section("Flux de prix")
        report.field(
            "dernier tick",
            f"bid {tick.bid:.{spec.digits}f} / ask {tick.ask:.{spec.digits}f} "
            f"(spread {(tick.ask - tick.bid) / spec.point:.0f} points)",
        )
        try:
            tick_time = rule.server_epoch_to_utc(tick.time_msc / 1000).astimezone(display_tz)
            report.field("heure du tick", f"{tick_time:%Y-%m-%d %H:%M:%S} ({settings.bot.display_timezone})")
        except InvalidServerTime as exc:
            report.field("heure du tick", str(exc))
        report.field("âge du tick", f"{feed.tick_age_s:.1f} s")
        report.field("état du flux", feed.state.value)
        if feed.state is FeedState.STALE:
            message = f"Flux figé : dernier tick il y a {feed.tick_age_s:.0f} s alors que le marché cote"
            findings.append(Finding(Level.ERROR, message))

        report.section("Marge et valeur du mouvement (calculées par le terminal)")
        volume = settings.test_order.volume
        try:
            margin = broker.calc_margin(C.ORDER_TYPE_BUY, spec.name, volume, tick.ask)
            move = broker.calc_profit(C.ORDER_TYPE_BUY, spec.name, 1.0, tick.ask, tick.ask + 1.0)
            report.field(f"marge pour {volume:g} lot", f"{margin:.2f} {account.currency}")
            report.field("hausse de 1,00 pour 1 lot", f"{move:.2f} {account.currency}")
            report.field(f"hausse de 1,00 pour {volume:g} lot", f"{move * volume:.2f} {account.currency}")
        except BrokerError as exc:
            findings.append(Finding(Level.WARN, f"Calcul de marge ou de profit impossible : {exc}"))

    report.section("Bilan")
    for finding in findings:
        report.finding(finding)
    errors = sum(f.level is Level.ERROR for f in findings)
    warnings = sum(f.level is Level.WARN for f in findings)
    report.line()
    report.line(f"Résultat : {errors} erreur(s), {warnings} avertissement(s)")
    exit_code = EXIT_OK if errors == 0 else EXIT_PROBLEMS

    if test_order:
        if errors or spec is None:
            report.section("Ordre de test (compte DÉMO uniquement)")
            report.line("  REFUSÉ    corrige d'abord les erreurs ci-dessus")
            return EXIT_REFUSED
        exit_code = _report_test_order(report, broker, settings, spec, account, rule, schedule, now, sleep)
    return exit_code


def _mt5_broker(settings: Settings, secrets: Secrets) -> Broker:
    return MT5Broker(
        login=secrets.login,
        password=secrets.password,
        server=secrets.server,
        path=secrets.terminal_path,
        timeout_ms=settings.mt5.timeout_ms,
    )


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    broker_factory: Callable[[Settings, Secrets], Broker] = _mt5_broker,
    now_utc: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(
        prog="check_connection.py",
        description="Vérifie la connexion au terminal MT5 (Windows) et l'état du compte.",
    )
    parser.add_argument(
        "--test-order",
        action="store_true",
        help="ouvre puis ferme 0,01 lot avec SL/TP pour mesurer la commission (compte DÉMO uniquement)",
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

    reports_dir = _resolve(root, settings.paths.reports)
    setup_logging(
        log_dir=_resolve(root, settings.paths.logs),
        level=settings.logging.level,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
        display_timezone=settings.bot.display_timezone,
        secrets=secrets.sensitive_values(),
    )
    report = Report(SecretRedactor(secrets.sensitive_values()), echo)
    now_utc = now_utc or (lambda: datetime.now(UTC))
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    broker = broker_factory(settings, secrets)
    # Diagnostic interactif : peu d'essais, pour échouer vite et clairement (le bot live garde la config).
    reconnect = settings.mt5.reconnect.model_copy(
        update={"max_attempts": min(settings.mt5.reconnect.max_attempts, CHECK_MAX_ATTEMPTS)}
    )
    if secrets.terminal_path is None:
        report.line("MT5_PATH vide : le module cherche lui-même le terminal MT5 installé.")
    report.line(
        f"Connexion au terminal MT5 en cours (jusqu'à {settings.mt5.timeout_ms / 1000:.0f} s par essai, "
        f"{reconnect.max_attempts} essais au maximum)..."
    )
    try:
        try:
            ConnectionSupervisor(broker, reconnect, sleep=sleep).connect()
        except BrokerError as exc:
            log.error("connexion au terminal MT5 impossible : %s", exc)
            report.line(f"ERREUR : connexion au terminal MT5 impossible : {exc}")
            return EXIT_NO_CONNECTION
        log.info("connecté au terminal MT5 ; vérification en cours (ordre de test : %s)", args.test_order)
        try:
            exit_code = run_connection_check(
                broker,
                settings,
                secrets,
                now_utc=now_utc,
                report=report,
                reports_dir=reports_dir,
                stamp=stamp,
                test_order=args.test_order,
                sleep=sleep,
            )
        except BrokerError as exc:
            log.error("vérification interrompue : %s", exc)
            report.line(f"ERREUR : {exc}")
            return EXIT_PROBLEMS
        finally:
            broker.shutdown()
        log.info("vérification terminée, code de sortie %d", exit_code)
        return exit_code
    finally:
        report_path = reports_dir / f"check_connection_{stamp}.txt"
        report.save(report_path)
        echo(f"(rapport enregistré dans {report_path})")
