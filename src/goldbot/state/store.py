"""État du bot en SQLite (bibliothèque standard) : signaux traités, positions, état du risque.

Sert à l'idempotence (jamais deux ordres pour le même signal, même après un redémarrage) et à la
reprise : positions ouvertes, sorties horaires à venir, compteurs des limites de risque.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from goldbot.risk.limits import RiskLimits
from goldbot.strategies.base import OrderIntent

# Cycle de vie d'un signal.
SENDING = "envoi"  # enregistré juste avant order_send : à réconcilier si le bot s'arrête ici
OPEN = "ouvert"
CLOSED = "fermé"
SKIPPED = "ignoré"  # refusé par une limite ou par la taille minimale
FAILED = "échec"  # refusé par le broker
SIMULATED = "simulé"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    tag TEXT PRIMARY KEY,
    signal_time TEXT NOT NULL,
    side INTEGER NOT NULL,
    sl REAL NOT NULL,
    tp REAL,
    exit_at TEXT,
    reason TEXT,
    status TEXT NOT NULL,
    detail TEXT,
    position INTEGER,
    volume REAL,
    entry_price REAL,
    exit_price REAL,
    pnl REAL,
    created_utc TEXT NOT NULL,
    updated_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SignalRow:
    tag: str
    signal_time: pd.Timestamp
    side: int
    sl: float
    tp: float | None
    exit_at: pd.Timestamp | None
    status: str
    position: int | None
    volume: float | None
    entry_price: float | None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _stamp(value: pd.Timestamp | None) -> str | None:
    return None if value is None else value.tz_convert("UTC").isoformat()


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- signaux ------------------------------------------------------------------------------

    def seen(self, tag: str) -> bool:
        return self._db.execute("SELECT 1 FROM signals WHERE tag = ?", (tag,)).fetchone() is not None

    def record(self, intent: OrderIntent, status: str, detail: str = "") -> bool:
        """Enregistre un signal nouveau ; False s'il l'était déjà (l'ordre ne doit pas être renvoyé)."""
        now = _now()
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO signals (tag, signal_time, side, sl, tp, exit_at, reason, status, detail, "
                    "created_utc, updated_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        intent.tag,
                        _stamp(intent.time),
                        intent.side,
                        intent.sl,
                        intent.tp,
                        _stamp(intent.exit_at),
                        intent.reason,
                        status,
                        detail,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def update(self, tag: str, **fields: object) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._db:
            self._db.execute(
                # Noms de colonnes fournis par le code (jamais par l'utilisateur), valeurs en paramètres.
                f"UPDATE signals SET {columns}, updated_utc = ? WHERE tag = ?",
                (*fields.values(), _now(), tag),
            )

    def with_status(self, *statuses: str) -> list[SignalRow]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._db.execute(
            "SELECT tag, signal_time, side, sl, tp, exit_at, status, position, volume, entry_price "
            f"FROM signals WHERE status IN ({marks}) ORDER BY signal_time",
            statuses,
        ).fetchall()
        return [
            SignalRow(
                tag=row[0],
                signal_time=pd.Timestamp(row[1]),
                side=row[2],
                sl=row[3],
                tp=row[4],
                exit_at=None if row[5] is None else pd.Timestamp(row[5]),
                status=row[6],
                position=row[7],
                volume=row[8],
                entry_price=row[9],
            )
            for row in rows
        ]

    def detail(self, tag: str) -> dict[str, object]:
        cursor = self._db.execute("SELECT * FROM signals WHERE tag = ?", (tag,))
        names = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        return dict(zip(names, row, strict=True)) if row else {}

    # --- limites de risque ----------------------------------------------------------------------

    def save_risk(self, limits: RiskLimits) -> None:
        state = {
            "peak_equity": limits.peak_equity,
            "day": limits.day,
            "day_start_equity": limits.day_start_equity,
            "trades_today": limits.trades_today,
            "consecutive_losses": limits.consecutive_losses,
            "stopped_day": limits.stopped_day,
            "pause_until": _stamp(limits.pause_until),
            "halted": limits.halted,
        }
        with self._db:
            self._db.execute(
                "INSERT INTO risk_state (id, state) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET state = excluded.state",
                (json.dumps(state),),
            )

    def load_risk(self, limits: RiskLimits) -> bool:
        """Recharge les compteurs sauvegardés dans limits ; False s'il n'y en avait pas."""
        row = self._db.execute("SELECT state FROM risk_state WHERE id = 1").fetchone()
        if row is None:
            return False
        state = json.loads(row[0])
        for name, value in state.items():
            if name == "pause_until" and value is not None:
                value = pd.Timestamp(value)
            setattr(limits, name, value)
        return True
