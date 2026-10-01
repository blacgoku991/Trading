"""Commandes de l'expérience de scalping (scripts/run_scalp.py et scripts/backtest_scalp.py).

Compte DÉMO obligatoire, sans exception possible : aucun passage automatique en réel.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from goldbot import __version__
from goldbot.broker import mt5_constants as C
from goldbot.broker.base import Broker, BrokerError, Position
from goldbot.broker.mt5_broker import MT5Broker
from goldbot.broker.supervisor import ConnectionSupervisor
from goldbot.broker.symbols import SymbolSelectionError, resolve_gold_symbol
from goldbot.config import ConfigError, Secrets, Settings, load_secrets, load_settings
from goldbot.data.history import load_bars, load_ticks
from goldbot.data.market_hours import MarketSchedule
from goldbot.execution.orders import (
    OrderRejected,
    filling_candidates,
    find_entry_deals,
    select_filling,
    send_market_order,
)
from goldbot.live.lock import InstanceLock
from goldbot.monitoring.logging_setup import setup_logging
from goldbot.scalping.backtest import Instrument, run_backtest, summary
from goldbot.scalping.engine import BREAKOUT, PULLBACK, TWO_CANDLES
from goldbot.scalping.live import ScalpRunner, settings_dump, strategy_label, strategy_versions
from goldbot.scalping.store import CLOSED, FAILED, OPEN, SENDING, SENT, VERIFY_PREFIX, ScalpStore, params_hash

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


class PendingOrders(Refused):
    """Ordres encore ouverts ou en attente : l'expérience ne peut pas être archivée telle quelle."""


