"""Rejeu d'une journée sur les ticks Axi (demande de l'utilisateur, 29/09/2026 : « base-toi sur des tests sur la
journée d'aujourd'hui ») : la version démo et des variantes d'un seul réglage chacune, rejouées avec le même code que
le bot (scalping/backtest.py : mêmes signaux, stops, objectifs, lots et limites du compte), aux prix exécutables.

Windows, terminal MT5 ouvert (lecture de l'historique seulement, aucun ordre envoyé) :
    .\\.venv\\Scripts\\python.exe scripts\\rejeu_jour.py
    .\\.venv\\Scripts\\python.exe scripts\\rejeu_jour.py --jour 2026-09-29 --de 21:55 --a 23:00 --capital 4940
Sans MT5, sur des fichiers de scripts/export_history.py : --dir data/export.

Une journée ne prouve rien : le meilleur réglage d'un jour est souvent un autre le lendemain.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

import pandas as pd

from goldbot.broker.base import Broker, BrokerError
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, ScalpingConfig, Secrets, Settings, load_secrets, load_settings
from goldbot.data.history import export_bars, export_ticks, load_bars, load_ticks, server_epoch
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.timezones import ServerTimeRule
from goldbot.monitoring.logging_setup import SecretRedactor
from goldbot.scalping.backtest import Instrument, ScalpResult, run_backtest
from goldbot.scalping.engine import LONG
from goldbot.scalping.live import strategy_label, strategy_versions

EXIT_OK, EXIT_NO_DATA, EXIT_NO_CONNECTION, EXIT_CONFIG = 0, 1, 2, 4
BARS_BEFORE_DAYS = 35  # barres M1 lues avant le jour : tendance, ATR et ATR journalier connus dès la première minute
LOOK_BACK_DAYS = 7  # sans --jour : le jour serveur en cours, ou le dernier jour de cotation avant lui
DAY_LIMITS = ("perte du jour atteinte", "budget de perte du jour")


def variants(cfg: ScalpingConfig) -> dict[str, ScalpingConfig]:
    """La version démo, puis une variante par réglage (objectif, stop, durée, spread, signaux contraires)."""
    two = cfg.two_candles

    def with_two(**update: object) -> ScalpingConfig:
        return cfg.model_copy(update={"two_candles": two.model_copy(update=update)})

    return {
        "version démo": cfg,
        "objectif 30 pips": with_two(target_pips=[30.0]),
        "objectif 50 pips": with_two(target_pips=[50.0]),
        "stop au moins 15 pips": with_two(min_stop_pips=15.0),
        "durée max 5 min": cfg.model_copy(update={"max_hold_s": 300}),
        "durée max 15 min": cfg.model_copy(update={"max_hold_s": 900}),
        "spread max 2,5 pips": cfg.model_copy(update={"max_spread_pips": 2.5}),
        "pause 1 h à la réouverture": cfg.model_copy(update={"no_entry_after_open_min": 60.0}),
        "retournement si gain": cfg.model_copy(update={"opposite_signals": "retourner_si_gain"}),
    }


def select_day(ticks: pd.DataFrame, day: date, start: clock_time | None, end: clock_time | None,
               display_tz: str) -> pd.DataFrame:  # fmt: skip
    """Ticks du jour de cotation `day` (date serveur), éventuellement de `start` à `end` (heure affichée, Paris)."""
    first = server_epoch(day) * 1000
    ticks = ticks[(ticks["time_msc_server"] >= first) & (ticks["time_msc_server"] < first + 86_400_000)]
    if start is not None or end is not None:
        local = pd.to_datetime(ticks["time"], utc=True).dt.tz_convert(display_tz).dt.time
        keep = pd.Series(True, index=ticks.index)
        if start is not None:
            keep &= local >= start
        if end is not None:
            keep &= local < end
        ticks = ticks[keep]
    return ticks.reset_index(drop=True)


def replay_variants(ticks: pd.DataFrame, bars: pd.DataFrame, settings: Settings, instrument: Instrument,
                    capital: float) -> dict[str, ScalpResult]:  # fmt: skip
    """capital : devise de cotation (USD). Chaque variante repart du même capital, sans position ouverte."""
    schedule = MarketSchedule.from_config(settings.market_hours)
    return {
        name: run_backtest(ticks, bars, cfg, instrument=instrument, schedule=schedule, initial_equity=capital,
                           slippage_points=0.0, commission_per_lot_side=settings.backtest.commission_per_lot_side)
        for name, cfg in variants(settings.scalping).items()
    }  # fmt: skip


def report(results: dict[str, ScalpResult], *, settings: Settings, rule: ServerTimeRule, rate: float,
           currency: str) -> list[str]:  # fmt: skip
    """Tableau des variantes, puis les trades de la version démo (heure affichée), montants en devise du compte."""
    pip = settings.scalping.pip_size
    lines = [f"  {'réglage':<28}{'trades':>7}{'gagnants':>10}{'pips':>9}{'résultat':>14}"]
    for name, result in results.items():
        trades = result.trades
        pips = float(((trades["exit"] - trades["entry"]) * trades["side"]).sum() / pip) if len(trades) else 0.0
        wins = float((trades["pnl"] > 0).mean() * 100) if len(trades) else 0.0
        net = float(trades["pnl"].sum() * rate) if len(trades) else 0.0
        notes = []
        if any(reason in DAY_LIMITS for _, reason in result.refusals):
            notes.append("limite du jour atteinte")
        if result.halted_ms is not None:
            notes.append("arrêt total")
        suffix = f"  ({', '.join(notes)})" if notes else ""
        lines.append(f"  {name:<28}{len(trades):>7}{wins:>9.0f} %{pips:>+9.1f}{net:>+10.2f} {currency}{suffix}")
    demo_result = results["version démo"]
    demo = demo_result.trades
    lines.append("")
    reasons: Counter = Counter()
    for (_, reason), count in demo_result.refusals.items():
        reasons[reason] += count
    aside = ", ".join(f"{reason} {count}" for reason, count in reasons.most_common(4)) or "aucun"
    lines.append(f"Version démo : {sum(demo_result.signals.values())} signaux, {len(demo)} trades ; signaux écartés : "
                 f"{aside}.")  # fmt: skip
    if demo.empty:
        lines.append("Version démo : aucun trade sur cette période.")
        return lines
    lines.append("Trades de la version démo (heure de Paris) :")
    demo = demo.sort_values("open_ms")
    opened = rule.server_ms_to_utc(demo["open_ms"].to_numpy()).tz_convert(settings.bot.display_timezone)
    for when, row in zip(opened, demo.itertuples(), strict=True):
        pips = (row.exit - row.entry) * row.side / pip
        held = (row.exit_ms - row.open_ms) / 1000
        lines.append(f"  {when:%H:%M:%S} {'ACHAT' if row.side == LONG else 'VENTE'} {row.lots:g} lot à {row.entry:.2f}"
                     f" -> {row.exit:.2f} ({row.reason}, {held:.0f} s) {pips:+.1f} pips "
                     f"{row.pnl * rate:+.2f} {currency}")  # fmt: skip
    return lines


def _instrument(symbol: dict[str, object]) -> tuple[Instrument, float]:
    """Instrument du rejeu et valeur, en devise du compte, d'un mouvement de 1 $ sur une once."""
    instrument = Instrument(float(symbol["point"]), float(symbol["trade_contract_size"]), float(symbol["volume_min"]),
                            float(symbol["volume_max"]), float(symbol["volume_step"]),
                            int(symbol["trade_stops_level"]), int(symbol["trade_freeze_level"]))  # fmt: skip
    rate = float(symbol["trade_tick_value"]) / float(symbol["trade_tick_size"]) / float(symbol["trade_contract_size"])
    return instrument, rate


