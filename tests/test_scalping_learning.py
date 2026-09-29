"""Apprentissage à chaque trade : variantes, scores récents, abstention, signal joué à l'envers."""

import pytest

from goldbot.config import load_settings
from goldbot.scalping.engine import BREAKOUT, LONG, SHORT, Candle, Plan, Setup
from goldbot.scalping.learning import (
    BASE_VARIANT,
    LearningBook,
    TradeLearner,
    Variant,
    context_key,
    variant_grid,
    variant_plan,
)
from tests.conftest import CONFIG_PATH

CONFIG = load_settings(CONFIG_PATH).scalping
BASE = Plan(LONG, 4000.58, 3999.87, 4001.43, 0.71, 0.85, 0.26, 0.16)  # achat : ask 4000.58, bid 4000.42


def _plan(variant):
    return variant_plan(BASE, variant, CONFIG, point=0.01, bid=4000.42, ask=4000.58)


def test_base_variant_is_the_v1_plan():
    assert _plan(BASE_VARIANT) == BASE


def test_wider_stop_and_other_target():
    plan = _plan(Variant(2.0, 0.8))
    assert plan.side == LONG and plan.entry == 4000.58
    assert plan.stop_distance == pytest.approx(1.42) and plan.sl == pytest.approx(3999.16)
    assert plan.target == pytest.approx(1.14) and plan.tp == pytest.approx(4001.72)


def test_inverted_variant_sells_at_the_bid_with_its_stop_above():
    plan = _plan(Variant(1.0, 1.2, invert=True))
    assert plan.side == SHORT and plan.entry == 4000.42
    assert plan.sl == pytest.approx(4001.13) and plan.tp == pytest.approx(3999.57)


def test_a_variant_is_impossible_when_its_stop_is_too_far_or_its_target_too_small():
    far = Plan(LONG, 4000.58, 3996.58, 4005.38, 4.00, 4.80, 0.26, 0.16)
    assert variant_plan(far, Variant(2.0, 1.2), CONFIG, point=0.01, bid=4000.42, ask=4000.58) is None  # 8 $ > 6 $
    small = Plan(LONG, 4000.58, 4000.00, 4001.28, 0.58, 0.70, 0.26, 0.16)
    assert variant_plan(small, Variant(1.0, 0.8), CONFIG, point=0.01, bid=4000.42, ask=4000.58) is None  # 0,46 < 0,78


def test_grid_and_context_keys():
    grid = variant_grid((1.0, 2.0), (1.2,), (False, True))
    assert [v.name for v in grid] == [
        "stop x1 / objectif 1.2 R",
        "stop x2 / objectif 1.2 R",
        "stop x1 / objectif 1.2 R / inversé",
        "stop x2 / objectif 1.2 R / inversé",
    ]
    assert context_key(BREAKOUT, LONG, True) == "B+" and context_key(BREAKOUT, SHORT, True) == "B-"
    assert context_key(BREAKOUT, SHORT, False) == "B"


def _learner(**kwargs):
    variants = variant_grid((1.0, 2.0), (1.2,), (False, True))
    return TradeLearner(variants, **{"half_life": 10, "min_trades": 3, "always_trade": False, **kwargs})


def test_learner_starts_with_the_base_rules_then_plays_the_best_recent_variant():
    learner = _learner()
    names = set(learner.variants)
    variant, why = learner.choose("B+", names)
    assert variant == BASE_VARIANT and "règles de départ" in why
    for _ in range(3):
        learner.update("B+", "stop x1 / objectif 1.2 R", -1.0)
        learner.update("B+", "stop x1 / objectif 1.2 R / inversé", 0.8)
    variant, why = learner.choose("B+", names)
    assert variant.invert and "meilleure variante récente" in why
    assert learner.choose("B-", names)[0] == BASE_VARIANT  # achats et ventes appris séparément


def test_learner_abstains_when_every_variant_loses_unless_told_to_always_trade():
    for always in (False, True):
        learner = _learner(always_trade=always)
        for _ in range(3):
            learner.update("B+", "stop x1 / objectif 1.2 R", -1.0)
            learner.update("B+", "stop x2 / objectif 1.2 R", -0.2)
        variant, why = learner.choose("B+", set(learner.variants))
        if always:
            assert variant == Variant(2.0, 1.2)  # la moins mauvaise
        else:
            assert variant is None and "toutes les variantes perdent" in why


def test_recent_trades_weigh_more_than_old_ones():
    learner = _learner(half_life=2)
    for _ in range(10):
        learner.update("B+", "stop x1 / objectif 1.2 R", -1.0)
    for _ in range(3):
        learner.update("B+", "stop x1 / objectif 1.2 R", 1.0)
    assert learner.scores("B+")["stop x1 / objectif 1.2 R"] > 0  # trois gains récents l'emportent sur dix pertes


def test_learner_state_round_trips():
    learner = _learner()
    learner.update("P-", "stop x2 / objectif 1.2 R", 0.5)
    other = _learner()
    other.load(learner.to_dict())
    assert other.state == learner.state


def test_learning_book_simulates_every_variant_and_learns_when_they_close():
    book = LearningBook(CONFIG, point=0.01, digits=2, fee_per_oz=0.0)
    candle = Candle(0, 4000.40, 4000.50, 4000.40, 4000.50, 4000.42, 4000.58, 0.16, 5)
    setup = Setup(BREAKOUT, LONG, 4000.20, 3999.95, candle, key="B+1@4000.20", reason="test")
    plan, name, why = book.decide(setup, BASE, 4000.42, 4000.58, 5_000)
    assert plan == BASE and name == BASE_VARIANT.name and "règles de départ" in why
    # 18 variantes, sauf l'objectif de 0,8 R au stop x1 (0,57 $ < 3 x 0,26 $ de coût), dans les deux sens.
    assert len(book.sims) == 16
    updates = book.on_tick(5_000 + 121_000, 4000.42, 4000.58)  # durée maximale atteinte, prix inchangé
    assert len(updates) == len(set(book.learner.state["B+"])) == 16 and not book.sims
    assert all(r < 0 for _, _, r in updates)  # prix inchangé : chaque variante perd le spread
