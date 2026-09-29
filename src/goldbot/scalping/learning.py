"""Apprentissage à chaque trade : le bot corrige sa façon de jouer d'après ses derniers résultats.

Chaque signal est joué, en simulation, avec plusieurs variantes (stop plus ou moins large, objectif plus ou
moins loin, et même le sens inverse : vendre quand le signal dit d'acheter), que la variante ait été jouée en
vrai ou non. Après chaque trade simulé fermé, le score de la variante est mis à jour : moyenne de ses résultats
en R, les trades récents pesant plus (demi-vie en nombre de trades). Les scores peuvent être tenus séparément
pour les signaux d'achat et de vente : si les achats perdent en ce moment et que les ventes gagnent, le bot
s'abstient d'acheter, ou retourne le signal. Au signal suivant, il prend la variante qui marche le mieux en ce
moment ; si toutes perdent, il s'abstient (sauf en mode « toujours trader »).

Le risque par trade ne change jamais : ce n'est pas une logique de récupération des pertes (CLAUDE.md règle 4),
seulement le choix de la sortie, du sens et du « trader ou pas ». Même code dans le rejeu et dans le bot démo,
sans hasard : rejouable et vérifiable.
"""

from __future__ import annotations

from dataclasses import dataclass

from goldbot.config import ScalpingConfig
from goldbot.scalping.engine import LONG, Plan, Setup, SimTrade


@dataclass(frozen=True)
class Variant:
    stop_mult: float  # distance du stop = distance de base (structure du signal) x stop_mult
    target_ratio: float  # objectif = target_ratio x distance du stop
    invert: bool = False  # jouer le signal à l'envers (vendre au lieu d'acheter), même distance de stop

    @property
    def name(self) -> str:
        text = f"stop x{self.stop_mult:g} / objectif {self.target_ratio:g} R"
        return f"{text} / inversé" if self.invert else text


BASE_VARIANT = Variant(1.0, 1.2)  # règles v1


def variant_grid(stop_mults: tuple[float, ...], target_ratios: tuple[float, ...],
                 inverts: tuple[bool, ...] = (False,)) -> list[Variant]:  # fmt: skip
    return [Variant(m, t, inv) for inv in inverts for m in stop_mults for t in target_ratios]


def variant_plan(plan: Plan, variant: Variant, config: ScalpingConfig, *, point: float, bid: float, ask: float,
                 digits: int = 2) -> Plan | None:  # fmt: skip
    """Plan de la variante ; None si son stop est trop loin ou son objectif trop faible face au coût.

    Inversée : l'entrée se fait au prix disponible dans l'autre sens (bid pour vendre, ask pour acheter).
    """
    side = -plan.side if variant.invert else plan.side
    entry = (ask if side == LONG else bid) if variant.invert else plan.entry
    distance = round(plan.stop_distance * variant.stop_mult, digits)
    if distance > config.max_stop_points * point:
        return None
    target = round(variant.target_ratio * distance, digits)
    if target < config.min_target_cost_ratio * plan.cost:
        return None
    sl = round(entry - side * distance, digits)
    tp = round(entry + side * target, digits)
    return Plan(side, entry, sl, tp, distance, target, plan.cost, plan.spread)


def context_key(strategy: str, side: int, by_side: bool) -> str:
    """Contexte d'apprentissage : la stratégie, et le sens du signal si les scores sont séparés."""
    return strategy + (("+" if side == LONG else "-") if by_side else "")


