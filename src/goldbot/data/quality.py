"""Contrôle qualité de l'historique exporté, avec rapport en français.

Barres : doublons, heures invalides, barres hors cotation, trous, OHLC incohérents,
mouvements extrêmes, spread par heure, et validation historique de la règle d'heure
serveur (la pause quotidienne de l'or doit tomber à la même heure de New York toute
l'année). Ticks : spread, cotations croisées, et quel spread MT5 range dans les barres.
Tout est vectorisé : quelques secondes pour dix ans de barres M1.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from goldbot.data.market_hours import MarketSchedule, seconds_of_day
from goldbot.data.timezones import ServerTimeRule

# Mouvement d'une minute jugé extrême : plus de SPIKE_Z écarts-types robustes (sur la journée écoulée).
SPIKE_Z = 20.0
# Spread jugé aberrant : plus de SPREAD_OUTLIER_FACTOR fois la médiane de son heure.
SPREAD_OUTLIER_FACTOR = 10.0
GAP_BUCKETS = ((1, 1), (2, 4), (5, 29), (30, 119), (120, None))
_DAYS_FR = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")


def server_index(seconds: Iterable[int]) -> pd.DatetimeIndex:
    """Epochs serveur (s) -> heures serveur naïves."""
    return pd.DatetimeIndex(pd.to_datetime(np.asarray(seconds, dtype="int64"), unit="s"))


@dataclass(frozen=True)
class Gap:
    """Minutes de cotation consécutives sans barre (heure serveur)."""

    first_missing: pd.Timestamp
    last_missing: pd.Timestamp
    minutes: int


def find_gaps(server_times: pd.DatetimeIndex, schedule: MarketSchedule) -> tuple[int, list[Gap]]:
    """Minutes de cotation attendues entre la première et la dernière barre, et trous.

    Les minutes hors cotation (pause, week-end) ne comptent pas : un trou qui couvre une
    pause quotidienne reste un seul trou (jour férié, panne du flux…).
    """
    if len(server_times) == 0:
        return 0, []
    grid = pd.date_range(server_times.min().floor("min"), server_times.max(), freq="min")
    grid = grid[schedule.open_mask(grid)]
    missing = np.flatnonzero(~grid.isin(server_times.floor("min")))
    if len(missing) == 0:
        return len(grid), []
    cuts = np.flatnonzero(np.diff(missing) > 1)
    starts = np.concatenate(([missing[0]], missing[cuts + 1]))
    ends = np.concatenate((missing[cuts], [missing[-1]]))
    return len(grid), [Gap(grid[s], grid[e], int(e - s + 1)) for s, e in zip(starts, ends, strict=True)]


def off_hours(server_times: pd.DatetimeIndex, schedule: MarketSchedule) -> dict[str, int]:
    """Barres (ou ticks) horodatées pendant une fermeture, par motif."""
    closed = ~schedule.open_mask(server_times)
    weekday = np.asarray(server_times.weekday)
    clock = np.asarray((server_times - server_times.normalize()) / pd.Timedelta(seconds=1))
    weekend = closed & (weekday >= 5)
    in_break = (
        closed
        & ~weekend
        & ((clock >= seconds_of_day(schedule.break_start)) | (clock < seconds_of_day(schedule.break_end)))
    )
    monday = closed & ~weekend & ~in_break & (weekday == 0)
    friday = closed & ~weekend & ~in_break & (weekday == 4)
    other = closed & ~(weekend | in_break | monday | friday)
    return {
        "week-end (samedi, dimanche)": int(weekend.sum()),
        "pause quotidienne": int(in_break.sum()),
        "lundi avant l'ouverture": int(monday.sum()),
        "vendredi après la fermeture": int(friday.sum()),
        "jour fermé (config closed_dates)": int(other.sum()),
    }


def ohlc_errors(bars: pd.DataFrame) -> int:
    """Barres dont high/low ne contiennent pas open/close, ou prix nuls ou négatifs."""
    body_high = bars[["open", "close"]].max(axis=1)
    body_low = bars[["open", "close"]].min(axis=1)
    bad = (
        (bars["high"] < body_high)
        | (bars["low"] > body_low)
        | (bars[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    return int(bad.sum())


def spikes(bars: pd.DataFrame, point: float, top: int = 15) -> pd.DataFrame:
    """Plus gros mouvements d'une minute, en écarts-types robustes de la journée écoulée.

    Un vrai pic de cotation revient aussitôt (reverted) ; une annonce (NFP, Fed) non.
    """
    close = bars["close"].to_numpy(dtype="float64")
    move = np.diff(close, prepend=np.nan)
    # Écart-type robuste : 1,4826 x médiane des |variations| des 1440 minutes précédentes.
    scale = pd.Series(np.abs(move)).rolling(1440, min_periods=120).median().shift(1).to_numpy() * 1.4826
    z = np.abs(move) / np.maximum(scale, point)
    following = np.concatenate((move[1:], [np.nan]))
    reverted = np.sign(following) == -np.sign(move)
    reverted &= np.abs(following) >= 0.5 * np.abs(move)
    # Le retour d'un pic n'est pas un second pic.
    return_of_spike = np.concatenate(([False], reverted[:-1] & (z[:-1] > SPIKE_Z)))
    frame = pd.DataFrame(
        {
            "time": bars["time"].to_numpy(),
            "move": move,
            "z": z,
            "reverted": reverted,
            "spread": bars["spread"].to_numpy(),
        }
    )
    frame = frame[(frame["z"] > SPIKE_Z) & ~return_of_spike].sort_values("z", ascending=False)
    return frame.head(top).reset_index(drop=True)


def spread_by_hour(times_utc: pd.Series, spread_points: pd.Series, zone: str) -> pd.DataFrame:
    """Médiane, 95e centile et maximum du spread (points) par heure locale."""
    hour = times_utc.dt.tz_convert(zone).dt.hour
    grouped = spread_points.groupby(hour.to_numpy())
    table = pd.DataFrame(
        {"médiane": grouped.median(), "p95": grouped.quantile(0.95), "max": grouped.max(), "n": grouped.size()}
    )
    table.index.name = "heure"
    return table


def spread_outliers(times_utc: pd.Series, spread_points: pd.Series, zone: str) -> int:
    """Spreads nuls ou supérieurs à SPREAD_OUTLIER_FACTOR fois la médiane de leur heure."""
    hour = times_utc.dt.tz_convert(zone).dt.hour.to_numpy()
    median = spread_points.groupby(hour).transform("median").to_numpy()
    values = spread_points.to_numpy()
    return int(((values <= 0) | (values > SPREAD_OUTLIER_FACTOR * median)).sum())


@dataclass(frozen=True)
class RuleCheck:
    """Validation de la règle d'heure serveur par l'heure de clôture quotidienne."""

    table: pd.DataFrame  # une ligne par (année, période) : heure la plus fréquente, part, jours
    usual_time: str | None  # heure la plus fréquente sur tout l'historique
    odd_weeks: list[str]  # semaines dont la clôture habituelle diffère (ex. « 2025-W11 (15:58) »)

    @property
    def consistent(self) -> bool:
        return self.usual_time is not None and self.table["heure"].nunique() == 1 and not self.odd_weeks

    def explanation(self) -> str:
        if self.usual_time is None:
            return "aucune barre exploitable"
        if self.consistent:
            return f"dernière barre du jour à {self.usual_time} (heure de référence), été comme hiver, chaque année"
        details = ", ".join(f"{r.année} {r.période} : {r.heure} ({r.part:.0%})" for r in self.table.itertuples())
        odd = f" ; semaines différentes : {', '.join(self.odd_weeks)}" if self.odd_weeks else ""
        return f"heure de clôture quotidienne variable : {details}{odd}"


def daily_close_check(bars: pd.DataFrame, rule: ServerTimeRule) -> RuleCheck:
    """Heure locale (fuseau de référence de la règle) de la dernière barre de chaque jour serveur.

    La pause quotidienne de l'or tombe à heure fixe à New York. Si la règle est juste, cette
    heure est la même en heure d'été et en heure d'hiver, chaque année ; sinon elle saute
    d'une heure pendant une des deux périodes, ou seulement pendant les semaines où les
    changements d'heure américain et européen sont décalés. Seuls les jours du lundi au jeudi
    comptent (le vendredi, la semaine ferme un peu plus tôt), et une semaine n'est jugée
    qu'avec au moins 3 jours (un jour de fermeture anticipée ne suffit pas à l'écarter).
    """
    empty = RuleCheck(pd.DataFrame(columns=["année", "période", "heure", "part", "jours"]), None, [])
    valid = bars.dropna(subset=["time"])
    server_day = valid["time_server"].to_numpy() // 86_400
    # Lundi à jeudi, sans le dernier jour des données (peut-être incomplet).
    keep = (server_index(valid["time_server"]).weekday <= 3) & (server_day < server_day.max(initial=0))
    valid, server_day = valid[keep], server_day[keep]
    if valid.empty:
        return empty
    last = valid.groupby(server_day)["time_server"].idxmax()
    local = valid.loc[last.to_numpy(), "time"].dt.tz_convert(rule.reference_tz.key)
    summer = np.array([stamp.dst().total_seconds() != 0 for stamp in local])
    iso = local.dt.isocalendar()
    days = pd.DataFrame(
        {
            "année": local.dt.year.to_numpy(),
            "période": np.where(summer, "heure d'été", "heure d'hiver"),
            "semaine": [f"{y}-W{w:02d}" for y, w in zip(iso["year"], iso["week"], strict=True)],
            "heure": local.dt.strftime("%H:%M").to_numpy(),
        }
    )
    usual = days["heure"].value_counts().index[0]
    rows = []
    for (year, period), group in days.groupby(["année", "période"], sort=True):
        counts = group["heure"].value_counts()
        share = float(counts.iloc[0] / len(group))
        rows.append(
            {"année": int(year), "période": period, "heure": counts.index[0], "part": share, "jours": len(group)}
        )
    odd_weeks = []
    for week, group in days.groupby("semaine", sort=True):
        weekly = group["heure"].value_counts()
        if len(group) >= 3 and weekly.index[0] != usual and weekly.iloc[0] > len(group) / 2:
            odd_weeks.append(f"{week} ({weekly.index[0]})")
    return RuleCheck(pd.DataFrame(rows), usual, odd_weeks)


def tick_checks(ticks: pd.DataFrame, point: float) -> dict[str, float]:
    spread = (ticks["ask"] - ticks["bid"]) / point
    order = ticks["time_msc_server"].to_numpy()
    return {
        "ticks": len(ticks),
        "heures invalides (NaT)": int(ticks["time"].isna().sum()),
        "cotations croisées (ask < bid)": int((spread < 0).sum()),
        "spread nul": int((spread == 0).sum()),
        "horodatages en recul": int((np.diff(order) < 0).sum()),
        "doublons exacts": int(ticks.duplicated().sum()),
    }


def bar_spread_vs_ticks(bars: pd.DataFrame, ticks: pd.DataFrame, point: float) -> pd.DataFrame:
    """Quelle statistique des ticks de la minute MT5 enregistre-t-il dans la colonne spread des barres ?

    Pour chaque statistique (min, moyenne, médiane, max, premier, dernier) : part des minutes
    où elle vaut exactement le spread de la barre, et écart moyen en points.
    """
    minute = ticks["time_msc_server"].to_numpy() // 60_000 * 60
    spread = pd.Series(np.round((ticks["ask"] - ticks["bid"]).to_numpy() / point), index=minute)
    grouped = spread.groupby(level=0)
    stats = pd.DataFrame(
        {
            "min": grouped.min(),
            "moyenne": grouped.mean(),
            "médiane": grouped.median(),
            "max": grouped.max(),
            "premier": grouped.first(),
            "dernier": grouped.last(),
        }
    )
    bar_spread = bars.drop_duplicates("time_server").set_index("time_server")["spread"]
    joined = stats.join(bar_spread.rename("barre"), how="inner")
    rows = [
        {
            "statistique": name,
            "égalité exacte": float((joined[name].round() == joined["barre"]).mean()),
            "écart moyen (points)": float((joined[name] - joined["barre"]).abs().mean()),
            "minutes": len(joined),
        }
        for name in stats.columns
    ]
    return pd.DataFrame(rows).sort_values("égalité exacte", ascending=False).reset_index(drop=True)


# --- Rapport -------------------------------------------------------------------------


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _table(frame: pd.DataFrame, floats: str = "{:.1f}") -> list[str]:
    header = "| " + " | ".join(str(c) for c in [frame.index.name or "", *frame.columns]) + " |"
    lines = [header, "|" + "---|" * (len(frame.columns) + 1)]
    for index, *values in frame.itertuples(name=None):  # garde le type de chaque colonne
        cells = [floats.format(v) if isinstance(v, float) else str(v) for v in values]
        lines.append(f"| {index} | " + " | ".join(cells) + " |")
    return lines


def render_report(
    bars: pd.DataFrame,
    ticks: pd.DataFrame | None,
    *,
    rule: ServerTimeRule,
    schedule: MarketSchedule,
    point: float,
    zone: str,
    file_problems: list[str],
) -> tuple[str, list[str]]:
    """Rapport markdown complet et liste des alertes (lignes courtes pour la console)."""
    alerts: list[str] = list(file_problems)
    lines = ["# Contrôle qualité des données", ""]
    lines += [f"Heures affichées en {zone} sauf mention contraire. Spreads en points (1 point = {point:g}).", ""]

    lines += ["## Fichiers", ""]
    lines += [f"- PROBLÈME : {p}" for p in file_problems] or ["- Tous les fichiers correspondent au manifeste."]

    lines += ["", "## Barres M1 : vue d'ensemble", ""]
    times = bars["time"].dropna()
    local = times.dt.tz_convert(zone)
    lines.append(f"- {_fmt_int(len(bars))} barres, du {local.min():%Y-%m-%d %H:%M} au {local.max():%Y-%m-%d %H:%M}.")
    per_year = local.dt.year.value_counts().sort_index()
    lines.append("- Barres par année : " + ", ".join(f"{y} : {_fmt_int(n)}" for y, n in per_year.items()) + ".")

    duplicated = int(bars["time_server"].duplicated().sum())
    invalid = int(bars["time"].isna().sum())
    unsorted = int((np.diff(bars["time_server"].to_numpy()) < 0).sum())
    errors = ohlc_errors(bars)
    lines += ["", "## Barres M1 : intégrité", ""]
    lines.append(f"- Doublons d'horodatage : {_fmt_int(duplicated)}.")
    lines.append(f"- Heures serveur impossibles à convertir (changement d'heure) : {_fmt_int(invalid)}.")
    lines.append(f"- Horodatages en recul : {_fmt_int(unsorted)}.")
    lines.append(f"- OHLC incohérents ou prix nuls : {_fmt_int(errors)}.")
    for label, value in (("doublons", duplicated), ("heures invalides", invalid), ("OHLC incohérents", errors)):
        if value:
            alerts.append(f"barres : {value} {label}")

    server = server_index(bars["time_server"])
    outside = off_hours(server, schedule)
    lines += ["", "## Barres hors heures de cotation (heure serveur)", ""]
    lines += [f"- {label} : {_fmt_int(count)}" for label, count in outside.items()]
    if sum(outside.values()):
        alerts.append(f"barres : {sum(outside.values())} hors heures de cotation (horaires ou règle d'heure à revoir)")

    expected, gaps = find_gaps(server, schedule)
    missing = sum(gap.minutes for gap in gaps)
    lines += ["", "## Trous (minutes de cotation sans barre)", ""]
    share = missing / expected if expected else 0.0
    lines.append(f"- {_fmt_int(missing)} minutes manquantes sur {_fmt_int(expected)} attendues ({share:.2%}).")
    for low, high in GAP_BUCKETS:
        count = sum(1 for g in gaps if g.minutes >= low and (high is None or g.minutes <= high))
        label = f"{low} min" if low == high else (f"{low} à {high} min" if high else f"{low} min et plus")
        lines.append(f"- trous de {label} : {_fmt_int(count)}")
    longest = sorted(gaps, key=lambda g: g.minutes, reverse=True)[:20]
    if longest:
        lines += ["", "Plus longs trous (jours fériés probables au-delà de 20 h) :", ""]
        lines += ["| début (heure serveur) | jour | fin (heure serveur) | minutes |", "|---|---|---|---|"]
        lines += [
            f"| {g.first_missing:%Y-%m-%d %H:%M} | {_DAYS_FR[g.first_missing.weekday()]} | "
            f"{g.last_missing:%Y-%m-%d %H:%M} | {_fmt_int(g.minutes)} |"
            for g in longest
        ]

    lines += ["", "## Règle d'heure serveur (validation sur tout l'historique)", ""]
    check = daily_close_check(bars, rule)
    lines.append(f"- Verdict : {'COHÉRENTE' if check.consistent else 'INCOHÉRENTE'} : {check.explanation()}.")
    lines.append(f"- Heure de référence : {rule.reference_tz.key}.")
    if not check.table.empty:
        shown = check.table.assign(part=check.table["part"].map("{:.0%}".format)).set_index("année")
        lines += ["", *_table(shown)]
    if not check.consistent:
        alerts.append(f"règle d'heure serveur : {check.explanation()}")

    lines += ["", "## Mouvements extrêmes sur une minute", ""]
    extreme = spikes(bars, point)
    if extreme.empty:
        lines.append(f"- Aucun mouvement au-delà de {SPIKE_Z:g} écarts-types robustes.")
    else:
        lines.append(
            f"- {len(extreme)} plus gros mouvements (> {SPIKE_Z:g} écarts-types robustes). « revient » = pic de "
            "cotation probable ; sinon, annonce ou vrai mouvement de marché."
        )
        lines += ["", "| heure | variation (USD) | écarts-types | revient | spread |", "|---|---|---|---|---|"]
        lines += [
            f"| {row.time.tz_convert(zone):%Y-%m-%d %H:%M} | {row.move:+.2f} | {row.z:.0f} | "
            f"{'oui' if row.reverted else 'non'} | {row.spread} |"
            for row in extreme.itertuples()
        ]
        if extreme["reverted"].any():
            alerts.append(f"barres : {int(extreme['reverted'].sum())} pics qui reviennent aussitôt (à inspecter)")

    lines += ["", "## Spread des barres M1 (points)", ""]
    spread = bars["spread"].astype("float64")
    quantiles = spread.quantile([0.05, 0.5, 0.95])
    lines.append(
        f"- min {spread.min():.0f}, 5 % {quantiles[0.05]:.0f}, médiane {quantiles[0.5]:.0f}, "
        f"95 % {quantiles[0.95]:.0f}, max {spread.max():.0f}."
    )
    valid = bars.dropna(subset=["time"])
    outliers = spread_outliers(valid["time"], valid["spread"].astype("float64"), zone)
    lines.append(f"- Spreads nuls ou > {SPREAD_OUTLIER_FACTOR:g} x la médiane de leur heure : {_fmt_int(outliers)}.")
    lines += ["", f"Par heure ({zone}), sur les 12 derniers mois :", ""]
    recent = valid[valid["time"] >= valid["time"].max() - pd.Timedelta(days=365)]
    lines += _table(spread_by_hour(recent["time"], recent["spread"].astype("float64"), zone))

    if ticks is not None and not ticks.empty:
        lines += ["", "## Ticks", ""]
        checks = tick_checks(ticks, point)
        lines += [f"- {label} : {_fmt_int(int(value))}" for label, value in checks.items()]
        tick_local = ticks["time"].dropna().dt.tz_convert(zone)
        lines.append(f"- Période : du {tick_local.min():%Y-%m-%d %H:%M} au {tick_local.max():%Y-%m-%d %H:%M}.")
        tick_outside = off_hours(server_index(ticks["time_msc_server"].to_numpy() // 1000), schedule)
        lines.append(f"- Ticks hors heures de cotation : {_fmt_int(sum(tick_outside.values()))}.")
        for label in ("cotations croisées (ask < bid)", "horodatages en recul", "heures invalides (NaT)"):
            if checks[label]:
                alerts.append(f"ticks : {checks[label]} {label}")
        tick_spread = (ticks["ask"] - ticks["bid"]) / point
        lines += ["", f"Spread des ticks par heure ({zone}) :", ""]
        valid_ticks = ticks["time"].notna()
        lines += _table(spread_by_hour(ticks.loc[valid_ticks, "time"], tick_spread[valid_ticks], zone))
        lines += ["", "### Que contient la colonne spread des barres ?", ""]
        comparison = bar_spread_vs_ticks(bars, ticks, point)
        if comparison.empty or comparison["minutes"].iloc[0] == 0:
            lines.append("- Pas de minute commune entre barres et ticks.")
        else:
            best = comparison.iloc[0]
            lines.append(
                f"- Sur {_fmt_int(int(best['minutes']))} minutes communes, le spread des barres correspond le plus "
                f"souvent au **{best['statistique']}** des ticks de la minute ({best['égalité exacte']:.0%} "
                "d'égalité exacte)."
            )
            shown = comparison.assign(**{"égalité exacte": comparison["égalité exacte"].map("{:.0%}".format)})
            lines += ["", *_table(shown.set_index("statistique"), floats="{:.2f}")]

    lines += ["", "## Alertes", ""]
    lines += [f"- {alert}" for alert in alerts] or ["- Aucune."]
    return "\n".join(lines) + "\n", alerts
