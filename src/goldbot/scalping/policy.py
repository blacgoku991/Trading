"""Règles d'entrée communes au rejeu et au bot démo : mêmes entrées, même décision.

Trois cas à ne pas confondre :
- nouveau signal : une occasion distincte (clé d'occasion différente de celles des trades ouverts) ;
  il compte dans les limites (positions, risque cumulé, entrées par minute, délai entre entrées) ;
- fractionnement : un même trade envoyé en plusieurs ordres (volume au-delà du maximum par ordre, ou
  exécution partielle). Les ordres partagent l'identifiant du signal (#1, #2…) et son budget de risque ;
  ils comptent pour une seule entrée ;
- doublon accidentel : le même signal (même identifiant) ou la même occasion (même clé) rencontré une
  deuxième fois. Jamais de deuxième ordre : il est compté, pas envoyé.

Cadence liée au bénéfice (Cadence) : les limites de positions, de risque cumulé et d'entrées par minute sont
relevées par paliers tant que les derniers trades fermés sont en bénéfice net, et reviennent à la base dès qu'ils
sont en perte. Le risque de chaque trade ne change jamais.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from goldbot.config import ScalpingConfig
from goldbot.risk.sizing import position_size


@dataclass(frozen=True)
class Exposure:
    """Trade ouvert connu des règles d'entrée (envoyé au broker ou simulé)."""

    tag: str
    key: str
    side: int
    risk: float  # perte au stop, dans la devise du compte (tous ses ordres)


@dataclass(frozen=True)
class Limits:
    entries_per_minute: int
    seconds_between_entries: float
    open_positions: int
    total_risk_pct: float


class Cadence:
    """Cadence liée au bénéfice (demande de l'utilisateur : « plus de trades tant qu'il génère du bénéfice »).

    Après chaque trade fermé, résultat net frais compris : si les `window_trades` derniers trades sont en bénéfice
    net et qu'au moins `window_trades` trades ont fermé depuis le dernier changement, la cadence monte d'un palier
    (limites de base x multipliers[k]) ; dès qu'ils sont en perte nette, retour immédiat à la base. Monter
    lentement, redescendre vite. Plafonds absolus : entrées par minute et risque ouvert (au plus la perte
    journalière maximale). Le risque par trade ne change pas : ce n'est ni une martingale ni une logique de
    récupération (CLAUDE.md règle 4), l'activité augmente après des gains, jamais après des pertes.
    Même code dans le rejeu et le bot démo ; l'état se reconstruit à partir des trades fermés (rejouables).
    """

    def __init__(self, config: ScalpingConfig) -> None:
        self.cfg = config
        self.results: deque[float] = deque(maxlen=config.cadence.window_trades)
        self.level = 0
        self.since_change = 0

    @property
    def enabled(self) -> bool:
        return self.cfg.cadence.enabled

    def limits(self, level: int | None = None) -> Limits:
        cfg, cadence = self.cfg, self.cfg.cadence
        level = self.level if level is None else level
        factor = cadence.multipliers[level] if self.enabled else 1.0
        return Limits(
            # Au plus une entrée par bougie : les signaux d'une même bougie sont décidés au même instant.
            entries_per_minute=min(
                int(cfg.max_entries_per_minute * factor), cadence.ceiling_entries_per_minute, 60 // cfg.candle_seconds
            ),
            seconds_between_entries=cfg.min_seconds_between_entries / factor,
            open_positions=int(cfg.max_open_positions * factor),
            total_risk_pct=min(cfg.max_total_risk_pct * factor, cadence.ceiling_total_risk_pct),
        )

    def top_limits(self) -> Limits:
        return self.limits(len(self.cfg.cadence.multipliers) - 1)

    def recent_result(self) -> float:
        return sum(self.results)

    def on_close(self, pnl: float) -> str | None:
        """Enregistre un trade fermé (résultat net) ; renvoie un message si le palier change."""
        self.results.append(pnl)
        self.since_change += 1
        if not self.enabled or len(self.results) < self.results.maxlen:
            return None
        total, window = self.recent_result(), self.results.maxlen
        if total <= 0:
            # En perte : base, et il faudra une nouvelle série complète de trades en bénéfice pour remonter.
            self.since_change = 0
            if self.level > 0:
                self.level = 0
                return f"cadence : retour à la base, les {window} derniers trades perdent ({total:+.2f})"
            return None
        top = len(self.cfg.cadence.multipliers) - 1
        if pnl > 0 and self.level < top and self.since_change >= window:  # jamais sur un trade perdant
            self.level, self.since_change = self.level + 1, 0
            return f"cadence : palier {self.level} (x{self.cfg.cadence.multipliers[self.level]:g}), les {window} " \
                   f"derniers trades gagnent ({total:+.2f})"  # fmt: skip
        return None

    def rebuild(self, results: list[float]) -> None:
        """Reconstruit l'état à partir des résultats des trades fermés, dans l'ordre (reprise après redémarrage)."""
        self.results.clear()
        self.level, self.since_change = 0, 0
        for pnl in results:
            self.on_close(pnl)

    def describe(self) -> str:
        limits = self.limits()
        state = f"palier {self.level}" if self.enabled else "désactivée"
        recent = f"{len(self.results)} derniers trades {self.recent_result():+.2f}" if self.results else "aucun trade"
        return (
            f"cadence {state} : {limits.entries_per_minute} entrées/min, {limits.open_positions} positions, "
            f"risque ouvert {limits.total_risk_pct:g} % ({recent})"
        )


