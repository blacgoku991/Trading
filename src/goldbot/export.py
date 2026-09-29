"""Export de l'historique de l'or depuis MT5 (logique de scripts/export_history.py, testable sans MT5)."""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from importlib import metadata
from pathlib import Path

from goldbot import __version__
from goldbot.broker.base import AccountState, Broker, BrokerError, SymbolSpec, TerminalState
from goldbot.broker.checks import MIN_MAXBARS, check_server_time
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, Secrets, Settings, load_secrets, load_settings
from goldbot.data.history import ExportResult, export_bars, export_ticks, megabytes, safe_name, thousands
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.monitoring.logging_setup import SecretRedactor, setup_logging

log = logging.getLogger("goldbot.export")

EXIT_OK = 0
EXIT_PROBLEMS = 1
EXIT_NO_CONNECTION = 2
EXIT_CONFIG = 4

MANIFEST_NAME = "manifest.json"
# Champs de symbol_info utiles au backtest (taille du contrat, valeur du point, swaps…).
_SYMBOL_FIELDS = (
    "name",
    "description",
    "digits",
    "point",
    "trade_tick_size",
    "trade_tick_value",
    "trade_contract_size",
    "volume_min",
    "volume_max",
    "volume_step",
    "trade_stops_level",
    "trade_freeze_level",
    "filling_mode",
    "swap_mode",
    "swap_long",
    "swap_short",
    "swap_rollover3days",
    "spread",
    "spread_float",
    "currency_base",
    "currency_profit",
    "currency_margin",
)


def _package_version() -> str:
    try:
        return metadata.version("MetaTrader5")
    except metadata.PackageNotFoundError:
        return "non installé"


def _duration(seconds: float) -> str:
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"


def _clean_previous_export(out_dir: Path, symbol: str) -> int:
    """Supprime les fichiers d'un export précédent (uniquement ceux créés par ce script)."""
    patterns = (f"{safe_name(symbol)}_M1_*.parquet", f"{safe_name(symbol)}_ticks_*.parquet", MANIFEST_NAME)
    removed = 0
    for pattern in patterns:
        for path in out_dir.glob(pattern):
            path.unlink()
            removed += 1
    return removed


def _section(result: ExportResult, since: date, until: date, **extra: object) -> dict[str, object]:
    return {
        **extra,
        "since_server_date": since.isoformat(),
        "until_server_date_excluded": until.isoformat(),
        "rows": result.rows,
        "files": [asdict(record) for record in result.files],
        "unstable_chunks": result.unstable,
        "empty_chunks": result.empty,
        "failed_chunks": result.failed,
    }


def build_manifest(
    *,
    created_utc: datetime,
    version: tuple[int, int, str],
    terminal: TerminalState,
    account: AccountState,
    spec: SymbolSpec,
    settings: Settings,
    offset_check: dict[str, object],
    bars: dict[str, object],
    ticks: dict[str, object],
) -> dict[str, object]:
    """Contenu de manifest.json. Aucun secret : ni login, ni serveur, ni chemin du PC."""
    return {
        "format": 1,
        "created_utc": created_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "goldbot_version": __version__,
        "metatrader5_package": _package_version(),
        "terminal": {"version": list(version), "build": terminal.build, "maxbars": terminal.maxbars},
        "account": {"company": account.company, "currency": account.currency, "trade_mode": account.trade_mode},
        "symbol": {name: getattr(spec, name) for name in _SYMBOL_FIELDS},
        "server_time": {
            "reference_timezone": settings.server_time.reference_timezone,
            "offset_hours": settings.server_time.offset_hours,
            "check_at_export": offset_check,
        },
        "bars": bars,
        "ticks": ticks,
    }


def _offset_check(broker: Broker, spec: SymbolSpec, now: datetime, settings: Settings) -> dict[str, object]:
    rule = ServerTimeRule.from_config(settings.server_time)
    schedule = MarketSchedule.from_config(settings.market_hours)
    try:
        check = check_server_time(broker.tick(spec.name), now, rule, schedule)
    except BrokerError as exc:
        return {"market_open": None, "error": str(exc)}
    measured = check.market_open
    return {
        "market_open": check.market_open,
        "expected_offset_s": check.expected_offset_s,
        "measured_offset_s": round(check.measured_offset_s, 1) if measured else None,
        "residual_s": round(check.residual_s, 1) if measured else None,
    }


