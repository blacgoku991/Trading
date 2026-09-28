"""Heure serveur du broker <-> UTC.

MT5 renvoie les heures (ticks, barres, deals) en heure SERVEUR encodée comme un epoch,
pas en UTC. Chez Axi : GMT+2 ou GMT+3 selon l'heure d'été de New York, soit
heure serveur = heure de New York + 7 h toute l'année (docs/RESEARCH.md §1.12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from goldbot.config import ServerTimeConfig

_EPOCH = datetime(1970, 1, 1)


class InvalidServerTime(ValueError):
    """Heure serveur ambiguë ou inexistante (changement d'heure, marché fermé à ces heures)."""


@dataclass(frozen=True)
class ServerTimeRule:
    """Heure serveur = heure locale de reference_tz + offset."""

    reference_tz: ZoneInfo
    offset: timedelta

    @classmethod
    def from_config(cls, config: ServerTimeConfig) -> ServerTimeRule:
        return cls(ZoneInfo(config.reference_timezone), timedelta(hours=config.offset_hours))

    def utc_offset_at(self, at_utc: datetime) -> timedelta:
        """Décalage heure serveur - UTC à l'instant donné (ex. +3 h en été chez Axi)."""
        return at_utc.astimezone(self.reference_tz).utcoffset() + self.offset

    def to_server(self, at_utc: datetime) -> datetime:
        """Heure serveur (naïve) correspondant à un instant UTC."""
        return at_utc.astimezone(self.reference_tz).replace(tzinfo=None) + self.offset

    def server_epoch(self, at_utc: datetime) -> float:
        """Instant UTC exprimé dans l'échelle « epoch serveur » utilisée par MT5."""
        return at_utc.timestamp() + self.utc_offset_at(at_utc).total_seconds()

    def server_to_utc(self, server_time: datetime) -> datetime:
        """Heure serveur (naïve) -> instant UTC. Lève InvalidServerTime aux changements d'heure."""
        local = server_time - self.offset
        first = local.replace(tzinfo=self.reference_tz, fold=0)
        second = local.replace(tzinfo=self.reference_tz, fold=1)
        if first.utcoffset() == second.utcoffset():
            return first.astimezone(UTC)
        round_trip = first.astimezone(UTC).astimezone(self.reference_tz).replace(tzinfo=None)
        kind = "ambiguë" if round_trip == local else "inexistante"
        raise InvalidServerTime(f"heure serveur {server_time} {kind} (changement d'heure)")

    def server_epoch_to_utc(self, epoch_seconds: float) -> datetime:
        """Epoch renvoyé par MT5 (heure serveur) -> instant UTC."""
        return self.server_to_utc(_EPOCH + timedelta(seconds=epoch_seconds))


def measured_offset(tick_epoch: float, now_utc: datetime) -> float:
    """Écart (s) entre l'heure d'un tick et l'horloge du PC : décalage serveur, moins l'âge du tick."""
    return tick_epoch - now_utc.timestamp()


def format_offset(offset: timedelta | float) -> str:
    """timedelta ou secondes -> « GMT+3 », « GMT+5:30 », « GMT-4 »."""
    seconds = offset.total_seconds() if isinstance(offset, timedelta) else offset
    sign = "+" if seconds >= 0 else "-"
    minutes = round(abs(seconds) / 60)
    hours, rest = divmod(minutes, 60)
    return f"GMT{sign}{hours}" + (f":{rest:02d}" if rest else "")
