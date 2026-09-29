"""Historique MT5 (barres M1 et ticks) : export vers des fichiers parquet, puis relecture.

MT5 renvoie les heures en heure SERVEUR encodée en epoch (docs/RESEARCH.md §1.12). Les
fichiers gardent cette valeur brute (time_server, time_msc_server) et ajoutent la colonne
time en UTC, calculée avec la règle d'heure serveur de la config.

Le terminal télécharge l'historique auprès du serveur au fil des demandes : chaque
tranche est relue jusqu'à obtenir deux lectures identiques.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from goldbot.broker import mt5_constants as C
from goldbot.broker.base import RATES_DTYPE, TICKS_DTYPE, Broker, BrokerError
from goldbot.data.timezones import ServerTimeRule

log = logging.getLogger("goldbot.history")

_EPOCH = datetime(1970, 1, 1)
# Sans données pendant ce nombre de mois d'affilée, on considère avoir atteint le début de l'historique.
STOP_AFTER_EMPTY_MONTHS = 6
# Plafond si le symbole n'a encore renvoyé aucune barre (évite de remonter jusqu'en 1970).
GIVE_UP_WITHOUT_DATA_MONTHS = 24


@dataclass(frozen=True)
class Chunk:
    """Tranche [start, end[ en epoch serveur (secondes)."""

    label: str
    start: int
    end: int


def server_epoch(day: date) -> int:
    """Minuit (heure serveur) du jour donné, en epoch serveur."""
    return int((datetime.combine(day, datetime.min.time()) - _EPOCH).total_seconds())


def _next_month(month: date) -> date:
    return (month.replace(day=28) + timedelta(days=4)).replace(day=1)


def month_chunks(since: date, until: date) -> list[Chunk]:
    """Mois (heure serveur) couvrant [since, until[, du plus récent au plus ancien."""
    chunks = []
    month = until.replace(day=1)
    while month >= since.replace(day=1):
        start, end = max(month, since), min(_next_month(month), until)
        if start < end:
            chunks.append(Chunk(f"{month:%Y-%m}", server_epoch(start), server_epoch(end)))
        month = (month - timedelta(days=1)).replace(day=1)
    return chunks


def day_chunks(since: date, until: date) -> list[Chunk]:
    """Jours du lundi au vendredi (heure serveur) de [since, until[, du plus ancien au plus récent.

    L'or ne cote pas le samedi ni le dimanche en heure serveur (config market_hours).
    """
    chunks = []
    day = since
    while day < until:
        if day.weekday() < 5:
            chunks.append(Chunk(day.isoformat(), server_epoch(day), server_epoch(day + timedelta(days=1))))
        day += timedelta(days=1)
    return chunks


def fetch_stable(
    fetch: Callable[[], np.ndarray],
    key: str,
    *,
    attempts: int = 4,
    pause_s: float = 0.5,
    empty_pause_s: float = 3.0,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[np.ndarray, bool]:
    """Relit une tranche jusqu'à deux lectures identiques ; renvoie (données, stable).

    Une lecture vide est reconfirmée après une pause plus longue : le terminal peut être
    en train de télécharger ce morceau d'historique.
    """
    previous = fetch()
    for _ in range(attempts - 1):
        sleep(empty_pause_s if len(previous) == 0 else pause_s)
        current = fetch()
        if len(current) == len(previous) and np.array_equal(current[key], previous[key]):
            return current, True
        previous = current
    return previous, False


def _require_fields(raw: np.ndarray, dtype: np.dtype, operation: str) -> None:
    names = raw.dtype.names or ()
    missing = [name for name in dtype.names if name not in names]
    if missing:
        raise ValueError(f"{operation} : champs absents {missing} (version du package MetaTrader5 ?)")


def bars_frame(raw: np.ndarray, rule: ServerTimeRule) -> pd.DataFrame:
    """Tableau copy_rates_* -> DataFrame (time en UTC, time_server brut en secondes)."""
    _require_fields(raw, RATES_DTYPE, "copy_rates_range")
    server = raw["time"].astype("int64")
    return pd.DataFrame(
        {
            "time": rule.server_ms_to_utc(server * 1000),
            "time_server": server,
            "open": raw["open"].astype("float64"),
            "high": raw["high"].astype("float64"),
            "low": raw["low"].astype("float64"),
            "close": raw["close"].astype("float64"),
            "tick_volume": raw["tick_volume"].astype("int64"),
            "spread": raw["spread"].astype("int32"),  # en points
            "real_volume": raw["real_volume"].astype("int64"),
        }
    )


def ticks_frame(raw: np.ndarray, rule: ServerTimeRule) -> pd.DataFrame:
    """Tableau copy_ticks_* -> DataFrame (time en UTC à la ms, time_msc_server brut)."""
    _require_fields(raw, TICKS_DTYPE, "copy_ticks_range")
    server_ms = raw["time_msc"].astype("int64")
    return pd.DataFrame(
        {
            "time": rule.server_ms_to_utc(server_ms),
            "time_msc_server": server_ms,
            "bid": raw["bid"].astype("float64"),
            "ask": raw["ask"].astype("float64"),
            "last": raw["last"].astype("float64"),
            "volume": raw["volume"].astype("int64"),
            "flags": raw["flags"].astype("int64"),  # TICK_FLAG_*
            "volume_real": raw["volume_real"].astype("float64"),
        }
    )


@dataclass(frozen=True)
class FileRecord:
    name: str
    rows: int
    first_utc: str | None
    last_utc: str | None
    size_bytes: int
    sha256: str


@dataclass
class ExportResult:
    files: list[FileRecord] = field(default_factory=list)
    unstable: list[str] = field(default_factory=list)  # tranches jamais relues à l'identique
    empty: list[str] = field(default_factory=list)  # tranches vides au milieu de l'historique
    failed: list[str] = field(default_factory=list)  # « tranche : erreur »

    @property
    def rows(self) -> int:
        return sum(record.rows for record in self.files)

    @property
    def size_bytes(self) -> int:
        return sum(record.size_bytes for record in self.files)


def safe_name(symbol: str) -> str:
    """Nom de symbole utilisable dans un nom de fichier (XAUUSD.pro reste XAUUSD.pro)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _iso(value: pd.Timestamp) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_parquet(frame: pd.DataFrame, path: Path, sort_by: str) -> FileRecord:
    """Écrit le fichier (trié, compressé zstd) de façon atomique et renvoie sa fiche."""
    frame = frame.sort_values(sort_by, kind="stable").reset_index(drop=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    frame.to_parquet(partial, compression="zstd", index=False)
    partial.replace(path)
    times = frame["time"].dropna()
    return FileRecord(
        name=path.name,
        rows=len(frame),
        first_utc=_iso(times.min()) if len(times) else None,
        last_utc=_iso(times.max()) if len(times) else None,
        size_bytes=path.stat().st_size,
        sha256=sha256_of(path),
    )


def _read_chunk(
    fetch: Callable[[], np.ndarray],
    key: str,
    reconnect: Callable[[], object],
    sleep: Callable[[float], None],
) -> tuple[np.ndarray, bool]:
    """fetch_stable, avec une reconnexion si la liaison avec le terminal est perdue."""
    try:
        return fetch_stable(fetch, key, sleep=sleep)
    except BrokerError as error:
        if error.code not in C.IPC_ERROR_CODES:
            raise
        log.warning("liaison perdue pendant l'export (%s) : reconnexion", error)
        reconnect()
        return fetch_stable(fetch, key, sleep=sleep)


def thousands(value: int) -> str:
    """12345678 -> « 12 345 678 »."""
    return f"{value:,}".replace(",", " ")


def megabytes(size_bytes: int) -> str:
    return f"{size_bytes / 1e6:.1f} Mo".replace(".", ",")


def export_bars(
    broker: Broker,
    symbol: str,
    *,
    since: date,
    until: date,
    out_dir: Path,
    rule: ServerTimeRule,
    reconnect: Callable[[], object] = lambda: None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = print,
) -> ExportResult:
    """Barres M1 de [since, until[ (dates serveur), mois par mois en remontant le temps.

    Un fichier par année civile (heure serveur). On s'arrête quand l'historique du broker
    s'arrête : STOP_AFTER_EMPTY_MONTHS mois vides d'affilée.
    """
    result = ExportResult()
    frames: list[pd.DataFrame] = []
    year: str | None = None
    empty_run = 0
    seen_data = False
    # Mois vides depuis les dernières données : de vrais trous seulement si des données plus anciennes suivent.
    pending_empty: list[str] = []

    def flush() -> None:
        if frames:
            path = out_dir / f"{safe_name(symbol)}_M1_{year}.parquet"
            record = write_parquet(pd.concat(frames, ignore_index=True), path, "time_server")
            result.files.append(record)
            progress(f"  {year} : {thousands(record.rows)} barres -> {record.name} ({megabytes(record.size_bytes)})")
            frames.clear()

    for chunk in month_chunks(since, until):
        if chunk.label[:4] != year:
            flush()
            year = chunk.label[:4]
        fetch = partial(broker.rates_range, symbol, C.TIMEFRAME_M1, chunk.start, chunk.end - 1)
        failed = False
        try:
            raw, stable = _read_chunk(fetch, "time", reconnect, sleep)
        except BrokerError as error:
            result.failed.append(f"{chunk.label} : {error}")
            raw, stable, failed = np.zeros(0, RATES_DTYPE), True, True
        raw = raw[(raw["time"] >= chunk.start) & (raw["time"] < chunk.end)]
        if len(raw) == 0:
            empty_run += 1
            if seen_data and not failed:
                pending_empty.append(chunk.label)
            if empty_run >= (STOP_AFTER_EMPTY_MONTHS if seen_data else GIVE_UP_WITHOUT_DATA_MONTHS):
                break
            continue
        seen_data, empty_run = True, 0
        result.empty += pending_empty
        pending_empty = []
        if not stable:
            result.unstable.append(chunk.label)
        frames.append(bars_frame(raw, rule))
    flush()
    return result


def export_ticks(
    broker: Broker,
    symbol: str,
    *,
    since: date,
    until: date,
    out_dir: Path,
    rule: ServerTimeRule,
    reconnect: Callable[[], object] = lambda: None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = print,
) -> ExportResult:
    """Ticks de [since, until[ (dates serveur), jour par jour ; un fichier par semaine ISO."""
    result = ExportResult()
    frames: list[pd.DataFrame] = []
    week: str | None = None

    def flush() -> None:
        if frames:
            path = out_dir / f"{safe_name(symbol)}_ticks_{week}.parquet"
            record = write_parquet(pd.concat(frames, ignore_index=True), path, "time_msc_server")
            result.files.append(record)
            progress(f"  {week} : {thousands(record.rows)} ticks -> {record.name} ({megabytes(record.size_bytes)})")
            frames.clear()

    for chunk in day_chunks(since, until):
        iso = date.fromisoformat(chunk.label).isocalendar()
        label = f"{iso.year}-W{iso.week:02d}"
        if label != week:
            flush()
            week = label
        # Borne haute prise large puis filtrée : on ne dépend pas de son inclusion par MT5.
        fetch = partial(broker.ticks_range, symbol, chunk.start, chunk.end, C.COPY_TICKS_ALL)
        try:
            raw, stable = _read_chunk(fetch, "time_msc", reconnect, sleep)
        except BrokerError as error:
            result.failed.append(f"{chunk.label} : {error}")
            continue
        raw = raw[(raw["time_msc"] >= chunk.start * 1000) & (raw["time_msc"] < chunk.end * 1000)]
        if len(raw) == 0:
            result.empty.append(chunk.label)
            continue
        if not stable:
            result.unstable.append(chunk.label)
        frames.append(ticks_frame(raw, rule))
    flush()
    return result


# --- Relecture (Codespaces, backtest) ---------------------------------------------------


def verify_files(folder: Path, manifest_name: str = "manifest.json") -> list[str]:
    """Compare les fichiers présents au manifeste de l'export ; renvoie la liste des problèmes."""
    manifest_path = Path(folder) / manifest_name
    if not manifest_path.is_file():
        return [f"{manifest_name} absent : intégrité des fichiers non vérifiable"]
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    for section in ("bars", "ticks"):
        for record in manifest.get(section, {}).get("files", []):
            path = Path(folder) / record["name"]
            if not path.is_file():
                problems.append(f"{record['name']} : fichier manquant")
            elif path.stat().st_size != record["size_bytes"] or sha256_of(path) != record["sha256"]:
                problems.append(f"{record['name']} : contenu différent du manifeste (fichier abîmé ?)")
    return problems


def load_bars(folder: Path, symbol: str, *, deduplicate: bool = True) -> pd.DataFrame:
    """Barres M1 de tous les fichiers annuels, triées ; doublons d'horodatage retirés.

    deduplicate=False garde les doublons, pour que le contrôle qualité puisse les compter.
    """
    paths = sorted(Path(folder).glob(f"{safe_name(symbol)}_M1_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"aucun fichier {safe_name(symbol)}_M1_*.parquet dans {folder}")
    bars = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    bars = bars.sort_values("time_server", kind="stable")
    if deduplicate:
        bars = bars.drop_duplicates("time_server", keep="first")
    return bars.reset_index(drop=True)


# Un jour de cotation complet compte environ 1 380 barres M1 : en dessous de ce seuil, ce n'est pas du M1.
DENSE_DAY_BARS = 1000


def minute_history_start(bars: pd.DataFrame, *, run_days: int = 5) -> int | None:
    """Epoch serveur (s) du premier jour d'une série de run_days jours denses (vrai historique M1).

    Avant, certains brokers ne gardent qu'environ une barre par jour (données journalières) :
    inutilisable pour une stratégie à la minute.
    """
    days = bars["time_server"].to_numpy() // 86_400
    counts = pd.Series(1, index=days).groupby(level=0).size()
    dense = (counts >= DENSE_DAY_BARS).astype(int)
    streak = dense.rolling(run_days).sum()
    hits = streak.index[streak.to_numpy() >= run_days]
    if len(hits) == 0:
        return None
    first = counts.index.get_loc(hits[0]) - run_days + 1
    return int(counts.index[first]) * 86_400


def exported_symbols(folder: Path) -> list[str]:
    """Symboles présents dans un dossier d'export (préfixe des fichiers *_M1_*.parquet)."""
    return sorted({path.name.rsplit("_M1_", 1)[0] for path in Path(folder).glob("*_M1_*.parquet")})


def load_ticks(folder: Path, symbol: str) -> pd.DataFrame:
    """Ticks de tous les fichiers hebdomadaires, triés (plusieurs ticks par ms sont possibles)."""
    paths = sorted(Path(folder).glob(f"{safe_name(symbol)}_ticks_*.parquet"))
    if not paths:
        raise FileNotFoundError(f"aucun fichier {safe_name(symbol)}_ticks_*.parquet dans {folder}")
    ticks = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    return ticks.sort_values("time_msc_server", kind="stable").reset_index(drop=True)