def _from_broker(broker: Broker, supervisor: ConnectionSupervisor, settings: Settings, day: date | None,
                 rule: ServerTimeRule, now_utc: datetime, folder: Path, sleep: Callable[[float], None]
                 ) -> tuple[pd.DataFrame, pd.DataFrame, date, dict[str, object], str, float]:  # fmt: skip
    """Ticks du jour et barres M1 d'avant, lus dans le terminal (mêmes fonctions que l'export)."""
    spec, _ = resolve_gold_symbol(broker, settings.symbol)
    account = broker.account()
    days = [day] if day is not None else [rule.to_server(now_utc).date() - timedelta(days=k) for k in range(LOOK_BACK_DAYS)]
    for candidate in days:
        common = dict(out_dir=folder, rule=rule, reconnect=supervisor.ensure_connected, sleep=sleep,
                      progress=lambda _: None)  # fmt: skip
        export_ticks(broker, spec.name, since=candidate, until=candidate + timedelta(days=1), **common)
        try:
            ticks = load_ticks(folder, spec.name)
        except FileNotFoundError:
            continue
        export_bars(broker, spec.name, since=candidate - timedelta(days=BARS_BEFORE_DAYS),
                    until=candidate + timedelta(days=1), **common)  # fmt: skip
        symbol = {name: getattr(spec, name) for name in ("point", "trade_contract_size", "volume_min", "volume_max",
                  "volume_step", "trade_stops_level", "trade_freeze_level", "trade_tick_value", "trade_tick_size")}
        return ticks, load_bars(folder, spec.name), candidate, symbol, account.currency, account.equity
    raise FileNotFoundError("aucun tick sur les jours demandés (marché fermé ?)")


