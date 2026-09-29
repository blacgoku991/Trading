"""Journalisation : fichier rotatif horodaté en UTC, console en heure d'affichage, secrets masqués."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, tzinfo
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

LOGGER_NAME = "goldbot"
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"


class SecretRedactor(logging.Filter):
    """Remplace toute valeur secrète par *** (défense en profondeur, CLAUDE.md règle 7)."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        # Les plus longs d'abord, pour ne pas masquer partiellement un secret qui en contient un autre.
        self._secrets = sorted({secret for secret in secrets if secret}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


class _ZoneFormatter(logging.Formatter):
    def __init__(self, zone: tzinfo, time_format: str) -> None:
        super().__init__(_FORMAT)
        self._zone = zone
        self._time_format = time_format

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.fromtimestamp(record.created, tz=self._zone).strftime(self._time_format)


def setup_logging(
    *,
    log_dir: Path,
    level: str,
    max_bytes: int,
    backup_count: int,
    display_timezone: str,
    secrets: Iterable[str] = (),
) -> logging.Logger:
    """Configure le logger « goldbot » (idempotent) et le renvoie."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    redactor = SecretRedactor(secrets)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "goldbot.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    # Fichier : ISO 8601 en UTC, sans ambiguïté. Console : heure d'affichage (Paris).
    file_handler.setFormatter(_ZoneFormatter(UTC, "%Y-%m-%dT%H:%M:%SZ"))

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(_ZoneFormatter(ZoneInfo(display_timezone), "%Y-%m-%d %H:%M:%S"))
    # Message déjà affiché tel quel à l'écran (extra={"console": False}) : seulement dans le fichier.
    console_handler.addFilter(lambda record: getattr(record, "console", True))

    for handler in (file_handler, console_handler):
        handler.addFilter(redactor)
        logger.addHandler(handler)
    return logger
