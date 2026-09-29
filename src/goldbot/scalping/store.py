"""État de l'expérience de scalping (SQLite) : signaux, trades démo, simulation parallèle, réglages figés."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Statuts d'un signal.
REFUSED = "refusé"
NOT_SENT = "non envoyé"  # accepté, mais pas transmis au broker (cadence non vérifiée ou --simulation)
SENDING = "envoi"  # enregistré juste avant l'envoi : à réconcilier si le bot s'arrête ici
OPEN = "ouvert"
CLOSED = "fermé"
FAILED = "échec"

# Trade de la vérification technique (--verification) : gardé dans l'état, exclu des résultats.
VERIFY_PREFIX = "SC-VERIF-"
_EXPERIMENT = f"tag NOT LIKE '{VERIFY_PREFIX}%'"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS signals (
    tag TEXT PRIMARY KEY,
    time_ms INTEGER NOT NULL,
    side INTEGER NOT NULL,
    level REAL,
    spread REAL,
    status TEXT NOT NULL,
    detail TEXT,
    volume REAL,
    entry REAL,
    sl REAL,
    tp REAL,
    risk REAL,
    position INTEGER,
    open_price REAL,
    deadline_ms INTEGER,
    exit_price REAL,
    exit_reason TEXT,
    est_pnl REAL,
    real_pnl REAL,
    closed_ms INTEGER
);
CREATE TABLE IF NOT EXISTS sims (
    tag TEXT PRIMARY KEY,
    time_ms INTEGER NOT NULL,
    side INTEGER NOT NULL,
    entry REAL NOT NULL,
    sl REAL NOT NULL,
    tp REAL NOT NULL,
    volume REAL NOT NULL,
    sent INTEGER NOT NULL,
    exit_price REAL,
    exit_reason TEXT,
    pnl REAL,
    closed_ms INTEGER,
    state TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class DemoTrade:
    tag: str
    side: int
    volume: float
    sl: float
    tp: float
    risk: float
    position: int | None
    open_price: float | None
    deadline_ms: int
    time_ms: int


def params_hash(params: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ScalpStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- réglages figés ----------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def has_activity(self) -> bool:
        return self._db.execute(f"SELECT 1 FROM signals WHERE {_EXPERIMENT} LIMIT 1").fetchone() is not None

    # --- signaux -------------------------------------------------------------------------------

    def seen(self, tag: str) -> bool:
        return self._db.execute("SELECT 1 FROM signals WHERE tag = ?", (tag,)).fetchone() is not None

    def record(self, tag: str, time_ms: int, side: int, status: str, **fields: object) -> bool:
        """Nouveau signal ; False s'il existait déjà (jamais deux ordres pour le même signal)."""
        columns = ["tag", "time_ms", "side", "status", *fields]
        marks = ", ".join("?" for _ in columns)
        try:
            with self._db:
                self._db.execute(
                    f"INSERT INTO signals ({', '.join(columns)}) VALUES ({marks})",
                    (tag, time_ms, side, status, *fields.values()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def update(self, tag: str, **fields: object) -> None:
        # Noms de colonnes fournis par le code, valeurs en paramètres.
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._db:
            self._db.execute(f"UPDATE signals SET {columns} WHERE tag = ?", (*fields.values(), tag))

    def demo_trades(self, *statuses: str) -> list[DemoTrade]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._db.execute(
            "SELECT tag, side, volume, sl, tp, risk, position, open_price, deadline_ms, time_ms "
            f"FROM signals WHERE status IN ({marks}) ORDER BY time_ms",
            statuses,
        ).fetchall()
        return [DemoTrade(*row) for row in rows]

    def signal(self, tag: str) -> dict[str, object]:
        cursor = self._db.execute("SELECT * FROM signals WHERE tag = ?", (tag,))
        names = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        return dict(zip(names, row, strict=True)) if row else {}

    def closed_demo_results(self) -> list[float]:
        rows = self._db.execute(
            f"SELECT real_pnl FROM signals WHERE status = ? AND real_pnl IS NOT NULL AND {_EXPERIMENT}", (CLOSED,)
        ).fetchall()
        return [row[0] for row in rows]

    def realized_since(self, time_ms: int) -> float:
        row = self._db.execute(
            f"SELECT COALESCE(SUM(real_pnl), 0) FROM signals WHERE status = ? AND closed_ms >= ? AND {_EXPERIMENT}",
            (CLOSED, time_ms),
        ).fetchone()
        return float(row[0])

    def trading_days(self) -> int:
        row = self._db.execute(
            f"SELECT COUNT(DISTINCT time_ms / 86400000) FROM signals WHERE status IN (?, ?) AND {_EXPERIMENT}",
            (OPEN, CLOSED),
        ).fetchone()
        return int(row[0])

    # --- simulation parallèle ------------------------------------------------------------------

    def record_sim(self, tag: str, time_ms: int, side: int, entry: float, sl: float, tp: float, volume: float,
                   sent: bool) -> None:  # fmt: skip
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO sims (tag, time_ms, side, entry, sl, tp, volume, sent, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ouvert')",
                (tag, time_ms, side, entry, sl, tp, volume, int(sent)),
            )

    def close_sim(self, tag: str, exit_price: float, reason: str, pnl: float, closed_ms: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE sims SET exit_price = ?, exit_reason = ?, pnl = ?, closed_ms = ?, state = 'fermé' "
                "WHERE tag = ?",
                (exit_price, reason, pnl, closed_ms, tag),
            )

    def interrupt_open_sims(self) -> int:
        """Au redémarrage : les trades simulés en cours sont perdus (suivi en mémoire), on le note."""
        with self._db:
            cursor = self._db.execute("UPDATE sims SET state = 'interrompu' WHERE state = 'ouvert'")
        return cursor.rowcount

    def sim_results(self) -> list[float]:
        return [row[0] for row in self._db.execute("SELECT pnl FROM sims WHERE state = 'fermé'").fetchall()]

    def open_sims(self) -> list[tuple[int, float, float]]:
        """(côté, prix d'entrée, lots) des trades simulés en cours."""
        return self._db.execute("SELECT side, entry, volume FROM sims WHERE state = 'ouvert'").fetchall()

    def sim_realized_since(self, time_ms: int) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM sims WHERE state = 'fermé' AND closed_ms >= ?", (time_ms,)
        ).fetchone()
        return float(row[0])

    def status_counts(self) -> dict[str, int]:
        query = f"SELECT status, COUNT(*) FROM signals WHERE {_EXPERIMENT} GROUP BY status"
        return dict(self._db.execute(query).fetchall())


def utc_now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
