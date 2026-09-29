"""Analyse des erreurs et apprentissage « en avançant » (walk-forward) de l'expérience de scalping.

Chaque signal jouable est simulé seul, sans les limites du compte, avec son contexte à l'entrée et son résultat
en R (mouvement divisé par la distance du stop, spread et glissement compris). Deux usages :
- comprendre les erreurs : résultat moyen par tranche de contexte (heure, stop face à l'ATR, coût, …). Lecture
  descriptive sur toute la période : elle suggère des pistes, elle ne prouve rien ;
- apprendre sans tricher : pour chaque jour, un filtre est appris sur les jours précédents seulement, puis
  appliqué à ce jour. Seul le résultat de ces jours « à venir » mesure si l'apprentissage aide vraiment.

Le filtre est volontairement simple et lisible : pour chaque contexte pré-enregistré (liste FEATURES, fixée
avant de regarder les résultats), le résultat moyen de chaque tranche est rapproché de la moyenne générale
(d'autant plus que la tranche compte peu de trades), puis les écarts s'additionnent. Un signal n'est pris que
si le résultat attendu ainsi estimé est positif.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from goldbot.config import ConfigError, ScalpingConfig, load_settings
from goldbot.data.history import load_bars, load_ticks
from goldbot.data.market_hours import MarketSchedule
from goldbot.data.quality import server_index
from goldbot.data.timezones import ServerTimeRule
from goldbot.scalping.backtest import Instrument, context_by_minute
from goldbot.scalping.engine import STRATEGY_NAMES, CandleBuilder, SimTrade, make_detectors, plan_trade
from goldbot.scalping.policy import reason_key

_EPOCH = datetime(1970, 1, 1)
INF = float("inf")

# Contextes appris par le filtre et bornes de leurs tranches, fixés avant de regarder les résultats.
FEATURES: dict[str, list[float]] = {
    "heure": [0, 8, 10, 14, 16, 18, 22, 24],  # heure de Paris : nuit / Asie, ouverture de Londres, Londres, …
    "sens": [-INF, 0, INF],  # vente, achat
    "stop_atr": [0, 0.3, 0.5, 0.75, 1.0, 1.5, INF],  # distance du stop en ATR M1
    "cout_r": [0, 0.1, 0.15, 0.2, 0.3, INF],  # coût estimé en part du risque
    "atr": [0, 1, 1.5, 2, 3, INF],  # volatilité (ATR M1 en $)
    "entree_au_dela_atr": [-INF, 0, 0.25, 0.5, 1, INF],  # prix d'entrée au-delà du niveau clé, en ATR
    "meme_sens_ouverts": [0, 1, 2, 3, INF],  # trades du même sens déjà ouverts
}
LABELS = {
    "heure": "heure de Paris",
    "sens": "sens (-1 vente, +1 achat)",
    "stop_atr": "stop / ATR M1",
    "cout_r": "coût / risque",
    "atr": "ATR M1 ($)",
    "entree_au_dela_atr": "entrée au-delà du niveau (ATR)",
    "meme_sens_ouverts": "trades du même sens déjà ouverts",
}


def signal_table(
    ticks: pd.DataFrame,
    bars: pd.DataFrame,
    config: ScalpingConfig,
    *,
    instrument: Instrument,
    schedule: MarketSchedule,
    rule: ServerTimeRule,
    strategies: tuple[str, ...] | None = None,
    slippage_points: float = 0.0,
    commission_per_lot_side: float = 0.0,
    display_timezone: str = "Europe/Paris",
) -> tuple[pd.DataFrame, Counter]:
    """Tous les signaux dont le plan est accepté, chacun simulé seul ; renvoie aussi les refus du plan."""
    server = server_index(ticks["time_msc_server"].to_numpy() // 1000)
    open_mask = schedule.open_mask(server)
    times = ticks["time_msc_server"].to_numpy(dtype="int64")[open_mask]
    bids = ticks["bid"].to_numpy(dtype="float64")[open_mask]
    asks = ticks["ask"].to_numpy(dtype="float64")[open_mask]
    bar_times, trends, atrs = context_by_minute(bars, config)
    point, size_ms = instrument.point, config.candle_seconds * 1000
    slip = slippage_points * point
    fee = 2 * commission_per_lot_side / instrument.contract_size
    builder, detectors = CandleBuilder(config.candle_seconds), make_detectors(config, strategies)
    open_trades: list[tuple[SimTrade, dict[str, object], float]] = []  # (trade, ligne, distance du stop)
    rows: list[dict[str, object]] = []
    refusals: Counter = Counter()

    for i in range(len(times)):
        now, bid, ask = int(times[i]), float(bids[i]), float(asks[i])
        if open_trades:
            still = []
            for trade, row, distance in open_trades:
                if trade.on_tick(now, bid, ask):
                    row.update(
                        r=trade.move / distance,
                        mfe_r=trade.mfe / distance,
                        mae_r=trade.mae / distance,
                        sortie=trade.reason,
                        duree_s=(trade.exit_ms - trade.open_ms) / 1000,
                    )
                    rows.append(row)
                else:
                    still.append((trade, row, distance))
            open_trades = still
        for candle in builder.add(now, bid, ask):
            bar = int(np.searchsorted(bar_times, (candle.start_ms + size_ms) // 1000 - 60, side="right")) - 1
            trend = int(trends[bar]) if bar >= 0 else 0
            atr = float(atrs[bar]) if bar >= 0 else 0.0
            for detector in detectors:
                setup = detector.on_candle(candle, trend, atr)
                if setup is None:
                    continue
                if now > candle.start_ms + 2 * size_ms:
                    refusals[(setup.strategy, "signal périmé")] += 1
                    continue
                if not schedule.is_open(_EPOCH + timedelta(milliseconds=now + config.max_hold_s * 1000)):
                    refusals[(setup.strategy, "le marché ferme avant la durée max")] += 1
                    continue
                plan = plan_trade(
                    setup,
                    bid,
                    ask,
                    config,
                    point=point,
                    stops_level_points=instrument.stops_level_points,
                    freeze_level_points=instrument.freeze_level_points,
                    commission_per_oz=fee,
                )
                if isinstance(plan, str):
                    refusals[(setup.strategy, reason_key(plan))] += 1
                    continue
                row = {
                    "strategie": setup.strategy,
                    "tag": setup.tag,
                    "time_ms": now,
                    "sens": setup.side,
                    "atr": atr,
                    "stop_atr": plan.stop_distance / atr if atr > 0 else np.nan,
                    "cout_r": plan.cost / plan.stop_distance,
                    "spread_points": plan.spread / point,
                    "entree_au_dela_atr": (plan.entry - setup.level) * setup.side / atr if atr > 0 else np.nan,
                    "meme_sens_ouverts": sum(1 for trade, _, _ in open_trades if trade.side == setup.side),
                    **setup.features,
                }
                trade = SimTrade.open(setup.tag, plan, bid, ask, now, config.max_hold_s, slip, 1.0, fee)
                open_trades.append((trade, row, plan.stop_distance))

    table = pd.DataFrame(rows)
    if table.empty:
        return table, refusals
    table = table.sort_values("time_ms").reset_index(drop=True)
    local = rule.server_ms_to_utc(table["time_ms"].to_numpy()).tz_convert(display_timezone)
    table["heure"] = local.hour + local.minute / 60
    table["jour"] = table["time_ms"] // 86_400_000
    return table, refusals


def _bucket(values: pd.Series, edges: list[float]) -> pd.Series:
    return pd.cut(values, edges, labels=False, right=False, include_lowest=True)


def _stats(r: pd.Series) -> dict[str, float]:
    n = len(r)
    wins, losses = r[r > 0].sum(), -r[r <= 0].sum()
    error = r.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    return {
        "trades": n,
        "gagnants_pct": (r > 0).mean() * 100 if n else np.nan,
        "esperance_r": r.mean() if n else np.nan,
        "t": r.mean() / error if n > 1 and error > 0 else np.nan,
        "profit_factor": wins / losses if losses > 0 else np.nan,
    }


def bucket_report(table: pd.DataFrame, column: str, edges: list[float]) -> pd.DataFrame:
    """Résultat par tranche d'un contexte (descriptif, toute la période)."""
    buckets = _bucket(table[column], edges)
    lines = []
    for index in range(len(edges) - 1):
        chosen = table.loc[buckets == index, "r"]
        if chosen.empty:
            continue
        lines.append({"tranche": f"[{edges[index]:g} ; {edges[index + 1]:g}[", **_stats(chosen)})
    return pd.DataFrame(lines)


