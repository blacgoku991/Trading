"""État de l'expérience de scalping (SQLite) : versions, signaux, ordres démo, simulation parallèle.

- versions : chaque stratégie avec sa version et l'empreinte de ses réglages (JSON complet gardé) ;
- signals : une ligne par signal (décision : refusé, non envoyé, envoyé, échec), identifiant = tag ;
- orders : une ligne par ordre envoyé au broker. Un signal fractionné a plusieurs ordres (#1, #2…),
  un signal normal en a un. Le commentaire MT5 de l'ordre (tag#n) relie la position à son signal ;
- sims : la simulation avec glissement supplémentaire, un trade simulé par signal accepté.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Statuts d'un signal.
REFUSED = "refusé"
NOT_SENT = "non envoyé"  # accepté, mais pas transmis au broker (mode --simulation)
SENT = "envoyé"
FAILED = "échec"
# Statuts d'un ordre (FAILED aussi).
SENDING = "envoi"  # enregistré juste avant l'envoi : à rapprocher des positions si le bot s'arrête ici
OPEN = "ouvert"
CLOSED = "fermé"

SCHEMA_VERSION = "3"  # 2 : stratégies versionnées, ordres fractionnables ; 3 : variante apprise par signal

# Trade de la vérification technique (--verification) : gardé dans l'état, exclu des résultats.
VERIFY_PREFIX = "SC-VERIF-"
_EXPERIMENT = f"tag NOT LIKE '{VERIFY_PREFIX}%'"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS versions (
    strategy TEXT NOT NULL,
    version INTEGER NOT NULL,
    params_hash TEXT NOT NULL,
    params TEXT NOT NULL,
    first_ms INTEGER NOT NULL,
    PRIMARY KEY (strategy, version, params_hash)
);
CREATE TABLE IF NOT EXISTS signals (
    tag TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    version INTEGER,
    params_hash TEXT,
    key TEXT,
    time_ms INTEGER NOT NULL,
    side INTEGER NOT NULL,
    level REAL,
    structure REAL,
    spread REAL,
    status TEXT NOT NULL,
    reason TEXT,
    detail TEXT,
    volume REAL,
    entry REAL,
    sl REAL,
    tp REAL,
    risk REAL,
    parts INTEGER,
    variante TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    tag TEXT NOT NULL,
    part INTEGER NOT NULL,
    comment TEXT NOT NULL UNIQUE,
    side INTEGER NOT NULL,
    volume REAL NOT NULL,
    sl REAL,
    tp REAL,
    risk REAL,
    spread REAL,
    status TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    detail TEXT,
    position INTEGER,
    open_price REAL,
    open_ms INTEGER,
    deadline_ms INTEGER,
    entry_fees REAL,
    exit_price REAL,
    exit_reason TEXT,
    exit_ms INTEGER,
    est_pnl REAL,
    profit REAL,
    commission REAL,
    swap REAL,
    fee REAL,
    real_pnl REAL,
    closed_ms INTEGER,
    PRIMARY KEY (tag, part)
);
CREATE TABLE IF NOT EXISTS sims (
    tag TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
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
    fees REAL,
    closed_ms INTEGER,
    state TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Order:
    tag: str
    part: int
    comment: str
    side: int
    volume: float
    sl: float
    tp: float
    risk: float
    spread: float
    time_ms: int
    position: int | None
    open_price: float | None
    open_ms: int | None
    deadline_ms: int | None
    entry_fees: float | None
    strategy: str
    key: str | None

    @property
    def label(self) -> str:
        """« SC-B-…-L » pour un ordre unique, « SC-B-…-L (ordre 2) » pour un morceau de trade fractionné."""
        return self.tag if self.part == 1 else f"{self.tag} (ordre {self.part})"


_ORDER_COLUMNS = (
    "o.tag, o.part, o.comment, o.side, o.volume, o.sl, o.tp, o.risk, o.spread, o.time_ms, o.position, "
    "o.open_price, o.open_ms, o.deadline_ms, o.entry_fees, s.strategy, s.key"
)


def params_hash(params: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:8]


def _schema_of(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        db.close()
    return row[0] if row else "1"


class ScalpStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.archived: Path | None = None
        if path.exists() and _schema_of(path) != SCHEMA_VERSION:
            # Base d'une version précédente du bot : gardée à part, jamais réécrite.
            self.archived = path.with_name(f"{path.stem}_ancien_format_{datetime.now(UTC):%Y%m%dT%H%M%SZ}{path.suffix}")
            path.rename(self.archived)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.set_meta("schema", SCHEMA_VERSION)

    def close(self) -> None:
        self._db.close()

    def _rows(self, query: str, args: tuple = ()) -> list[dict[str, object]]:
        cursor = self._db.execute(query, args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    # --- réglages et versions ------------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def bump(self, key: str) -> int:
        """Compteur (doublons évités…)."""
        value = int(self.meta(key) or 0) + 1
        self.set_meta(key, str(value))
        return value

    def register_version(self, strategy: str, version: int, digest: str, params: dict[str, object],
                         first_ms: int) -> None:  # fmt: skip
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO versions (strategy, version, params_hash, params, first_ms) VALUES (?, ?, ?, ?, ?)",
                (strategy, version, digest, json.dumps(params, sort_keys=True, default=str), first_ms),
            )

    def versions(self) -> list[dict[str, object]]:
        return self._rows("SELECT * FROM versions ORDER BY first_ms")

    def has_activity(self) -> bool:
        return self._db.execute(f"SELECT 1 FROM signals WHERE {_EXPERIMENT} LIMIT 1").fetchone() is not None

    # --- signaux -------------------------------------------------------------------------------

    def seen(self, tag: str) -> bool:
        return self._db.execute("SELECT 1 FROM signals WHERE tag = ?", (tag,)).fetchone() is not None

    def record_signal(self, tag: str, **fields: object) -> bool:
        """Nouveau signal ; False s'il existait déjà (jamais deux décisions pour le même signal)."""
        columns = ["tag", *fields]
        marks = ", ".join("?" for _ in columns)
        try:
            with self._db:
                self._db.execute(
                    f"INSERT INTO signals ({', '.join(columns)}) VALUES ({marks})", (tag, *fields.values())
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def update_signal(self, tag: str, **fields: object) -> None:
        # Noms de colonnes fournis par le code, valeurs en paramètres.
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._db:
            self._db.execute(f"UPDATE signals SET {columns} WHERE tag = ?", (*fields.values(), tag))

    def signal(self, tag: str) -> dict[str, object]:
        rows = self._rows("SELECT * FROM signals WHERE tag = ?", (tag,))
        return rows[0] if rows else {}

    def status_counts(self) -> dict[str, int]:
        query = f"SELECT status, COUNT(*) FROM signals WHERE {_EXPERIMENT} GROUP BY status"
        return dict(self._db.execute(query).fetchall())

    def trading_days(self) -> int:
        row = self._db.execute(
            f"SELECT COUNT(DISTINCT time_ms / 86400000) FROM signals WHERE status = ? AND {_EXPERIMENT}", (SENT,)
        ).fetchone()
        return int(row[0])

    # --- ordres --------------------------------------------------------------------------------

    def record_order(self, tag: str, part: int, *, comment: str, side: int, volume: float, sl: float, tp: float,
                     risk: float, spread: float, time_ms: int) -> bool:  # fmt: skip
        """Ordre sur le point de partir ; False s'il existait déjà (jamais deux envois du même ordre)."""
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO orders (tag, part, comment, side, volume, sl, tp, risk, spread, status, time_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (tag, part, comment, side, volume, sl, tp, risk, spread, SENDING, time_ms),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def update_order(self, tag: str, part: int, **fields: object) -> None:
        columns = ", ".join(f"{name} = ?" for name in fields)
        with self._db:
            self._db.execute(f"UPDATE orders SET {columns} WHERE tag = ? AND part = ?", (*fields.values(), tag, part))

    def orders(self, *statuses: str) -> list[Order]:
        marks = ", ".join("?" for _ in statuses)
        rows = self._db.execute(
            f"SELECT {_ORDER_COLUMNS} FROM orders o JOIN signals s ON s.tag = o.tag "
            f"WHERE o.status IN ({marks}) ORDER BY o.time_ms, o.part",
            statuses,
        ).fetchall()
        return [Order(*row) for row in rows]

    def order(self, tag: str, part: int) -> dict[str, object]:
        rows = self._rows("SELECT * FROM orders WHERE tag = ? AND part = ?", (tag, part))
        return rows[0] if rows else {}

    def known_comments(self) -> set[str]:
        return {row[0] for row in self._db.execute("SELECT comment FROM orders").fetchall()}

    def closed_orders(self) -> list[dict[str, object]]:
        """Ordres démo fermés de l'expérience, avec la stratégie et la version de leur signal."""
        return self._rows(
            "SELECT o.*, s.strategy, s.version, s.params_hash FROM orders o JOIN signals s ON s.tag = o.tag "
            f"WHERE o.status = ? AND o.real_pnl IS NOT NULL AND o.{_EXPERIMENT} ORDER BY o.closed_ms",
            (CLOSED,),
        )

    def realized_since(self, time_ms: int) -> float:
        row = self._db.execute(
            f"SELECT COALESCE(SUM(real_pnl), 0) FROM orders WHERE status = ? AND closed_ms >= ? AND {_EXPERIMENT}",
            (CLOSED, time_ms),
        ).fetchone()
        return float(row[0])

    # --- simulation parallèle ------------------------------------------------------------------

    def record_sim(self, tag: str, strategy: str, time_ms: int, side: int, entry: float, sl: float, tp: float,
                   volume: float, sent: bool) -> None:  # fmt: skip
        with self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO sims (tag, strategy, time_ms, side, entry, sl, tp, volume, sent, state) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ouvert')",
                (tag, strategy, time_ms, side, entry, sl, tp, volume, int(sent)),
            )

    def close_sim(self, tag: str, exit_price: float, reason: str, pnl: float, fees: float, closed_ms: int) -> None:
        with self._db:
            self._db.execute(
                "UPDATE sims SET exit_price = ?, exit_reason = ?, pnl = ?, fees = ?, closed_ms = ?, state = 'fermé' "
                "WHERE tag = ?",
                (exit_price, reason, pnl, fees, closed_ms, tag),
            )

    def interrupt_open_sims(self) -> int:
        """Au redémarrage : les trades simulés en cours sont perdus (suivi en mémoire), on le note."""
        with self._db:
            cursor = self._db.execute("UPDATE sims SET state = 'interrompu' WHERE state = 'ouvert'")
        return cursor.rowcount

    def closed_sims(self) -> list[dict[str, object]]:
        return self._rows("SELECT * FROM sims WHERE state = 'fermé' ORDER BY closed_ms")

    def open_sims(self) -> list[dict[str, object]]:
        return self._rows("SELECT * FROM sims WHERE state = 'ouvert' ORDER BY time_ms")

    def sim_realized_since(self, time_ms: int) -> float:
        row = self._db.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM sims WHERE state = 'fermé' AND closed_ms >= ?", (time_ms,)
        ).fetchone()
        return float(row[0])
