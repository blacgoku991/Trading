"""Chargement et validation de la configuration (settings.yaml) et des secrets (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# Plafond dur du risque par trade (CLAUDE.md §3.5) : aucune configuration ne peut le dépasser.
RISK_PER_TRADE_HARD_CAP_PCT = 1.0


class ConfigError(Exception):
    """Configuration ou secrets invalides. Le message ne contient jamais de valeur secrète."""


def _check_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"fuseau horaire inconnu : {name!r}") from exc
    return name


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BotConfig(_Section):
    magic: int = Field(gt=0, lt=2**63)
    display_timezone: str = "Europe/Paris"

    @field_validator("display_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _check_timezone(value)


class ReconnectConfig(_Section):
    base_delay_s: float = Field(gt=0)
    max_delay_s: float = Field(gt=0)
    max_attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def _delays_ordered(self) -> ReconnectConfig:
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s doit être supérieur ou égal à base_delay_s")
        return self


class Mt5Config(_Section):
    timeout_ms: int = Field(gt=0)
    # True seulement si le terminal est lancé avec /portable (données dans son dossier d'installation).
    portable: bool = False
    reconnect: ReconnectConfig


class SymbolConfig(_Section):
    name: str = Field(min_length=1)
    search_group: str = Field(min_length=1)

    @property
    def is_auto(self) -> bool:
        return self.name.strip().lower() == "auto"


class ServerTimeConfig(_Section):
    reference_timezone: str
    offset_hours: int = Field(ge=-12, le=14)

    @field_validator("reference_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        return _check_timezone(value)


class MarketHoursConfig(_Section):
    week_open: time
    week_close: time
    daily_break_start: time
    daily_break_end: time
    closed_dates: tuple[date, ...] = ()

    @field_validator("week_open", "week_close", "daily_break_start", "daily_break_end", mode="before")
    @classmethod
    def _quoted_time(cls, value: object) -> object:
        # Sans guillemets, YAML lit 23:59 comme le nombre 1439 (base 60) : on exige une chaîne.
        if not isinstance(value, str):
            raise ValueError('heure à écrire entre guillemets au format "HH:MM", par exemple "23:59"')
        return value

    @model_validator(mode="after")
    def _break_spans_midnight(self) -> MarketHoursConfig:
        # Modèle Axi : la pause quotidienne chevauche minuit (23:59 -> 01:01).
        if self.daily_break_start <= self.daily_break_end:
            raise ValueError(
                "la pause quotidienne doit chevaucher minuit (daily_break_start > daily_break_end)"
            )
        return self


class FeedConfig(_Section):
    stale_after_s: float = Field(gt=0)
    grace_after_open_s: float = Field(ge=0)


class RiskConfig(_Section):
    risk_per_trade_pct: float = Field(gt=0, le=RISK_PER_TRADE_HARD_CAP_PCT)
    max_daily_loss_pct: float = Field(gt=0, le=100)
    max_drawdown_pct: float = Field(gt=0, le=100)
    max_open_positions: int = Field(ge=1)
    max_trades_per_day: int = Field(ge=1)
    max_consecutive_losses: int = Field(ge=1)


class TestOrderConfig(_Section):
    __test__ = False  # pas une classe de test pytest

    volume: float = Field(gt=0, le=0.01)
    stop_distance: float = Field(gt=0)
    deviation_points: int = Field(ge=0)


class PathsConfig(_Section):
    logs: Path
    reports: Path


class ExportConfig(_Section):
    # Dossier des fichiers exportés (ignoré par git).
    directory: Path
    # Barres M1 : date serveur la plus ancienne demandée (l'export s'arrête avant si l'historique du broker s'arrête).
    bars_since: date
    # Ticks : nombre de jours récents (calendaires) exportés, un fichier par semaine.
    tick_days: int = Field(ge=0, le=3650)


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    max_bytes: int = Field(gt=0)
    backup_count: int = Field(ge=1)


class Settings(_Section):
    bot: BotConfig
    mt5: Mt5Config
    symbol: SymbolConfig
    server_time: ServerTimeConfig
    market_hours: MarketHoursConfig
    feed: FeedConfig
    risk: RiskConfig
    test_order: TestOrderConfig
    paths: PathsConfig
    export: ExportConfig
    logging: LoggingConfig


def _format_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(racine)"
        lines.append(f"  - {location} : {error['msg']}")
    return "\n".join(lines)


def load_settings(path: Path) -> Settings:
    """Lit et valide config/settings.yaml. Lève ConfigError avec un message lisible."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"fichier de configuration introuvable : {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"configuration invalide ({path}) :\n{_format_errors(exc)}") from exc