def learn(train: pd.DataFrame, features: dict[str, list[float]], prior: float) -> tuple[float, dict]:
    """Moyenne générale et écart (rétréci) de chaque tranche, appris sur les trades d'entraînement."""
    base = float(train["r"].mean())
    effects = {}
    for column, edges in features.items():
        stats = train.groupby(_bucket(train[column], edges))["r"].agg(["sum", "count"])
        effects[column] = ((stats["sum"] + prior * base) / (stats["count"] + prior) - base).to_dict()
    return base, effects


def predict(rows: pd.DataFrame, base: float, effects: dict, features: dict[str, list[float]]) -> pd.Series:
    """Résultat attendu (R) de chaque signal : moyenne générale + écarts de ses tranches."""
    total = pd.Series(base, index=rows.index)
    for column, edges in features.items():
        total = total + _bucket(rows[column], edges).map(effects[column]).fillna(0.0)
    return total


def walk_forward(
    table: pd.DataFrame,
    features: dict[str, list[float]] = FEATURES,
    *,
    min_train_days: int = 5,
    prior: float = 50.0,
    threshold: float = 0.0,
) -> pd.DataFrame:
    """Pour chaque jour après les min_train_days premiers : filtre appris sur les jours précédents seulement."""
    days = sorted(table["jour"].unique())
    tested = []
    for day in days[min_train_days:]:
        train, test = table[table["jour"] < day], table[table["jour"] == day]
        base, effects = learn(train, features, prior)
        expected = predict(test, base, effects, features)
        tested.append(test.assign(attendu_r=expected, pris=expected > threshold))
    return pd.concat(tested) if tested else table.iloc[0:0].assign(attendu_r=np.nan, pris=False)