class TradeLearner:
    """Score de chaque variante, par contexte (stratégie, ou stratégie et sens), mis à jour après chaque trade."""

    def __init__(self, variants: list[Variant], *, half_life: float, min_trades: int, always_trade: bool,
                 default: Variant = BASE_VARIANT) -> None:  # fmt: skip
        self.variants = {v.name: v for v in variants}
        self.half_life = half_life
        self.decay = 0.5 ** (1.0 / half_life)
        self.min_trades = min_trades
        self.always_trade = always_trade
        self.default = default
        # contexte -> variante -> [somme pondérée des R, somme des poids, nombre de trades]
        self.state: dict[str, dict[str, list[float]]] = {}

    def update(self, key: str, name: str, r: float) -> None:
        score = self.state.setdefault(key, {}).setdefault(name, [0.0, 0.0, 0])
        score[0] = score[0] * self.decay + r
        score[1] = score[1] * self.decay + 1.0
        score[2] += 1

    def scores(self, key: str) -> dict[str, float]:
        """Moyenne récente (R) des variantes qui ont assez de trades pour être jugées."""
        arms = self.state.get(key, {})
        return {name: s[0] / s[1] for name, s in arms.items() if s[2] >= self.min_trades}

    def best(self, key: str) -> tuple[str, float] | None:
        scores = self.scores(key)
        if not scores:
            return None
        name = max(scores, key=scores.get)
        return name, scores[name]

    def choose(self, key: str, allowed: set[str]) -> tuple[Variant | None, str]:
        """Variante à jouer pour ce signal (None : ne pas trader) et l'explication du choix."""
        ready = {name: value for name, value in self.scores(key).items() if name in allowed}
        if not ready:
            if self.default.name in allowed:
                return self.default, "pas encore assez de trades pour juger : règles de départ"
            return None, "variante de départ impossible pour ce signal"
        best = max(ready, key=ready.get)
        text = f"meilleure variante récente {best} ({ready[best]:+.2f} R en moyenne)"
        if ready[best] <= 0 and not self.always_trade:
            return None, f"toutes les variantes perdent en ce moment ({text})"
        return self.variants[best], text

    def to_dict(self) -> dict[str, object]:
        return {"state": self.state}

    def load(self, data: dict[str, object]) -> None:
        self.state = {k: {n: list(v) for n, v in arms.items()} for k, arms in dict(data.get("state", {})).items()}


class LearningBook:
    """Trades simulés de toutes les variantes de chaque signal, pour apprendre (rejeu et bot démo)."""

    def __init__(self, config: ScalpingConfig, *, point: float, digits: int, fee_per_oz: float) -> None:
        cfg = config.learning
        inverts = (False, True) if cfg.allow_invert else (False,)
        variants = variant_grid(tuple(cfg.stop_mults), tuple(cfg.target_ratios), inverts)
        default = Variant(1.0, config.target_ratio)
        self.learner = TradeLearner(variants, half_life=cfg.half_life_trades, min_trades=cfg.min_trades,
                                    always_trade=cfg.always_trade, default=default)  # fmt: skip
        self.config = config
        self.by_side = cfg.by_side
        self.point, self.digits, self.fee = point, digits, fee_per_oz
        self.sims: list[tuple[SimTrade, str, str, float]] = []  # (trade simulé, contexte, variante, distance du stop)

    def decide(self, setup: Setup, plan: Plan, bid: float, ask: float, now_ms: int) -> tuple[Plan | None, str, str]:
        """(plan à jouer ou None, variante, explication) ; lance aussi les trades simulés de toutes les variantes."""
        key = context_key(setup.strategy, setup.side, self.by_side)
        plans = {}
        for variant in self.learner.variants.values():
            chosen = variant_plan(plan, variant, self.config, point=self.point, bid=bid, ask=ask, digits=self.digits)
            if chosen is not None:
                plans[variant.name] = chosen
        for name, chosen in plans.items():
            trade = SimTrade.open(setup.tag, chosen, bid, ask, now_ms, self.config.max_hold_s, 0.0, 1.0, self.fee)
            self.sims.append((trade, key, name, chosen.stop_distance))
        variant, why = self.learner.choose(key, set(plans))
        if variant is None:
            return None, "", why
        return plans[variant.name], variant.name, why

    def on_tick(self, time_ms: int, bid: float, ask: float) -> list[tuple[str, str, float]]:
        """Avance les trades simulés ; chaque trade fermé met à jour le score de sa variante. Renvoie ces mises à jour."""
        updates, still = [], []
        for trade, key, name, distance in self.sims:
            if trade.on_tick(time_ms, bid, ask):
                r = trade.move / distance
                self.learner.update(key, name, r)
                updates.append((key, name, r))
            else:
                still.append((trade, key, name, distance))
        self.sims = still
        return updates
