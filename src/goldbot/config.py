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
            raise ValueError("la pause quotidienne doit chevaucher minuit (daily_break_start > daily_break_end)")
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


def _require_quoted_time(value: object) -> object:
    # Sans guillemets, YAML lit 23:59 comme le nombre 1439 (base 60) : on exige une chaîne.
    if not isinstance(value, str):
        raise ValueError('heure à écrire entre guillemets au format "HH:MM", par exemple "07:00"')
    return value


class BacktestConfig(_Section):
    # Capital de départ, dans la devise de cotation (USD) : résultats en % et en R indépendants de la devise.
    initial_equity: float = Field(gt=0)
    # Début de la période hors échantillon (UTC) : réservée à la validation finale, exclue par défaut.
    out_of_sample_start: date
    # Coût d'entrée : spread de chaque barre x multiplicateur, avec un plancher (points).
    spread_multiplier: float = Field(ge=1.0)
    min_spread_points: float = Field(ge=0)
    # Glissement défavorable par exécution au marché ou au SL (points).
    slippage_points: float = Field(ge=0)
    commission_per_lot_side: float = Field(ge=0)


class AsianBreakoutConfig(_Section):
    # Heures de Londres (zoneinfo : changements d'heure gérés).
    range_start: time
    range_end: time
    entry_end: time
    exit_time: time
    buffer_atr: float = Field(ge=0)
    tp_r: float = Field(gt=0)
    min_range_atr: float = Field(ge=0)
    max_range_atr: float = Field(gt=0)

    @field_validator("range_start", "range_end", "entry_end", "exit_time", mode="before")
    @classmethod
    def _quoted_time(cls, value: object) -> object:
        return _require_quoted_time(value)

    @model_validator(mode="after")
    def _ordered(self) -> AsianBreakoutConfig:
        if not self.range_start < self.range_end < self.entry_end < self.exit_time:
            raise ValueError("il faut range_start < range_end < entry_end < exit_time")
        if self.min_range_atr >= self.max_range_atr:
            raise ValueError("min_range_atr doit être inférieur à max_range_atr")
        return self


class SessionMomentumConfig(_Section):
    # Place de référence (fuseau IANA) et heures locales.
    zone: str
    signal_time: time
    exit_time: time
    min_move_atr: float = Field(ge=0)
    sl_atr: float = Field(gt=0)
    tp_r: float = Field(ge=0)  # 0 : pas de TP, sortie à exit_time
    fade: bool = False
    enabled: bool = True  # false : gardée pour mémoire, ni backtestée par défaut ni tradée

    @field_validator("zone")
    @classmethod
    def _valid_zone(cls, value: str) -> str:
        return _check_timezone(value)

    @field_validator("signal_time", "exit_time", mode="before")
    @classmethod
    def _quoted_time(cls, value: object) -> object:
        return _require_quoted_time(value)

    @model_validator(mode="after")
    def _ordered(self) -> SessionMomentumConfig:
        if not self.signal_time < self.exit_time:
            raise ValueError("il faut signal_time < exit_time")
        return self


class ScalpBreakoutConfig(_Section):
    """Stratégie « cassure » : cassure des 60 s précédentes confirmée, dans le sens de la tendance M1."""

    enabled: bool
    version: int = Field(ge=1)
    lookback_s: int = Field(ge=10)
    min_window_candles: int = Field(ge=1)
    confirm_closes: int = Field(ge=1, le=10)
    ema_fast: int = Field(ge=2)
    ema_slow: int = Field(ge=3)
    stop_lookback_s: int = Field(ge=5)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpBreakoutConfig:
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast doit être inférieur à ema_slow")
        return self


class ScalpPullbackConfig(_Section):
    """Stratégie « impulsion-repli » : impulsion, repli partiel, puis reprise dans le sens de l'impulsion."""

    enabled: bool
    version: int = Field(ge=1)
    impulse_atr: float = Field(gt=0)
    impulse_max_s: int = Field(ge=5)
    retrace_min: float = Field(gt=0, lt=1)
    retrace_max: float = Field(gt=0, le=1)
    pullback_max_s: int = Field(ge=5)
    atr_period: int = Field(ge=2)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpPullbackConfig:
        if self.retrace_min >= self.retrace_max:
            raise ValueError("retrace_min doit être inférieur à retrace_max")
        return self


