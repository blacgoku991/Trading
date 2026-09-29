"""Contrôle qualité des données exportées (logique de scripts/data_report.py). Tourne partout."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from goldbot.config import ConfigError, load_settings
from goldbot.data.history import exported_symbols, load_bars, load_ticks, minute_history_start, verify_files
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.quality import render_report
from goldbot.data.timezones import ServerTimeRule

EXIT_OK = 0
EXIT_ALERTS = 1
EXIT_NO_DATA = 2
EXIT_CONFIG = 4


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    now_utc: Callable[[], datetime] | None = None,
    echo: Callable[[str], None] = print,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="data_report.py", description="Contrôle qualité des données exportées.")
    parser.add_argument("--dir", type=Path, help="dossier des fichiers exportés (défaut : export.directory)")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG

    folder = _resolve(root, args.dir or settings.export.directory)
    manifest_path = folder / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    symbols = [manifest["symbol"]["name"]] if manifest else exported_symbols(folder)
    if len(symbols) != 1:
        echo(f"ERREUR : un seul symbole attendu dans {folder}, trouvé : {symbols or 'aucun'}")
        return EXIT_NO_DATA
    symbol = symbols[0]
    point = float(manifest["symbol"]["point"]) if manifest else 0.01
    if not manifest:
        echo("ATTENTION : manifest.json absent, point supposé égal à 0,01")

    echo(f"Lecture des fichiers de {symbol} dans {folder}...")
    bars = load_bars(folder, symbol, deduplicate=False)
    start = minute_history_start(bars)
    if start is None:
        echo("ERREUR : aucune période avec de vraies barres d'une minute (au moins 1 000 barres par jour)")
        return EXIT_NO_DATA
    sparse = int((bars["time_server"] < start).sum())
    bars = bars[bars["time_server"] >= start].reset_index(drop=True)
    first_day = datetime.fromtimestamp(start, UTC).date()
    note = (
        f"Historique minute exploitable à partir du {first_day} (date serveur)"
        + (f" ; {sparse} barres éparses plus anciennes (environ une par jour) ignorées." if sparse else ".")
    )
    echo(note)
    ticks = load_ticks(folder, symbol) if any(folder.glob("*_ticks_*.parquet")) else None
    text, alerts = render_report(
        bars,
        ticks,
        rule=ServerTimeRule.from_config(settings.server_time),
        schedule=MarketSchedule.from_config(settings.market_hours),
        point=point,
        zone=settings.bot.display_timezone,
        file_problems=verify_files(folder),
        notes=[note],
    )
    stamp = (now_utc or (lambda: datetime.now(UTC)))().strftime("%Y%m%dT%H%M%SZ")
    report_path = _resolve(root, settings.paths.reports) / f"qualite_donnees_{stamp}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(text, encoding="utf-8")
    echo(f"{len(bars)} barres, {0 if ticks is None else len(ticks)} ticks analysés.")
    for alert in alerts:
        echo(f"  ALERTE {alert}")
    echo(f"Rapport : {report_path}")
    return EXIT_ALERTS if alerts else EXIT_OK