_TRUE_WORDS = {"true", "1", "yes", "oui"}
_FALSE_WORDS = {"false", "0", "no", "non", ""}


@dataclass(frozen=True)
class Secrets:
    """Secrets lus dans .env (CLAUDE.md règle 7). Exclus du repr : jamais affichés ni journalisés."""

    login: int = field(repr=False)
    password: str = field(repr=False)
    server: str = field(repr=False)
    terminal_path: str | None = None
    live_trading: bool = False
    telegram_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)

    def sensitive_values(self) -> list[str]:
        """Valeurs à masquer si elles apparaissaient par erreur dans un journal ou un rapport."""
        values = [str(self.login), self.password, self.server, self.telegram_token, self.telegram_chat_id]
        return [value for value in values if value]


def _read_env_file(env_path: Path) -> dict[str, str | None]:
    if not env_path.is_file():
        return {}
    if env_path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff"):
        raise ConfigError(
            f"{env_path} est enregistré en UTF-16 : dans le Bloc-notes, Fichier > Enregistrer sous, "
            "encodage UTF-8"
        )
    try:
        return dotenv_values(env_path)
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{env_path} n'est pas lisible en UTF-8 : réenregistre-le en UTF-8") from exc


def _missing_secrets_message(env_path: Path | None, missing: list[str]) -> str:
    names = ", ".join(missing)
    if env_path is None:
        return f"secrets manquants : {names}"
    if not env_path.is_file():
        message = f"fichier {env_path} introuvable (secrets manquants : {names}). Crée-le : Copy-Item .env.example .env"
    else:
        message = (
            f"secrets vides ou absents dans {env_path} : {names}. Écris chaque valeur juste après le signe =, "
            "puis enregistre le fichier (Ctrl+S)"
        )
    stray = env_path.with_name(env_path.name + ".txt")
    if stray.is_file():
        message += f". Attention : le fichier {stray.name} existe, le Bloc-notes a peut-être ajouté .txt au nom"
    return message


def load_secrets(env_path: Path | None) -> Secrets:
    """Lit les secrets : variables d'environnement d'abord, puis le fichier .env."""
    env_path = Path(env_path) if env_path else None
    file_values = _read_env_file(env_path) if env_path else {}

    def get(key: str, *, strip: bool = True) -> str:
        value = os.environ.get(key)
        if value is None:
            value = file_values.get(key)
        value = value or ""
        return value.strip() if strip else value

    missing = [key for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER") if not get(key)]
    if missing:
        raise ConfigError(_missing_secrets_message(env_path, missing))

    login_text = get("MT5_LOGIN")
    if not login_text.isdigit():
        raise ConfigError("MT5_LOGIN doit être un nombre entier (numéro du compte MT5)")

    live_text = get("LIVE_TRADING").lower()
    if live_text in _TRUE_WORDS:
        live_trading = True
    elif live_text in _FALSE_WORDS:
        live_trading = False
    else:
        raise ConfigError("LIVE_TRADING doit valoir true ou false")

    # Entre guillemets doubles, python-dotenv interprète \t et \n : un chemin Windows
    # comme "C:\temp\new\terminal64.exe" deviendrait invalide sans erreur visible.
    terminal_path = get("MT5_PATH")
    if any(ord(char) < 32 for char in terminal_path):
        raise ConfigError(
            "MT5_PATH contient un caractère invisible : écris le chemin sans guillemets "
            "ou entre guillemets simples"
        )

    return Secrets(
        login=int(login_text),
        password=get("MT5_PASSWORD", strip=False),
        server=get("MT5_SERVER"),
        terminal_path=terminal_path or None,
        live_trading=live_trading,
        telegram_token=get("TELEGRAM_TOKEN") or None,
        telegram_chat_id=get("TELEGRAM_CHAT_ID") or None,
    )