class ScalpTwoCandleConfig(_Section):
    """Stratégie « deux bougies » (idée de l'utilisateur, 29/09) : une bougie dans un sens, une bougie contraire, puis
    entrée dans le sens de la première ; stop au-delà des deux bougies ; une position par objectif (en pips)."""

    enabled: bool = False
    version: int = Field(default=1, ge=1)
    minutes: int = Field(default=1, ge=1, le=60)  # durée des bougies lues
    min_stop_pips: float = Field(default=10.0, gt=0)  # stop plus proche : élargi à ce minimum (pas de refus)
    target_pips: list[float] = Field(default_factory=lambda: [20.0, 25.0, 30.0], min_length=1, max_length=5)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpTwoCandleConfig:
        if any(t <= 0 for t in self.target_pips) or self.target_pips != sorted(self.target_pips):
            raise ValueError("two_candles.target_pips : objectifs positifs, du plus proche au plus loin")
        return self


class ScalpLearningConfig(_Section):
    """Apprentissage à chaque trade : choix de la variante (sortie, sens) d'après les derniers résultats."""

    enabled: bool
    version: int = Field(ge=1)
    by_side: bool  # scores séparés pour les signaux d'achat et de vente
    allow_invert: bool  # le bot peut jouer un signal à l'envers
    half_life_trades: float = Field(gt=0)  # les trades récents pèsent plus : poids divisé par 2 tous les N trades
    min_trades: int = Field(ge=1)  # trades simulés avant de juger une variante
    always_trade: bool  # False : pas de trade quand toutes les variantes perdent en ce moment
    stop_mults: list[float] = Field(min_length=1)
    target_ratios: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpLearningConfig:
        if any(m < 1 for m in self.stop_mults) or any(t <= 0 for t in self.target_ratios):
            raise ValueError("stop_mults >= 1 (jamais un stop plus proche que la structure) et target_ratios > 0")
        return self


class ScalpCadenceConfig(_Section):
    """Cadence liée au bénéfice : plus de trades par minute seulement tant que les derniers trades rapportent."""

    enabled: bool
    version: int = Field(ge=1)
    window_trades: int = Field(ge=5)  # jugée sur les N derniers trades fermés, frais compris
    multipliers: list[float] = Field(min_length=1)  # niveau k : limites de base x multipliers[k] ; niveau 0 = x1
    # Plafonds absolus, quel que soit le niveau : requêtes au serveur (TOO_MANY_REQUESTS), règles d'Axi (pas de
    # HFT), bug qui enverrait des ordres en rafale ; risque ouvert au plus égal à la perte journalière maximale.
    ceiling_entries_per_minute: int = Field(ge=1, le=60)
    ceiling_total_risk_pct: float = Field(gt=0)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpCadenceConfig:
        if self.multipliers[0] != 1 or any(b <= a for a, b in zip(self.multipliers, self.multipliers[1:])):
            raise ValueError("multipliers : commence à 1 (limites de base), puis strictement croissant")
        return self


class ScalpMarketReadConfig(_Section):
    """Lecture du marché : sens achat / vente relu à chaque barre M1 close (scalping/market_read.py)."""

    enabled: bool = False
    model: str = ""  # nom du modèle dans market_read.MODELS
    version: int = Field(default=1, ge=1)
    params: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpMarketReadConfig:
        if self.enabled and not self.model:
            raise ValueError("market_read : nom du modèle requis quand la lecture est active")
        return self


