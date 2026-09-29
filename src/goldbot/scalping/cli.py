"""Commandes de l'expérience de scalping (scripts/run_scalp.py et scripts/backtest_scalp.py).

Compte DÉMO obligatoire, sans exception possible : aucun passage automatique en réel.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from goldbot import __version__
from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, Secrets, Settings, load_secrets, load_settings
from goldbot.data.history import load_bars, load_ticks
from goldbot.data.market_hours import MarketSchedule
from goldbot.execution.orders import OrderRejected, filling_candidates, select_filling, send_market_order
from goldbot.live.lock import InstanceLock
from goldbot.monitoring.logging_setup import setup_logging
from goldbot.scalping.backtest import Instrument, run_backtest, summary
from goldbot.scalping.live import DemoAccountChanged, ScalpRunner
from goldbot.scalping.store import CLOSED, OPEN, VERIFY_PREFIX, ScalpStore, params_hash
from goldbot.scalping.learning import SignalModel, strategy_signature

log = logging.getLogger("goldbot.scalp")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_CONNECTION = 2
EXIT_REFUSED = 3
EXIT_CONFIG = 4
EXIT_LOCKED = 5

POLL_SECONDS = 0.5
REPORT_EVERY = pd.Timedelta(minutes=15)
STATUS_EVERY = pd.Timedelta(minutes=5)
VERIFY_HOLD_S = 15  # la vérification ferme la position de test au bout de 15 s
VERIFY_STOP = 3.0  # stop de la position de test à 3 $ (0,01 lot : environ 3 $ de risque)
VERIFY_TIGHTEN = 0.5  # puis stop resserré de 0,50 $ (même requête que pour reposer un stop manquant)


class Refused(Exception):
    pass


def _resolve(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def _open_store(path: Path, settings: Settings, *, new_experiment: bool, say: Callable[[str], None],
                model_key: str = "none") -> ScalpStore:
    """Réglages figés pendant l'expérience : un changement exige --nouvelle-experience (l'ancienne est archivée)."""
    current = params_hash({**settings.scalping.model_dump(), "model": model_key,
                           "strategy_signature": strategy_signature(settings)})
    store = ScalpStore(path)
    saved = store.meta("params_hash")
    if saved is not None and saved != current and store.has_activity():
        if not new_experiment:
            store.close()
            raise Refused(
                "les réglages de l'expérience ont changé en cours de collecte. Remets-les comme avant, ou lance "
                "une nouvelle expérience avec --nouvelle-experience (l'ancienne sera archivée)"
            )
        if store.demo_trades(OPEN, "envoi"):
            store.close()
            raise Refused("positions encore ouvertes : terminer l'expérience avant d'archiver ses réglages")
        store.close()
        archive = path.with_name(f"{path.stem}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}{path.suffix}")
        path.rename(archive)
        say(f"Ancienne expérience archivée : {archive.name}")
        store = ScalpStore(path)
    if store.meta("params_hash") != current:
        store.set_meta("params_hash", current)
        store.set_meta("started_utc", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
        store.set_meta("strategy_signature", strategy_signature(settings))
    return store


def _verify(runner: ScalpRunner, store_path: Path, settings: Settings, say: Callable[[str], None]) -> bool:
    """Ouverture, stop serveur (présent puis modifié), redémarrage, sortie à durée maximale, estimé contre exécuté.

    Le trade de test est gardé dans l'état (pour la reprise après redémarrage) mais exclu des bilans.
    """
    broker, spec, cfg = runner.broker, runner.spec, runner.cfg
    checks: list[tuple[str, bool, str]] = []
    now_ms = runner.server_now_ms()
    if not runner._is_open(now_ms) or not runner._is_open(now_ms + (VERIFY_HOLD_S + 60) * 1000):
        say("Marché fermé (ou fermeture imminente) : relance la vérification pendant les heures de cotation.")
        return False
    tick = broker.tick(spec.name)
    tag = f"{VERIFY_PREFIX}{now_ms}"
    request = {
        "action": C.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": spec.volume_min,
        "type": C.ORDER_TYPE_BUY,
        "sl": round(tick.bid - VERIFY_STOP, spec.digits),
        "tp": round(tick.ask + cfg.target_ratio * VERIFY_STOP, spec.digits),
        "deviation": 20,
        "magic": cfg.magic,
        "comment": tag,
        "type_time": C.ORDER_TIME_GTC,
    }
    say(f"Ouverture d'un achat de test ({spec.volume_min:g} lot, stop à {VERIFY_STOP:.2f} $)...")
    try:
        filling, _ = select_filling(broker, {**request, "price": tick.ask}, spec.filling_mode)
        candidates = filling_candidates(spec.filling_mode)
        send_market_order(broker, request, candidates[candidates.index(filling) :], sleep=runner.sleep)
    except (OrderRejected, BrokerError) as exc:
        say(f"  ÉCHEC ouverture : {exc}")
        return False
    position = runner._find(tag)
    checks.append(("ouverture d'une position de test (achat, lot minimal)", position is not None, ""))
    if position is None:
        say("  ÉCHEC : position introuvable, vérifie l'onglet Trade de MT5 (et ferme-la à la main si besoin)")
        return False
    checks.append(("stop côté serveur présent dès l'entrée", position.sl > 0, f"stop {position.sl:.2f}"))
    runner.store.record(tag, now_ms, 1, OPEN, volume=position.volume, sl=position.sl, tp=position.tp,
                        entry=tick.ask, risk=0.0)  # fmt: skip
    runner.store.update(tag, position=position.ticket, open_price=position.price_open,
                        deadline_ms=int(position.time_msc) + VERIFY_HOLD_S * 1000)  # fmt: skip
    # Stop resserré côté serveur (jamais éloigné) : c'est la requête utilisée pour reposer un stop manquant.
    tighter = round(position.sl + VERIFY_TIGHTEN, spec.digits)
    result = broker.order_send(
        {
            "action": C.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": position.ticket,
            "sl": tighter,
            "tp": position.tp,
            "magic": cfg.magic,
        }
    )
    moved = runner._find(tag)
    stop_moved = result.retcode == C.TRADE_RETCODE_DONE and moved is not None and abs(moved.sl - tighter) < spec.point
    checks.append(
        (
            f"stop modifiable côté serveur (resserré de {VERIFY_TIGHTEN:.2f} $)",
            stop_moved,
            f"stop {moved.sl:.2f}" if stop_moved else f"retcode {result.retcode} {result.comment}",
        )
    )
    if stop_moved:
        runner.store.update(tag, sl=moved.sl)
    runner.store.close()
    # Redémarrage simulé : un nouveau bot relit l'état et reprend la position.
    restarted = ScalpRunner(
        broker,
        runner.supervisor,
        ScalpStore(store_path),
        settings=settings,
        spec=spec,
        local_only=False,
        now_utc=runner.now_utc,
        say=say,
        sleep=runner.sleep,
    )
    resumed = tag in [t.tag for t in restarted.store.demo_trades(OPEN)]
    checks.append(("redémarrage : position reprise depuis l'état enregistré", resumed, ""))
    say(f"  Position {position.ticket} ouverte à {position.price_open:.2f} ; sortie forcée dans {VERIFY_HOLD_S} s...")
    for _ in range(int((VERIFY_HOLD_S + 30) / POLL_SECONDS)):
        restarted._manage_positions(restarted.server_now_ms())
        if restarted.store.signal(tag).get("status") == CLOSED:
            break
        runner.sleep(POLL_SECONDS)
    result_row = restarted.store.signal(tag)
    closed = result_row.get("status") == CLOSED
    checks.append(("clôture à la durée maximale, même en perte", closed, str(result_row.get("exit_reason") or "")))
    if closed:
        estimate, real = result_row.get("est_pnl"), result_row.get("real_pnl")
        both = estimate is not None and real is not None
        checks.append(
            (
                "résultat estimé avant clôture rapproché du résultat exécuté (deals, commissions comprises)",
                both,
                f"estimé {estimate:+.2f}, exécuté {real:+.2f} {runner.currency}" if both else "",
            )
        )
    say("")
    say("Vérification technique :")
    for label, ok, detail in checks:
        say(f"  {'OK   ' if ok else 'ÉCHEC'} {label}{f' ({detail})' if detail else ''}")
    restarted.store.close()
    if not closed:
        say("  La position de test est encore ouverte (stop serveur en place) : relance --verification ou ferme-la.")
    return all(ok for _, ok, _ in checks)


def live_main(
    argv: list[str] | None = None,
    *,
    root: Path | None = None,
    broker_factory: Callable[[Settings, Secrets], Broker] = MT5Broker.from_settings,
    now_utc: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = print,
    max_steps: int | None = None,
    default_config: Path | None = None,
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="run_scalp.py", description="Expérience de scalping sur compte DÉMO.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--verification", action="store_true", help="test technique : ouverture, stop, redémarrage, sortie"
    )
    mode.add_argument("--simulation", action="store_true", help="tout est calculé, aucun ordre n'est envoyé")
    parser.add_argument("--bilan", action="store_true", help="affiche les deux bilans et s'arrête")
    parser.add_argument("--nouvelle-experience", action="store_true", help="archive l'expérience en cours")
    parser.add_argument("--config", type=Path, default=default_config or root / "config" / "settings.yaml")
    parser.add_argument("--env", type=Path, default=root / ".env")
    parser.add_argument("--minutes", type=float, help="arrête les entrées et ferme les positions après cette durée")
    parser.add_argument("--model", type=Path, help="JSON entraîné : affiche ses scores, sans filtrer par défaut")
    parser.add_argument("--filter-model", action="store_true", help="applique le modèle admissible au filtre démo")
    args = parser.parse_args(argv)
    if args.bilan and args.verification:
        parser.error("--bilan ne se combine pas avec --verification")
    if args.minutes is not None and (not 0 < args.minutes <= 1440):
        parser.error("--minutes doit être compris entre 0 et 1440")
    if args.filter_model and args.model is None:
        parser.error("--filter-model nécessite --model")
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

    def say(text: str) -> None:
        echo(text)
        log.info(text)

    now_utc = now_utc or (lambda: datetime.now(UTC))
    lock = InstanceLock(root / "data" / "scalp.lock")
    if not args.bilan and not lock.acquire():
        echo("ERREUR : l'expérience de scalping tourne déjà sur ce PC.")
        return EXIT_LOCKED
    broker = broker_factory(settings, secrets)
    supervisor = ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=sleep)
    name = settings.scalping.experiment_name
    store_path = root / "data" / (f"{name}_simulation.sqlite" if args.simulation else f"{name}.sqlite")
    store = None
    say(f"=== Expérience de scalping - goldbot {__version__} - réglages expérimentaux, pas une stratégie rentable ===")
    try:
        try:
            supervisor.connect()
        except BrokerError as exc:
            echo(f"ERREUR : connexion au terminal MT5 impossible : {exc}")
            return EXIT_NO_CONNECTION
        account = broker.account()
        if account.trade_mode != C.ACCOUNT_TRADE_MODE_DEMO:
            raise Refused("compte non démo : l'expérience de scalping ne tourne QUE sur un compte démo")
        if account.margin_mode != C.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise Refused("compte en netting : le magic ne suffit plus à isoler les autres positions")
        if not args.simulation and not args.bilan:
            if not account.trade_allowed:
                raise Refused("trading refusé sur ce compte (connexion avec le mot de passe investisseur ?)")
            if not account.trade_expert:
                raise Refused("le serveur interdit le trading automatique sur ce compte")
            if not broker.terminal().trade_allowed:
                raise Refused("bouton Algo Trading désactivé dans MT5 : active-le (il doit être vert)")
        spec, _ = resolve_gold_symbol(broker, settings.symbol)
        model = None
        if args.model:
            try:
                model = SignalModel.load(_resolve(root, args.model), settings)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise Refused(f"modèle inutilisable : {exc}") from exc
            if args.filter_model and not model.approved:
                raise Refused("ce modèle n'a pas amélioré le test réservé : observation seule possible")
        if args.bilan or args.verification:
            store = ScalpStore(store_path)  # lecture des bilans / test technique : pas de contrôle des réglages
        else:
            model_key = f"{model.digest}:{args.filter_model}" if model else "none"
            store = _open_store(store_path, settings, new_experiment=args.nouvelle_experience, say=say,
                                model_key=model_key)
            if store.meta("start_equity") is None:
                store.set_meta("start_equity", str(account.equity))
        if not args.bilan:
            interrupted = store.interrupt_open_sims()  # suivi en mémoire : perdu à l'arrêt précédent
            if interrupted:
                say(f"{interrupted} trade(s) simulé(s) interrompu(s) par l'arrêt précédent (exclus du bilan 2).")
        runner = ScalpRunner(broker, supervisor, store, settings=settings, spec=spec, local_only=args.simulation,
                             now_utc=now_utc, say=say, sleep=sleep, model=model,
                             filter_model=args.filter_model)  # fmt: skip
        if model and model.payload["data_through_ms"] > runner.server_now_ms():
            raise Refused("modèle contenant des observations postérieures à l'horloge du terminal")
        if args.bilan:
            for line in runner.reports():
                echo(line)
            return EXIT_OK
        if args.verification:
            return EXIT_OK if _verify(runner, store_path, settings, say) else EXIT_FAILED
        cfg = settings.scalping
        say(f"Stratégie {cfg.strategy} ; plafond {cfg.max_entries_per_minute} entrées / 60 s (pas un quota).")
        say("Apprentissage : " + ("filtre figé actif" if args.filter_model else
            "scores du modèle en observation" if model else "collecte des indicateurs et résultats, aucun modèle entraîné chargé"))
        day = store.trading_days()
        say(
            f"Compte DÉMO, equity {account.equity:.2f} {account.currency}. Risque {cfg.risk_per_trade_pct:g} % "
            f"par trade, {cfg.max_open_positions} positions au plus, stop -{cfg.daily_loss_pct:g} % par jour."
        )
        say(
            f"Jour de collecte {day + 1} sur {cfg.experiment_days} annoncés. Ne pas modifier les réglages avant la fin."
        )
        if not cfg.cadence_verified and not args.simulation:
            say(
                f"Plafond d'envoi configuré : une entrée toutes les {cfg.broker_min_seconds_between_orders:g} s. "
                "Ce réglage ne constitue pas une confirmation des conditions Axi."
            )
        say("Ctrl+C pour arrêter (les positions de l'expérience sont alors fermées). Bilans toutes les 15 minutes.")
        steps = 0
        last_report = last_status = pd.Timestamp(now_utc())
        deadline = last_status + pd.Timedelta(minutes=args.minutes) if args.minutes is not None else None
        try:
            while max_steps is None or steps < max_steps:
                if deadline is not None and pd.Timestamp(now_utc()) >= deadline:
                    say("Durée de l'essai terminée : fermeture des positions de l'expérience...")
                    try:
                        left = runner.close_all("fin de l'essai")
                    except BrokerError as exc:
                        say(f"Fermeture non confirmée : {exc}. Relance le bot pour reprendre les positions.")
                        return EXIT_FAILED
                    if left:
                        say(f"{left} position(s) encore ouverte(s), stop serveur conservé ; relance pour les gérer.")
                        return EXIT_FAILED
                    break
                try:
                    runner.step()
                    now = pd.Timestamp(now_utc())
                    if now - last_status >= STATUS_EVERY:
                        last_status = now
                        say(runner.status_line())
                    if now - last_report >= REPORT_EVERY:
                        last_report = now
                        for line in runner.reports():
                            say(line)
                except BrokerError as exc:
                    log.warning("passage interrompu (%s) : nouvel essai", exc)
                steps += 1
                sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            say("Arrêt demandé : fermeture des positions de l'expérience...")
            try:
                left = runner.close_all("arrêt du bot")
            except BrokerError as exc:
                say(f"Fermeture impossible ({exc}) : les stops restent sur le serveur ; relance le bot pour les gérer.")
            else:
                if left:
                    say(f"{left} position(s) encore ouverte(s) (marché fermé ?) : leur stop reste sur le serveur ; "
                        "relance le bot pour les gérer.")  # fmt: skip
        try:
            for line in runner.reports():
                say(line)
        except BrokerError as exc:
            echo(f"Bilans indisponibles ({exc}) : relance avec --bilan.")
        return EXIT_OK
    except (Refused, DemoAccountChanged, SymbolSelectionError) as exc:
        echo(f"REFUS : {exc}")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        echo("Arrêt demandé : les positions ouvertes gardent leur stop sur le serveur ; relance pour leur sortie.")
        return EXIT_OK
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass  # --verification ferme déjà l'ancien store avant de le rouvrir
        broker.shutdown()
        lock.release()


def backtest_main(
    argv: list[str] | None = None, *, root: Path | None = None, echo: Callable[[str], None] = print
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="backtest_scalp.py", description="Expérience de scalping sur les ticks.")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--dir", type=Path, help="dossier des données (défaut : export.directory)")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return EXIT_CONFIG
    folder = _resolve(root, args.dir or settings.export.directory)
    symbol = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["symbol"]
    instrument = Instrument(
        float(symbol["point"]),
        float(symbol["trade_contract_size"]),
        float(symbol["volume_min"]),
        float(symbol["volume_max"]),
        float(symbol["volume_step"]),
        int(symbol["trade_stops_level"]),
        int(symbol["trade_freeze_level"]),
    )
    # Valeur en devise du compte d'un mouvement de 1 $ sur 1 once (au moment de l'export).
    rate = float(symbol["trade_tick_value"]) / float(symbol["trade_tick_size"]) / float(symbol["trade_contract_size"])
    ticks = load_ticks(folder, symbol["name"])
    bars = load_bars(folder, symbol["name"])
    bars = bars[bars["time_server"] >= ticks["time_msc_server"].iloc[0] // 1000 - 7 * 86_400].reset_index(drop=True)
    days = (ticks["time_msc_server"].iloc[-1] - ticks["time_msc_server"].iloc[0]) / 86_400_000 * 5 / 7
    cfg = settings.scalping
    start = 5000.0 / rate
    echo(f"Ticks du {pd.Timestamp(ticks['time'].iloc[0]):%Y-%m-%d} au {pd.Timestamp(ticks['time'].iloc[-1]):%Y-%m-%d}, "
         f"départ 5 000 (devise du compte), réglages de config/settings.yaml (scalping).")  # fmt: skip
    for label, slip in (
        ("prix exécutables", 0.0),
        (f"glissement +{cfg.extra_slippage_points:g} points", cfg.extra_slippage_points),
    ):
        schedule = MarketSchedule.from_config(settings.market_hours)
        result = run_backtest(
            ticks, bars, cfg, instrument=instrument, schedule=schedule, initial_equity=start, slippage_points=slip
        )
        m = summary(result, days)
        echo("")
        echo(f"### {label}")
        echo(f"signaux {result.signals}, trades {m['trades']}")
        if m["trades"]:
            echo(
                f"gagnants {m['gagnants_pct']:.0f} %, gain moyen {m['gain_moyen'] * rate:+.2f}, perte moyenne "
                f"{m['perte_moyenne'] * rate:+.2f}, profit factor {m['profit_factor']:.2f}"
            )
            echo(
                f"gains {m['gains'] * rate:+.0f}, pertes {m['pertes'] * rate:+.0f}, net {m['net'] * rate:+.0f} ; "
                f"compte {start * rate:,.0f} -> {result.final_equity * rate:,.0f}, "
                f"pire baisse {m['drawdown_pct']:.1f} %"
            )
            echo(f"sorties : objectif {m['objectif']}, stop {m['stop']}, durée max {m['duree_max']}")
        echo("refus : " + ", ".join(f"{k} {v}" for k, v in result.refusals.most_common()))
    return EXIT_OK