class EntryPolicy:
    def __init__(self, config: ScalpingConfig) -> None:
        self.cadence = Cadence(config)
        self.entries: deque[int] = deque()  # heures (ms) des entrées acceptées, 60 dernières secondes
        self.paused_until: dict[int, int] = {}  # sens -> fin de la pause (stop touché trop vite, pertes d'affilée)
        self.pause_reason: dict[int, str] = {}
        self.streak: dict[int, int] = {}  # sens -> pertes d'affilée

    @property
    def cfg(self) -> ScalpingConfig:
        return self.cadence.cfg

    @cfg.setter
    def cfg(self, config: ScalpingConfig) -> None:
        self.cadence.cfg = config  # une seule config pour les limites de base et la cadence

    def refusal(
        self,
        now_ms: int,
        *,
        key: str,
        risk: float,
        equity: float,
        day_result: float,
        day_start_equity: float,
        open_trades: list[Exposure],
        side: int | None = None,
        day_realized: float | None = None,
        direction: int | None = None,
    ) -> str | None:
        """Motif du refus, ou None si l'entrée est permise."""
        cfg = self.cfg
        limits = self.cadence.limits()
        if day_result <= -cfg.daily_loss_pct / 100 * day_start_equity:
            return f"perte du jour atteinte : {day_result:+.2f}, limite -{cfg.daily_loss_pct:g} %"
        if any(trade.key == key for trade in open_trades):
            return "même occasion déjà en position : doublon évité"
        if len(open_trades) >= limits.open_positions:
            return f"positions ouvertes au maximum : {limits.open_positions}"
        if cfg.market_read.enabled and side is not None:
            if not direction:
                return "lecture du marché : pas de sens clair en ce moment"
            if side != direction:
                return f"lecture du marché : marché {'acheteur' if direction > 0 else 'vendeur'} en ce moment"
        elif cfg.direction_filter and side is not None:
            if not direction:
                return "sens : pas de mouvement net du jour (moins de " \
                       f"{cfg.direction_min_move_atr:g} ATR depuis l'ouverture)"  # fmt: skip
            if side != direction:
                return f"sens : contre le mouvement du jour ({'hausse' if direction > 0 else 'baisse'})"
        if side is not None and cfg.max_same_side_positions is not None:
            same = sum(1 for trade in open_trades if trade.side == side)
            if same >= cfg.max_same_side_positions:
                return f"trades ouverts dans ce sens au maximum : {cfg.max_same_side_positions}"
        if side is not None and now_ms < self.paused_until.get(side, -1):
            left = (self.paused_until[side] - now_ms) / 1000
            return f"pause dans ce sens ({self.pause_reason[side]}) : encore {left:.0f} s"
        open_risk = sum(trade.risk for trade in open_trades)
        if open_risk + risk > limits.total_risk_pct / 100 * equity:
            return f"risque cumulé au maximum : {limits.total_risk_pct:g} % de l'equity"
        # Si tous les stops sont touchés, la journée finit à « réalisé - risque ouvert » : ce total ne doit pas
        # dépasser la perte maximale du jour. Réalisé seulement (un gain latent peut disparaître avant les stops).
        realized = day_result if day_realized is None else day_realized
        budget = cfg.daily_loss_pct / 100 * day_start_equity + realized
        if open_risk + risk > budget:
            return f"budget de perte du jour : {budget:.2f} restants, {open_risk:.2f} déjà en jeu"
        while self.entries and self.entries[0] <= now_ms - 60_000:
            self.entries.popleft()
        if len(self.entries) >= limits.entries_per_minute:
            return f"entrées par minute au maximum : {limits.entries_per_minute} sur 60 s"
        if self.entries and now_ms - self.entries[-1] < limits.seconds_between_entries * 1000:
            return f"délai entre deux entrées : moins de {limits.seconds_between_entries:g} s"
        return None

    def accept(self, now_ms: int) -> None:
        self.entries.append(now_ms)

    def on_exit(self, side: int, reason: str, open_ms: int, exit_ms: int, pnl: float) -> str | None:
        """Après chaque trade fermé : pauses dans un sens qui se trompe (si réglées).

        - stop touché très vite : l'entrée était prise dans le bruit ;
        - loss_streak pertes d'affilée dans ce sens : le marché ne va pas dans ce sens en ce moment.
        Renvoie un message quand une pause commence.
        """
        cfg = self.cfg
        message = None
        self.streak[side] = self.streak.get(side, 0) + 1 if pnl < 0 else 0
        if reason == "stop" and cfg.quick_stop_pause_s > 0 and exit_ms - open_ms < cfg.quick_stop_s * 1000:
            message = self._pause(side, exit_ms, cfg.quick_stop_pause_s,
                                  f"stop touché en moins de {cfg.quick_stop_s:g} s")  # fmt: skip
        if cfg.loss_streak_pause_s > 0 and self.streak[side] >= cfg.loss_streak:
            message = self._pause(side, exit_ms, cfg.loss_streak_pause_s, f"{self.streak[side]} pertes d'affilée")
            self.streak[side] = 0
        return message

    def _pause(self, side: int, from_ms: int, seconds: float, why: str) -> str | None:
        until_ms = from_ms + int(seconds * 1000)
        if until_ms <= self.paused_until.get(side, 0):
            return None
        self.paused_until[side] = until_ms
        self.pause_reason[side] = why
        direction = "achats" if side > 0 else "ventes"
        return f"pause des {direction} pendant {seconds / 60:g} min ({why}) : le marché ne va pas dans ce sens"