class ScalpingConfig(_Section):
    """Expérience de scalping (démo uniquement), séparée de la stratégie principale."""

    magic: int = Field(gt=0, lt=2**63)
    candle_seconds: int = Field(ge=1, le=60)
    # Sorties, communes aux stratégies (comparaison à risque et sorties égaux).
    stop_buffer_points: float = Field(ge=0)
    min_stop_points: float = Field(gt=0)
    # Pips (demande de l'utilisateur : « compter en pips ») : 1 pip = pip_size en prix (or à 2 décimales : 0,10 $).
    pip_size: float = Field(default=0.1, gt=0)
    # Stop fixe en pips depuis l'entrée (ex. vente à 4000, 30 pips -> stop à 4003) au lieu du stop derrière la
    # structure du signal ; objectif fixe en pips, sinon target_ratio x le stop. None : stop derrière la structure.
    fixed_stop_pips: float | None = Field(default=None, gt=0)
    fixed_target_pips: float | None = Field(default=None, gt=0)
    # Sens autorisé (« toujours dans le bon sens ») : trades seulement dans le sens du mouvement du jour (prix -
    # ouverture du jour de cotation) quand il atteint direction_min_move_atr x l'ATR journalier ; aucun sinon.
    direction_filter: bool = False
    direction_min_move_atr: float = Field(default=0.5, gt=0)
    direction_atr_days: int = Field(default=14, ge=2)
    max_stop_points: float = Field(gt=0)
    target_ratio: float = Field(gt=0)
    min_target_cost_ratio: float = Field(ge=0)
    expected_slippage_points: float = Field(ge=0)
    extra_slippage_points: float = Field(ge=0)
    max_hold_s: int = Field(ge=5)
    # Compte.
    risk_per_trade_pct: float = Field(gt=0, le=RISK_PER_TRADE_HARD_CAP_PCT)
    # Lot fixe (choix de l'utilisateur) : risk_per_trade_pct devient le risque MAXIMAL d'un trade (trade refusé
    # au-delà, jamais de lot réduit en douce). None : lot calculé d'après risk_per_trade_pct (arrondi vers le bas).
    fixed_volume: float | None = Field(default=None, gt=0)
    max_open_positions: int = Field(ge=1)
    max_total_risk_pct: float = Field(gt=0)
    # Une entrée par bougie de 5 s au plus, soit 12 par minute (l'utilisateur a levé sa limite de 5 le 29/09).
    max_entries_per_minute: int = Field(ge=1, le=12)
    min_seconds_between_entries: float = Field(ge=0)
    daily_loss_pct: float = Field(gt=0)
    # Baisse maximale depuis le plus haut de l'expérience : arrêt total, relance manuelle (CLAUDE.md règle 5).
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=10.0)
    min_free_margin_pct: float = Field(ge=0, lt=100)
    experiment_days: int = Field(ge=1)
    # Corrections tirées des trades démo (29/09/2026) ; défauts = sans effet.
    max_same_side_positions: int | None = Field(default=None, ge=1)  # trades ouverts dans le même sens, au plus
    quick_stop_s: float = Field(default=20.0, gt=0)  # stop touché plus vite que ça : entrée prise dans le bruit
    quick_stop_pause_s: float = Field(default=0.0, ge=0)  # pause des entrées dans ce sens après un tel stop
    loss_streak: int = Field(default=3, ge=1)  # pertes d'affilée dans un même sens...
    loss_streak_pause_s: float = Field(default=0.0, ge=0)  # ...puis pause des entrées dans ce sens (0 : jamais)
    breakout: ScalpBreakoutConfig
    pullback: ScalpPullbackConfig
    learning: ScalpLearningConfig
    cadence: ScalpCadenceConfig
    # Sens relu à chaque minute d'après la réaction des bougies (remplace le sens du jour, direction_filter).
    market_read: ScalpMarketReadConfig = Field(default_factory=ScalpMarketReadConfig)
    two_candles: ScalpTwoCandleConfig = Field(default_factory=ScalpTwoCandleConfig)

    @model_validator(mode="after")
    def _consistent(self) -> ScalpingConfig:
        if self.direction_filter and self.market_read.enabled:
            raise ValueError("choisir un seul sens : direction_filter (sens du jour) ou market_read (lecture du marché)")
        if self.min_stop_points >= self.max_stop_points:
            raise ValueError("min_stop_points doit être inférieur à max_stop_points")
        if self.fixed_stop_pips is not None:
            distance = self.fixed_stop_pips * self.pip_size
            if not self.min_stop_points * 0.01 - 1e-9 <= distance <= self.max_stop_points * 0.01 + 1e-9:
                raise ValueError("fixed_stop_pips hors de [min_stop_points, max_stop_points]")
        if self.max_total_risk_pct < self.risk_per_trade_pct:
            raise ValueError("max_total_risk_pct doit couvrir au moins un trade")
        if self.max_entries_per_minute > self.cadence.ceiling_entries_per_minute:
            raise ValueError("max_entries_per_minute dépasse le plafond technique de la cadence")
        ceiling = self.cadence.ceiling_total_risk_pct
        if not self.max_total_risk_pct <= ceiling <= self.daily_loss_pct:
            raise ValueError("cadence : plafond du risque ouvert entre max_total_risk_pct et la perte journalière")
        if not (self.breakout.enabled or self.pullback.enabled or self.two_candles.enabled):
            raise ValueError("au moins une stratégie de scalping doit être active")
        if self.two_candles.min_stop_pips * self.pip_size > self.max_stop_points * 0.01 + 1e-9:
            raise ValueError("two_candles.min_stop_pips au-delà de max_stop_points")
        return self


class StrategiesConfig(_Section):
    asian_breakout: AsianBreakoutConfig
    # Portefeuille retenu (docs/STRATEGIES.md) : une stratégie par session.
    session_momentum: dict[str, SessionMomentumConfig]


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
    backtest: BacktestConfig
    strategies: StrategiesConfig
    scalping: ScalpingConfig
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
            f"{env_path} est enregistré en UTF-16 : dans le Bloc-notes, Fichier > Enregistrer sous, encodage UTF-8"
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
            "MT5_PATH contient un caractère invisible : écris le chemin sans guillemets ou entre guillemets simples"
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