def run_export(
    broker: Broker,
    supervisor: ConnectionSupervisor,
    settings: Settings,
    *,
    out_dir: Path,
    display_dir: Path,
    since: date,
    tick_days: int,
    now_utc: Callable[[], datetime],
    sleep: Callable[[float], None],
    say: Callable[[str], None],
) -> int:
    rule = ServerTimeRule.from_config(settings.server_time)
    started = now_utc()
    version = broker.version()
    terminal = broker.terminal()
    account = broker.account()
    spec, _ = resolve_gold_symbol(broker, settings.symbol)
    say(f"Terminal MT5 build {version[1]}, symbole {spec.name} ({spec.description}).")
    if terminal.maxbars < MIN_MAXBARS:
        say(
            f"ATTENTION : « Nombre max de barres dans le graphique » = {terminal.maxbars} : l'historique sera "
            "tronqué. Mets « Illimité » (Outils > Options > Graphiques), redémarre MT5 et relance l'export."
        )
    # Jours complets seulement : on s'arrête à minuit (heure serveur) du jour en cours.
    until = rule.to_server(started).date()
    out_dir.mkdir(parents=True, exist_ok=True)
    if _clean_previous_export(out_dir, spec.name):
        say(f"Anciens fichiers d'export supprimés dans {out_dir}.")

    def reconnect() -> object:
        return supervisor.ensure_connected()

    say("")
    say("Barres d'une minute, du plus récent au plus ancien (plusieurs minutes, le terminal télécharge chez Axi) :")
    bars = export_bars(
        broker,
        spec.name,
        since=since,
        until=until,
        out_dir=out_dir,
        rule=rule,
        reconnect=reconnect,
        sleep=sleep,
        progress=say,
    )
    ticks = ExportResult()
    tick_since = until - timedelta(days=tick_days)
    if tick_days > 0:
        say("")
        say(f"Ticks des {tick_days} derniers jours :")
        ticks = export_ticks(
            broker,
            spec.name,
            since=tick_since,
            until=until,
            out_dir=out_dir,
            rule=rule,
            reconnect=reconnect,
            sleep=sleep,
            progress=say,
        )

    manifest = build_manifest(
        created_utc=started,
        version=version,
        terminal=terminal,
        account=account,
        spec=spec,
        settings=settings,
        offset_check=_offset_check(broker, spec, now_utc(), settings),
        bars=_section(bars, since, until, timeframe="M1"),
        ticks=_section(ticks, tick_since, until, flags="COPY_TICKS_ALL"),
    )
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    say("")
    say(f"Terminé en {_duration((now_utc() - started).total_seconds())}.")
    if bars.files:
        # Fichiers écrits du plus récent au plus ancien.
        first, last = bars.files[-1].first_utc or "?", bars.files[0].last_utc or "?"
        say(
            f"  Barres : {thousands(bars.rows)} barres, du {first[:10]} au {last[:10]} (UTC), "
            f"{len(bars.files)} fichiers, {megabytes(bars.size_bytes)}"
        )
    else:
        say("  Barres : AUCUNE barre reçue du terminal.")
    if not bars.files or (bars.files[-1].first_utc or "9999")[:4] > str(until.year - 3):
        say(
            f"  Historique court ? Dans MT5, ouvre un graphique {spec.name} en M1, clique dedans et appuie sur la "
            "touche Début (Home) : le terminal télécharge l'historique ancien. Attends une minute et relance l'export."
        )
    if tick_days > 0:
        say(f"  Ticks : {thousands(ticks.rows)} ticks, {len(ticks.files)} fichiers, {megabytes(ticks.size_bytes)}")
    for label, chunks in (("jamais stabilisées", bars.unstable + ticks.unstable), ("vides", bars.empty)):
        if chunks:
            say(f"  Tranches {label} : {', '.join(chunks)}")
    failed = bars.failed + ticks.failed
    for failure in failed:
        say(f"  ÉCHEC {failure}")

    files = len(bars.files) + len(ticks.files) + 1
    size = bars.size_bytes + ticks.size_bytes + manifest_path.stat().st_size
    say("")
    say(f"Fichiers à m'envoyer : tout le dossier {display_dir} ({files} fichiers, {megabytes(size)}).")
    say(f"Pour l'ouvrir : explorer {display_dir}")
    say("Puis sélectionne tout (Ctrl+A) et glisse les fichiers dans la conversation.")
    log.info("export terminé : %d barres, %d ticks, %d échecs", bars.rows, ticks.rows, len(failed))
    return EXIT_PROBLEMS if failed or not bars.files else EXIT_OK


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    broker_factory: Callable[[Settings, Secrets], Broker] = MT5Broker.from_settings,
    now_utc: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(
        prog="export_history.py",
        description="Exporte l'historique de l'or (barres d'une minute et ticks) depuis le terminal MT5.",
    )
    parser.add_argument("--since", type=date.fromisoformat, help="date la plus ancienne pour les barres (AAAA-MM-JJ)")
    parser.add_argument("--tick-days", type=int, help="nombre de jours de ticks à exporter (0 : aucun)")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--env", type=Path, default=root / ".env")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(args.config)
        secrets = load_secrets(args.env)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG
    if args.tick_days is not None and args.tick_days < 0:
        echo("ERREUR : --tick-days doit être positif ou nul")
        return EXIT_CONFIG

    setup_logging(
        log_dir=_resolve(root, settings.paths.logs),
        level=settings.logging.level,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
        display_timezone=settings.bot.display_timezone,
        secrets=secrets.sensitive_values(),
    )
    redactor = SecretRedactor(secrets.sensitive_values())

    def say(text: str) -> None:
        echo(redactor.redact(text))

    now_utc = now_utc or (lambda: datetime.now(UTC))
    broker = broker_factory(settings, secrets)
    supervisor = ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=sleep)
    say(f"=== Export de l'historique de l'or - goldbot {__version__} ===")
    say("Connexion au terminal MT5 en cours...")
    try:
        supervisor.connect()
    except BrokerError as exc:
        log.error("connexion au terminal MT5 impossible : %s", exc)
        say(f"ERREUR : connexion au terminal MT5 impossible : {exc}")
        return EXIT_NO_CONNECTION
    try:
        return run_export(
            broker,
            supervisor,
            settings,
            out_dir=_resolve(root, settings.export.directory),
            display_dir=settings.export.directory,  # relatif au dossier du repo, d'où la commande est lancée
            since=args.since or settings.export.bars_since,
            tick_days=settings.export.tick_days if args.tick_days is None else args.tick_days,
            now_utc=now_utc,
            sleep=sleep,
            say=say,
        )
    except (BrokerError, SymbolSelectionError) as exc:
        log.error("export interrompu : %s", exc)
        say(f"ERREUR : export interrompu : {exc}")
        return EXIT_PROBLEMS
    finally:
        broker.shutdown()