def _open_store(path: Path, settings: Settings, *, new_experiment: bool, say: Callable[[str], None]) -> ScalpStore:
    """Réglages figés pendant l'expérience : un changement exige --nouvelle-experience (l'ancienne est archivée)."""
    current = params_hash(settings_dump(settings.scalping))  # réglages neutres ajoutés après coup : ignorés
    store = ScalpStore(path)
    if store.archived is not None:
        say(f"Base d'une version précédente du bot archivée : {store.archived.name}")
    saved = store.meta("params_hash")
    if store.has_activity() and (new_experiment or (saved is not None and saved != current)):
        pending = store.orders(OPEN, SENDING)
        if pending:
            # Archiver maintenant perdrait le résultat de ces trades (jamais relu ensuite) : live_main les ferme et
            # les enregistre d'abord dans l'expérience en cours (avec --nouvelle-experience), puis archive.
            store.close()
            raise PendingOrders(
                f"{len(pending)} ordre(s) de l'expérience encore ouvert(s) ou en attente : lance "
                "run_scalp.py --nouvelle-experience, le bot les fermera et enregistrera leur résultat avant d'archiver"
            )
        if not new_experiment:
            store.close()
            raise Refused(
                "les réglages de l'expérience ont changé en cours de collecte. Remets-les comme avant, ou lance "
                "une nouvelle expérience avec --nouvelle-experience (l'ancienne sera archivée)"
            )
        store.close()
        archive = path.with_name(f"{path.stem}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}{path.suffix}")
        path.rename(archive)
        say(f"Ancienne expérience archivée : {archive.name}")
        store = ScalpStore(path)
    if store.meta("params_hash") != current:
        store.set_meta("params_hash", current)
        store.set_meta("started_utc", datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return store


def _wind_down(runner: ScalpRunner, sleep: Callable[[float], None], reason: str = "arrêt total : drawdown maximal",
               passes: int = 60) -> int:  # fmt: skip
    """Fin d'une expérience : fermer ses positions et enregistrer les sorties (et les ordres incertains), sans entrée.

    Renvoie le nombre de positions de l'expérience encore ouvertes (marché fermé, liaison perdue...).
    """
    for _ in range(passes):
        now_ms = runner.server_now_ms()
        runner._manage_positions(now_ms)  # sorties lues dans les deals, ordres incertains retrouvés ou notés échec
        runner._close_everything(runner._mine(), now_ms, reason)  # nouvel essai toutes les 5 s
        if not runner.store.orders(OPEN, SENDING) and not runner._mine():
            return 0
        sleep(POLL_SECONDS)
    left = runner._mine()
    for order in runner.store.orders(OPEN):
        if order.position not in left:
            # Position disparue et aucun deal de sortie dans l'historique : résultat inconnu, noté comme tel.
            runner.store.update_order(order.tag, order.part, status=CLOSED, closed_ms=runner.server_now_ms(),
                                      detail="position fermée, deal de sortie introuvable : résultat inconnu")  # fmt: skip
            runner.say(f"   ordre {order.comment} : position fermée mais deal de sortie introuvable, résultat inconnu")
    return len(left)


def _history_check(runner: ScalpRunner, comment: str, position: Position, sent_ms: int) -> tuple[str, bool, str]:
    """Ordre retrouvable dans l'historique des deals par son magic et son commentaire (réponse perdue du broker)."""
    label = "historique des deals : ordre retrouvable par magic et commentaire (si la réponse du broker se perd)"
    try:
        for _ in range(8):  # l'historique du terminal peut avoir un peu de retard
            found = find_entry_deals(runner.broker, magic=runner.cfg.magic, comments={comment},
                                     sent_s=sent_ms // 1000, now_s=runner.server_now_ms() // 1000)  # fmt: skip
            if comment in found:
                deal = found[comment]
                ok = deal.position_id == position.ticket
                return label, ok, f"deal {deal.ticket}, commentaire « {deal.comment} », position {deal.position_id}"
            runner.sleep(0.25)
        entries = [d for d in runner.broker.deals_for_position(position.ticket) if d.entry == C.DEAL_ENTRY_IN]
        seen = ", ".join(f"magic {d.magic}, commentaire « {d.comment} »" for d in entries) or "aucun deal d'entrée"
        return label, False, f"attendu « {comment} » ; trouvé : {seen}"
    except BrokerError as exc:
        return label, False, f"historique illisible : {exc}"


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
    comment = f"{tag}#1"
    sl = round(tick.bid - VERIFY_STOP, spec.digits)
    tp = round(tick.ask + cfg.target_ratio * VERIFY_STOP, spec.digits)
    request = {
        "action": C.TRADE_ACTION_DEAL,
        "symbol": spec.name,
        "volume": spec.volume_min,
        "type": C.ORDER_TYPE_BUY,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": cfg.magic,
        "comment": comment,
        "type_time": C.ORDER_TIME_GTC,
    }
    say(f"Ouverture d'un achat de test ({spec.volume_min:g} lot, stop à {VERIFY_STOP:.2f} $)...")
    # Enregistré avant l'envoi, comme un vrai signal : un arrêt pendant l'envoi reste rattrapable.
    runner.store.record_signal(tag, strategy="VERIF", version=0, params_hash="-", key=tag, time_ms=now_ms, side=1,
                               status=SENT, reason="vérification technique", volume=spec.volume_min, entry=tick.ask,
                               sl=sl, tp=tp, risk=0.0, parts=1)  # fmt: skip
    runner.store.record_order(tag, 1, comment=comment, side=1, volume=spec.volume_min, sl=sl, tp=tp, risk=0.0,
                              spread=tick.ask - tick.bid, time_ms=now_ms)  # fmt: skip
    try:
        filling, _ = select_filling(broker, {**request, "price": tick.ask}, spec.filling_mode)
        candidates = filling_candidates(spec.filling_mode)
        send_market_order(broker, request, candidates[candidates.index(filling) :], sleep=runner.sleep)
    except (OrderRejected, BrokerError) as exc:
        runner.store.update_order(tag, 1, status=FAILED, detail=str(exc))
        say(f"  ÉCHEC ouverture : {exc} (vérifie l'onglet Trade de MT5)")
        return False
    position = runner._find(comment)
    checks.append(("ouverture d'une position de test (achat, lot minimal)", position is not None, ""))
    if position is None:
        say("  ÉCHEC : position introuvable, vérifie l'onglet Trade de MT5 (et ferme-la à la main si besoin)")
        return False
    checks.append(("stop côté serveur présent dès l'entrée", position.sl > 0, f"stop {position.sl:.2f}"))
    checks.append(_history_check(runner, comment, position, now_ms))
    runner._mark_open(tag, 1, position, hold_s=VERIFY_HOLD_S)
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
    moved = runner._find(comment)
    stop_moved = result.retcode == C.TRADE_RETCODE_DONE and moved is not None and abs(moved.sl - tighter) < spec.point
    checks.append(
        (
            f"stop modifiable côté serveur (resserré de {VERIFY_TIGHTEN:.2f} $)",
            stop_moved,
            f"stop {moved.sl:.2f}" if stop_moved else f"retcode {result.retcode} {result.comment}",
        )
    )
    if stop_moved:
        runner.store.update_order(tag, 1, sl=moved.sl)
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
    resumed = any(order.tag == tag for order in restarted.store.orders(OPEN))
    checks.append(("redémarrage : position reprise depuis l'état enregistré", resumed, ""))
    say(f"  Position {position.ticket} ouverte à {position.price_open:.2f} ; sortie forcée dans {VERIFY_HOLD_S} s...")
    for _ in range(int((VERIFY_HOLD_S + 30) / POLL_SECONDS)):
        restarted._manage_positions(restarted.server_now_ms())
        if restarted.store.order(tag, 1).get("status") == CLOSED:
            break
        runner.sleep(POLL_SECONDS)
    row = restarted.store.order(tag, 1)
    closed = row.get("status") == CLOSED
    checks.append(("clôture à la durée maximale, même en perte", closed, str(row.get("exit_reason") or "")))
    if closed:
        estimate, real = row.get("est_pnl"), row.get("real_pnl")
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
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--env", type=Path, default=root / ".env")
    args = parser.parse_args(argv)
    if args.bilan and args.verification:
        parser.error("--bilan ne se combine pas avec --verification")
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
        log.info(text, extra={"console": False})  # déjà affiché : dans le journal seulement

    now_utc = now_utc or (lambda: datetime.now(UTC))
    lock = InstanceLock(root / "data" / "scalp.lock")
    if not args.bilan and not lock.acquire():
        echo("ERREUR : l'expérience de scalping tourne déjà sur ce PC.")
        return EXIT_LOCKED
    broker = broker_factory(settings, secrets)
    supervisor = ConnectionSupervisor(broker, settings.mt5.reconnect, sleep=sleep)
    store_path = root / "data" / ("scalp_simulation.sqlite" if args.simulation else "scalp.sqlite")
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
        if args.bilan or args.verification:
            store = ScalpStore(store_path)  # lecture des bilans / test technique : pas de contrôle des réglages
        else:
            try:
                store = _open_store(store_path, settings, new_experiment=args.nouvelle_experience, say=say)
            except PendingOrders:
                if not args.nouvelle_experience or args.simulation:
                    raise
                # Fin propre de l'expérience en cours : positions fermées, sorties enregistrées, puis archive.
                say("Avant d'archiver : fermeture des ordres restants de l'expérience et enregistrement du résultat...")
                old = ScalpStore(store_path)
                closer = ScalpRunner(broker, supervisor, old, settings=settings, spec=spec, local_only=False,
                                     now_utc=now_utc, say=say, sleep=sleep)  # fmt: skip
                left = _wind_down(closer, sleep, "fin de l'expérience")
                old.close()
                if left:
                    raise Refused(
                        f"{left} position(s) de l'expérience encore ouverte(s) (marché fermé ?) : leur stop "
                        "reste sur le serveur ; relance --nouvelle-experience à la réouverture"
                    ) from None
                store = _open_store(store_path, settings, new_experiment=True, say=say)
            if store.meta("start_equity") is None:
                store.set_meta("start_equity", str(account.equity))
        if not args.bilan:
            interrupted = store.interrupt_open_sims()  # suivi en mémoire : perdu à l'arrêt précédent
            if interrupted:
                say(f"{interrupted} trade(s) simulé(s) interrompu(s) par l'arrêt précédent (exclus du bilan 2).")
        runner = ScalpRunner(broker, supervisor, store, settings=settings, spec=spec, local_only=args.simulation,
                             now_utc=now_utc, say=say, sleep=sleep)  # fmt: skip
        if args.bilan:
            for line in runner.reports():
                echo(line)
            return EXIT_OK
        if args.verification:
            return EXIT_OK if _verify(runner, store_path, settings, say) else EXIT_FAILED
        if runner.halted:
            left = _wind_down(runner, sleep)
            detail = f" ; {left} position(s) encore ouverte(s), stop serveur en place" if left else ""
            raise Refused(f"arrêt total de l'expérience ({runner.halted}){detail}. Relance manuelle uniquement, "
                          "après réflexion : run_scalp.py --nouvelle-experience (l'expérience actuelle est "
                          "archivée)")  # fmt: skip
        cfg = settings.scalping
        now_ms = runner.server_now_ms()
        for code, (version, digest, params) in strategy_versions(cfg).items():
            store.register_version(code, version, digest, params, now_ms)
        say("Stratégies (version · empreinte des réglages) : " + ", ".join(runner.labels[c] for c in runner.versions))
        day = store.trading_days()
        say(
            f"Compte DÉMO, equity {account.equity:.2f} {account.currency}. "
            + (
                f"Lot fixe {cfg.fixed_volume:g} (trade refusé s'il risque plus de {cfg.risk_per_trade_pct:g} % au stop)"
                if cfg.fixed_volume is not None
                else f"Lot {' ou '.join(f'{v:g}' for v in sorted(cfg.lot_choices, reverse=True))} (le plus gros qui "
                f"risque au plus {cfg.risk_per_trade_pct:g} % au stop)"
                if cfg.lot_choices
                else f"Risque {cfg.risk_per_trade_pct:g} % par trade"
            )
            + f", -{cfg.daily_loss_pct:g} % par jour au plus, arrêt total à -{cfg.max_drawdown_pct:g} % depuis le plus "
            f"haut. De base : {cfg.max_open_positions} positions, {cfg.max_total_risk_pct:g} % de risque cumulé, "
            f"{cfg.max_entries_per_minute} entrées par minute au plus."
        )
        if cfg.cadence.enabled:
            top = runner.policy.cadence.top_limits()
            say(
                f"Cadence liée au bénéfice : si les {cfg.cadence.window_trades} derniers trades gagnent au total, "
                f"jusqu'à {top.open_positions} positions, {top.total_risk_pct:g} % de risque cumulé et "
                f"{top.entries_per_minute} entrées par minute ; retour à la base dès qu'ils perdent. "
                f"Maintenant : {runner.policy.cadence.describe()}."
            )
        say(
            f"Jour de collecte {day + 1} sur {cfg.experiment_days} annoncés. Ne pas modifier les réglages avant la fin."
        )
        if args.simulation:
            say("Mode --simulation : aucun ordre n'est envoyé, tout est simulé.")
        say("Ctrl+C pour arrêter (les positions de l'expérience sont alors fermées). Bilans toutes les 15 minutes.")
        steps = 0
        last_report = pd.Timestamp(now_utc())
        last_status = last_report - STATUS_EVERY  # première ligne d'état dès le premier passage (marché ouvert ?)
        try:
            while max_steps is None or steps < max_steps:
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
    except (Refused, SymbolSelectionError) as exc:
        echo(f"REFUS : {exc}")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        echo("Arrêt demandé : les positions ouvertes gardent leur stop sur le serveur ; relance pour leur sortie.")
        return EXIT_OK
    finally:
        broker.shutdown()
        lock.release()


def _echo_replay(echo: Callable[[str], None], title: str, result, days: float, rate: float, code: str | None) -> None:
    m = summary(result, days, code)
    signals = result.signals[code] if code else sum(result.signals.values())
    echo("")
    echo(f"### {title}")
    if not m["trades"]:
        echo(f"signaux {signals}, aucun trade")
    else:
        echo(
            f"signaux {signals} | trades {m['trades']} ({m['par_jour']:.1f} par jour) | gagnants "
            f"{m['gagnants_pct']:.0f} % | gain moyen {m['gain_moyen'] * rate:+.2f} | perte moyenne "
            f"{m['perte_moyenne'] * rate:+.2f} | profit factor {m['profit_factor']:.2f}"
        )
        echo(
            f"en pips : total {m['pips_total']:+.0f} | gain moyen {m['pips_gain_moyen']:+.1f} | perte moyenne "
            f"{m['pips_perte_moyenne']:+.1f} (1 pip = 0,10 $, spread et glissement compris)"
        )
        echo(
            f"gains {m['gains'] * rate:+.0f} | pertes {m['pertes'] * rate:+.0f} | net {m['net'] * rate:+.0f} (dont "
            f"frais {0.0 - m['frais'] * rate:+.0f}) | espérance {m['esperance_r']:+.3f} R | t {m['t_stat']:+.1f} | "
            f"pire baisse {m['drawdown_pct']:.1f} % | jours positifs {m['jours_positifs']} sur {m['jours']}"
        )
        echo(
            f"sorties : objectif {m['objectif']}, stop {m['stop']}, durée max {m['duree_max']} | durée moyenne "
            f"{m['duree_moyenne_s']:.0f} s | trades fractionnés {m['fractionnes']}"
        )
    refusals = Counter()
    for (strategy, reason), count in result.refusals.items():
        if code in (None, strategy):
            refusals[reason] += count
    echo("refus : " + (", ".join(f"{reason} {n}" for reason, n in refusals.most_common()) or "aucun"))


def backtest_main(
    argv: list[str] | None = None, *, root: Path | None = None, echo: Callable[[str], None] = print
) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="backtest_scalp.py", description="Expérience de scalping sur les ticks.")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--dir", type=Path, help="dossier des données (défaut : export.directory)")
    parser.add_argument(
        "--strategie",
        choices=["B", "P", "R", "ensemble", "actives", "comparer"],
        default="comparer",
        help="B = cassure, P = impulsion-repli, R = deux bougies, ensemble = cassure et impulsion-repli à la fois, "
        "actives = les stratégies actives de la config, comparer = cassure, impulsion-repli et les deux",
    )
    parser.add_argument("--sans-apprentissage", action="store_true", help="règles v1, sans apprentissage")
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
    # 30 jours de barres avant les ticks : ATR journalier (filtre de sens) connu dès le premier jour.
    bars = bars[bars["time_server"] >= ticks["time_msc_server"].iloc[0] // 1000 - 30 * 86_400].reset_index(drop=True)
    days = (ticks["time_msc_server"].iloc[-1] - ticks["time_msc_server"].iloc[0]) / 86_400_000 * 5 / 7
    cfg = settings.scalping
    if args.sans_apprentissage:
        cfg = cfg.model_copy(update={"learning": cfg.learning.model_copy(update={"enabled": False})})
    start = 5000.0 / rate
    versions = strategy_versions(cfg.model_copy(update={
        "breakout": cfg.breakout.model_copy(update={"enabled": True}),
        "pullback": cfg.pullback.model_copy(update={"enabled": True}),
        "two_candles": cfg.two_candles.model_copy(update={"enabled": True}),
    }))  # fmt: skip
    labels = {code: strategy_label(code, cfg, v, h) for code, (v, h, _) in versions.items()}
    active = tuple(code for code in (BREAKOUT, PULLBACK, TWO_CANDLES)
                   if getattr(cfg, {BREAKOUT: "breakout", PULLBACK: "pullback", TWO_CANDLES: "two_candles"}[code]).enabled)
    runs = {"B": [(BREAKOUT,)], "P": [(PULLBACK,)], "R": [(TWO_CANDLES,)], "ensemble": [(BREAKOUT, PULLBACK)],
            "actives": [active]}.get(args.strategie, [(BREAKOUT,), (PULLBACK,), (BREAKOUT, PULLBACK)])  # fmt: skip
    echo(f"Ticks du {pd.Timestamp(ticks['time'].iloc[0]):%Y-%m-%d} au {pd.Timestamp(ticks['time'].iloc[-1]):%Y-%m-%d}, "
         f"compte de départ 5 000 (devise du compte), {cfg.risk_per_trade_pct:g} % de risque par trade, mêmes "
         f"règles d'entrée, de sortie et de compte pour chaque stratégie (config/settings.yaml, scalping).")  # fmt: skip
    schedule = MarketSchedule.from_config(settings.market_hours)
    for slip in (0.0, cfg.extra_slippage_points):
        cost = "prix exécutables" if slip == 0 else f"glissement +{slip:g} points"
        for strategies in runs:
            result = run_backtest(ticks, bars, cfg, instrument=instrument, schedule=schedule, initial_equity=start,
                                  slippage_points=slip, strategies=strategies,
                                  commission_per_lot_side=settings.backtest.commission_per_lot_side)  # fmt: skip
            if len(strategies) == 1:
                _echo_replay(echo, f"{labels[strategies[0]]} seule, {cost}", result, days, rate, strategies[0])
            else:
                _echo_replay(echo, f"les deux ensemble, {cost}", result, days, rate, None)
                for code in strategies:
                    _echo_replay(echo, f"  dont {labels[code]}", result, days, rate, code)
    return EXIT_OK