def _days(rows: pd.DataFrame) -> str:
    by_day = rows.groupby("jour")["r"].sum()
    return f"{int((by_day > 0).sum())} sur {len(by_day)}"


def _line(title: str, r: pd.Series) -> str:
    s = _stats(r)
    if not s["trades"]:
        return f"{title} : aucun trade"
    return (
        f"{title} : {s['trades']} trades, gagnants {s['gagnants_pct']:.0f} %, espérance {s['esperance_r']:+.3f} R, "
        f"t {s['t']:+.1f}, profit factor {s['profit_factor']:.2f}"
    )


def report(table: pd.DataFrame, code: str, echo: Callable[[str], None]) -> None:
    rows = table[table["strategie"] == code]
    name = STRATEGY_NAMES[code]
    echo("")
    echo(f"##### {name} : {len(rows)} signaux jouables, chacun simulé seul")
    echo(_line("Tous les signaux", rows["r"]))
    exits = ", ".join(f"{reason} {count}" for reason, count in rows["sortie"].value_counts().items())
    echo(f"Sorties : {exits} | durée moyenne {rows['duree_s'].mean():.0f} s")
    losers, winners = rows[rows["r"] < 0], rows[rows["r"] > 0]
    if len(losers) and len(winners):
        echo(
            f"Excursions : {(losers['mfe_r'] >= 0.5).mean() * 100:.0f} % des perdants ont d'abord gagné 0,5 R ou plus "
            f"({(losers['mfe_r'] >= 1).mean() * 100:.0f} % : 1 R ou plus) ; {(winners['mae_r'] <= -0.5).mean() * 100:.0f}"
            f" % des gagnants ont d'abord perdu 0,5 R ou plus"
        )
    echo("Où ça perd (toute la période, descriptif) :")
    for column, edges in FEATURES.items():
        buckets = bucket_report(rows, column, edges)
        parts = [f"{b.tranche} {b.esperance_r:+.2f} R ({b.trades})" for b in buckets.itertuples() if b.trades >= 30]
        echo(f"  {LABELS[column]} : " + " | ".join(parts))
    tested = walk_forward(rows)
    if tested.empty:
        echo("Apprentissage : pas assez de jours")
        return
    echo(
        f"Apprentissage en avançant (appris sur les jours passés, testé sur le jour suivant, {tested['jour'].nunique()} jours) :"
    )
    echo("  " + _line("sans filtre", tested["r"]) + f", jours positifs {_days(tested)}")
    taken = tested[tested["pris"]]
    echo("  " + _line("avec filtre appris", taken["r"]) + (f", jours positifs {_days(taken)}" if len(taken) else ""))


def main(argv: list[str] | None = None, *, root: Path | None = None, echo: Callable[[str], None] = print) -> int:
    root = root or Path.cwd()
    parser = argparse.ArgumentParser(prog="analyse_scalp.py", description="Analyse des erreurs et apprentissage.")
    parser.add_argument("--config", type=Path, default=root / "config" / "settings.yaml")
    parser.add_argument("--dir", type=Path, help="dossier des données (défaut : export.directory)")
    parser.add_argument("--glissement", type=float, default=0.0, help="glissement supplémentaire, en points")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        echo(f"ERREUR de configuration : {exc}")
        return 4
    folder = args.dir or settings.export.directory
    folder = folder if folder.is_absolute() else root / folder
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
    ticks = load_ticks(folder, symbol["name"])
    bars = load_bars(folder, symbol["name"])
    bars = bars[bars["time_server"] >= ticks["time_msc_server"].iloc[0] // 1000 - 7 * 86_400].reset_index(drop=True)
    cfg = settings.scalping
    cfg = cfg.model_copy(update={
        "breakout": cfg.breakout.model_copy(update={"enabled": True}),
        "pullback": cfg.pullback.model_copy(update={"enabled": True}),
    })  # fmt: skip
    table, refusals = signal_table(
        ticks,
        bars,
        cfg,
        instrument=instrument,
        schedule=MarketSchedule.from_config(settings.market_hours),
        rule=ServerTimeRule.from_config(settings.server_time),
        slippage_points=args.glissement,
        commission_per_lot_side=settings.backtest.commission_per_lot_side,
        display_timezone=settings.bot.display_timezone,
    )
    echo(
        f"Ticks du {pd.Timestamp(ticks['time'].iloc[0]):%Y-%m-%d} au {pd.Timestamp(ticks['time'].iloc[-1]):%Y-%m-%d}, "
        f"glissement supplémentaire {args.glissement:g} points. Résultats en R (1 R = perte au stop)."
    )
    for code in sorted(table["strategie"].unique()) if not table.empty else []:
        report(table, code, echo)
    reports = root / settings.paths.reports
    reports.mkdir(parents=True, exist_ok=True)
    if not table.empty:
        table.to_csv(reports / "scalp_signaux.csv", index=False)
        echo("")
        echo(f"Détail de chaque signal : {reports / 'scalp_signaux.csv'}")
    return 0