def trade_volume(config: ScalpingConfig, equity: float, loss_per_lot: float, *, volume_min: float,
                 volume_step: float) -> tuple[float, str | None]:  # fmt: skip
    """(lot, motif du refus) ; même calcul au rejeu et en démo.

    Lot fixe : refusé si sa perte au stop dépasse risk_per_trade_pct de l'equity (plafond dur 1 %). Sinon : lot
    calculé d'après le risque, arrondi vers le bas, refusé sous le lot minimal (jamais arrondi vers le haut).
    """
    budget = equity * config.risk_per_trade_pct / 100
    if config.lot_choices:
        for lot in sorted(config.lot_choices, reverse=True):
            volume = round(int(lot / volume_step + 1e-9) * volume_step, 8)
            if volume >= volume_min and volume * loss_per_lot <= budget:
                return volume, None
        smallest = min(config.lot_choices)
        return 0.0, (f"lot {smallest:g} au-dessus du risque maximal : perd {smallest * loss_per_lot:.2f} au stop, plus "
                     f"que {config.risk_per_trade_pct:g} % ({budget:.2f})")  # fmt: skip
    if config.fixed_volume is not None:
        volume = round(int(config.fixed_volume / volume_step + 1e-9) * volume_step, 8)
        if volume < volume_min:
            return 0.0, f"lot fixe sous le minimum du broker : {config.fixed_volume:g} < {volume_min:g}"
        if volume * loss_per_lot > budget:
            return 0.0, (f"lot fixe au-dessus du risque maximal : {volume:g} lot perd {volume * loss_per_lot:.2f} au "
                         f"stop, plus que {config.risk_per_trade_pct:g} % ({budget:.2f})")  # fmt: skip
        return volume, None
    volume = position_size(budget, loss_per_lot, volume_min=volume_min, volume_max=float("inf"),
                           volume_step=volume_step)  # fmt: skip
    return volume, None if volume > 0 else "lot minimum au-dessus du budget de risque"


def drawdown_pct(equity: float, peak: float) -> float:
    """Baisse depuis le plus haut, en % (positive)."""
    return (1.0 - equity / peak) * 100.0 if peak > 0 else 0.0


def split_volume(volume: float, volume_max: float, volume_step: float) -> list[float]:
    """Fractionnement d'un trade en ordres d'au plus volume_max lots : parts égales, multiples du pas."""
    steps = round(volume / volume_step)
    if steps <= 0:
        return []
    max_steps = max(1, int(volume_max / volume_step + 1e-9))
    count = -(-steps // max_steps)
    base, extra = divmod(steps, count)
    return [round((base + (1 if i < extra else 0)) * volume_step, 8) for i in range(count)]


def ladder_volumes(volume: float, count: int, volume_min: float, volume_step: float) -> list[float]:
    """Lot réparti en `count` positions (une par objectif), parts égales en pas de volume, chacune au moins au lot
    minimal : moins de positions si le lot ne suffit pas (les objectifs les plus proches sont gardés)."""
    steps = round(volume / volume_step)
    count = min(count, int(volume / volume_min + 1e-9))
    if steps <= 0 or count <= 0:
        return []
    base, extra = divmod(steps, count)
    return [round((base + (1 if i < extra else 0)) * volume_step, 8) for i in range(count)]


def reason_key(message: str) -> str:
    """« stop trop loin : 7.10 $ > 6.00 $ » -> « stop trop loin » (pour compter les refus par motif)."""
    return message.split(" :")[0]