def _clock(text: str) -> clock_time:
    return datetime.strptime(text, "%H:%M").time()


def day_main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    broker_factory: Callable[[Settings, Secrets], Broker] = MT5Broker.from_settings,
    now_utc: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="rejeu_jour.py", description="Rejeu d'une journée : version démo et variantes.")
    parser.add_argument("--jour", type=date.fromisoformat, help="jour de cotation, date serveur (AAAA-MM-JJ)")
    parser.add_argument("--de", type=_clock, help="heure de début (HH:MM, heure de Paris)")
    parser.add_argument("--a", type=_clock, help="heure de fin (HH:MM, heure de Paris)")
    parser.add_argument("--capital", type=float, help="capital de départ (devise du compte ; défaut : equity du compte)")
    parser.add_argument("--dir", type=Path, help="fichiers de scripts/export_history.py au lieu du terminal MT5")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--env", type=Path, default=root / ".env")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG
    rule = ServerTimeRule.from_config(settings.server_time)
    now = (now_utc or (lambda: datetime.now(UTC)))()
    with tempfile.TemporaryDirectory() as temp:
        if args.dir is not None:
            folder = args.dir if args.dir.is_absolute() else root / args.dir
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            symbol, currency = manifest["symbol"], manifest["account"]["currency"]
            ticks, bars = load_ticks(folder, symbol["name"]), load_bars(folder, symbol["name"])
            day = args.jour or datetime.fromtimestamp(int(ticks["time_msc_server"].iloc[-1]) // 1000, tz=UTC).date()
            equity = None
        else:
            try:
                secrets = load_secrets(args.env)
            except ConfigError as exc:
                echo(f"ERREUR de configuration : {exc}")
                return EXIT_CONFIG
            redactor = SecretRedactor(secrets.sensitive_values())  # identifiants jamais affichés (règle 7)
            raw_echo = echo

            def echo(text: str) -> None:
                raw_echo(redactor.redact(text))

            broker = broker_factory(settings, secrets)
            supervisor = ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=sleep)
            echo("Lecture de l'historique dans le terminal MT5 (aucun ordre n'est envoyé)...")
            try:
                supervisor.connect()
                ticks, bars, day, symbol, currency, equity = _from_broker(broker, supervisor, settings, args.jour, rule,
                                                                          now, Path(temp), sleep)  # fmt: skip
            except (BrokerError, SymbolSelectionError) as exc:
                echo(f"ERREUR : terminal MT5 : {exc}")
                return EXIT_NO_CONNECTION
            except FileNotFoundError as exc:
                echo(f"ERREUR : {exc}")
                return EXIT_NO_DATA
            finally:
                broker.shutdown()
        instrument, rate = _instrument(symbol)
        day_ticks = select_day(ticks, day, args.de, args.a, settings.bot.display_timezone)
        if day_ticks.empty:
            echo(f"Aucun tick le {day:%d/%m/%Y} sur la période demandée (marché fermé ?).")
            return EXIT_NO_DATA
        first_s, last_s = (int(day_ticks["time_msc_server"].iloc[k]) // 1000 for k in (0, -1))
        bars = bars[(bars["time_server"] >= first_s - BARS_BEFORE_DAYS * 86_400) & (bars["time_server"] <= last_s)]
        capital = args.capital if args.capital is not None else (equity if equity is not None else 5000.0)
        results = replay_variants(day_ticks, bars.reset_index(drop=True), settings, instrument, capital / rate)
    cfg = settings.scalping
    labels = [strategy_label(code, cfg, version, digest) for code, (version, digest, _) in strategy_versions(cfg).items()]
    window = "toute la journée" if args.de is None and args.a is None else \
        f"de {args.de or clock_time(0, 0):%H:%M} à {args.a or clock_time(23, 59):%H:%M}"  # fmt: skip
    echo(f"=== Rejeu du {day:%d/%m/%Y} ({window}, heure de Paris), capital de départ {capital:.2f} {currency} ===")
    echo(f"Version démo : {', '.join(labels)}. Même code que le bot (signaux, stops, objectifs, lots, limites du compte),")
    echo("prix exécutables sans glissement ; chaque variante change un seul réglage. Une journée ne prouve rien : le")
    echo("meilleur réglage d'un jour est souvent un autre le lendemain.")
    for line in report(results, settings=settings, rule=rule, rate=rate, currency=currency):
        echo(line)
    return EXIT_OK
