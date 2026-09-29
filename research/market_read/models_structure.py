# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Lectures du marché par la STRUCTURE (swings M1 / M5, cassures, balayages, déséquilibres).

Sortie de chaque modèle, à la clôture de chaque barre M1 : +1 (lecture acheteuse), -1 (vendeuse), 0 (pas de
lecture claire). Une lecture dure au plus `hold` barres (barre du déclenchement comprise ; un nouveau
déclencheur dans le même sens relance le compte) ; elle s'éteint plus tôt si les
bougies réagissent contre elle (clôture au-delà du niveau qui la protège) et bascule tout de suite dans l'autre
sens si le déclencheur opposé apparaît. Les règles de vente sont le miroir exact des règles d'achat (le code
calcule la vente en appliquant la même fonction aux prix retournés : haut <-> -bas, etc.).

Définitions communes
--------------------
- Swing haut de largeur k : une bougie dont le plus haut dépasse strictement les k plus hauts précédents et n'est
  pas dépassé par les k suivants. Il n'est CONNU qu'à la clôture de la k-ième bougie suivante (aucun futur).
  Swing bas : miroir. « Dernier swing haut » = le plus récent swing haut déjà confirmé avant la bougie étudiée.
- Volatilité normale : moyenne des vrais ranges des 30 bougies M1 qui PRÉCÈDENT la bougie étudiée.
- Coût : spread de la bougie (plancher 0,15 $). Une bougie (ou un rebond) de signal doit couvrir au moins
  3 fois ce coût : en dessous, le mouvement attendu ne paie pas le spread.
- Reprise quotidienne : aucune lecture pendant les 30 premières minutes après la réouverture du jour serveur
  (spread du rollover) ; toute lecture s'éteint au changement de jour serveur.

Modèle 1 — « cassure de structure avec déplacement » (continuation)
    Achat quand, sur les trois dernières bougies (1, 2, 3 = la bougie qui vient de clôturer) :
    - la bougie 2 est une bougie de déplacement haussière : corps >= disp x volatilité normale, corps >= 3 x coût,
      clôture dans le quart haut de son range (pas de mèche de rejet) ;
    - la bougie 2 clôture AU-DESSUS du dernier swing haut (largeur k) alors que la bougie 1 clôturait encore en
      dessous : c'est elle qui casse la structure ;
    - la bougie 3 laisse un déséquilibre (fair value gap) : son plus bas reste au-dessus du plus haut de la
      bougie 1, et elle clôture encore au-dessus du niveau cassé (la cassure est acceptée, pas une mèche).
    La lecture reste acheteuse tant qu'aucune clôture ne repasse sous le plus haut de la bougie 1 (déséquilibre
    entièrement comblé = le déplacement est effacé), au plus `hold` barres.
    Pourquoi ça pourrait battre 0,19 $ : au-delà d'un swing visible sont posés des stops ; une cassure franche,
    par une grande bougie qui ne se fait pas reprendre, déclenche ces stops en cascade (Osler, 2005 : les prix
    accélèrent après avoir traversé des amas de stops). Le modèle ne lit que ces moments d'expansion, où
    l'amplitude des 10 minutes suivantes est plusieurs fois le coût.

Modèle 2 — « balayage de liquidité puis changement de caractère » (retournement)
    Achat quand :
    - une bougie passe sous le dernier swing bas SIGNIFICATIF (largeur k : le plus bas de 2k+1 bougies) :
      les stops vendeurs sous ce niveau sont balayés ; on retient l'extrême du balayage (mis à jour tant que le
      prix fait plus bas) et le dernier swing haut mineur (largeur 2) connu à cet extrême, c'est-à-dire le
      dernier sommet de la jambe baissière ;
    - au plus `window` bougies après l'extrême (la bougie de l'extrême comprise) et dans la même journée
      serveur, une bougie haussière clôture au-dessus de ce sommet mineur ET au-dessus du niveau balayé
      (changement de caractère : le prix reprend ce qu'il vient de casser), avec un rebond depuis l'extrême
      >= 3 x coût.
    Un seul balayage est suivi à la fois. S'il expire (fenêtre ou fin de journée) sans reprise, le prix encore
    sous le même niveau ne compte pas comme un nouveau balayage : il en faut un nouvel extrême plus bas (la
    fenêtre repart de lui), ou une bougie de nouveau entièrement au-dessus du niveau (plus bas >= niveau) puis
    un nouveau passage dessous.
    La lecture reste acheteuse tant qu'aucune clôture ne repasse sous l'extrême du balayage, au plus `hold`
    barres.
    Pourquoi : un balayage suivi d'une reprise nette montre que la cassure a servi à remplir des ordres dans
    l'autre sens (stops déclenchés absorbés) ; les vendeurs piégés sous le niveau doivent racheter, ce qui
    alimente un mouvement rapide vers le sommet opposé. Là encore seuls les moments actifs sont lus.

Modèle 3 — « tendance de structure M5 + fin de repli M1 » (continuation après repli)
    - Bougies M5 reconstruites à partir des M1 (heure serveur), utilisées seulement une fois terminées : à la
      clôture de la minute 4 du bloc, ou de la barre suivante s'il manque des minutes en fin de bloc.
      Structure M5 haussière depuis la dernière clôture M5 au-dessus du dernier swing haut M5 (largeur k5),
      baissière depuis la dernière clôture M5 sous le dernier swing bas M5 : cassure (BOS) et changement de
      caractère (CHoCH) font basculer la structure.
    - En structure M5 haussière, le M1 fait un repli : son dernier swing haut (largeur k1) est plus bas que le
      précédent (sommet descendant). Achat quand une bougie M1 haussière clôture au-dessus de ce sommet descendant
      alors que la précédente clôturait en dessous (fin du repli), avec un range >= 3 x coût et une clôture dans
      la moitié haute.
    - La lecture reste acheteuse tant qu'aucune clôture ne passe sous le plus bas du repli (depuis le sommet
      descendant) et que la structure M5 reste haussière, au plus `hold` barres.
    Pourquoi : c'est l'entrée classique du scalpeur (biais de l'unité supérieure, déclencheur sur l'unité
    inférieure) : on achète la reprise après un repli, près d'un plus bas protégé, avec les sommets M5 comme
    cible de liquidité ; le stop naturel est court par rapport au potentiel.

Rien ne garantit que ces lectures battent le coût : sur 2019-2023 le coût (0,15 à 0,30 $) vaut 0,5 à 1 fois la
volatilité normale d'une minute ; c'est précisément pourquoi les trois modèles restent neutres (0) la plupart du
temps et ne lisent que des bougies qui couvrent plusieurs fois ce coût.

Paramètres pré-déclarés (le premier jeu de chaque modèle est le meilleur a priori, choisi par raisonnement,
avant toute mesure) :
- modèle 1 : k = largeur du swing cassé, disp = taille minimale du corps en volatilités normales, hold ;
- modèle 2 : k = largeur du swing balayé, window = délai maximal balayage -> reprise, hold ;
- modèle 3 : k5 = largeur des swings M5, k1 = largeur des swings M1, hold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

POINT = 0.01
SPREAD_FLOOR = 0.15  # $ : plancher du spread, comme le banc d'essai
BASE_BARS = 30  # bougies de « volatilité normale » (avant la bougie étudiée)
COST_MULT = 3.0  # une bougie / un rebond de signal doit couvrir 3 fois le coût
BLACKOUT_MIN = 30  # minutes neutres après la reprise quotidienne
MINOR = 2  # largeur des swings mineurs du modèle 2
CLOSE_TOP = 0.25  # modèle 1 : clôture dans le quart haut du range


# ---------------------------------------------------------------- outils causaux

def _ohlc(b: pd.DataFrame):
    return tuple(b[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))


def _cost(b: pd.DataFrame) -> np.ndarray:
    return np.maximum(b["spread"].to_numpy(dtype="float64") * POINT, SPREAD_FLOOR)


def _shift(x: np.ndarray, k: int = 1, fill=np.nan) -> np.ndarray:
    out = np.empty_like(x)
    out[:k] = fill
    out[k:] = x[:-k]
    return out


def _base_vol(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Moyenne des vrais ranges des BASE_BARS bougies avant la bougie i (exclue)."""
    prev = _shift(c)
    tr = np.nanmax(np.vstack([h - l, np.abs(h - prev), np.abs(l - prev)]), axis=0)
    return _shift(pd.Series(tr).rolling(BASE_BARS, min_periods=BASE_BARS).mean().to_numpy())


def _swings(x: np.ndarray, k: int):
    """Swings hauts de x (largeur k), vus à la clôture de chaque barre : (niveau du dernier swing confirmé,
    barre de ce swing, niveau du swing confirmé avant lui). Un swing en j n'est confirmé qu'en j + k."""
    n = len(x)
    s = pd.Series(x)
    left = s.shift(1).rolling(k, min_periods=k).max().to_numpy()
    right = s[::-1].shift(1).rolling(k, min_periods=k).max().to_numpy()[::-1]
    with np.errstate(invalid="ignore"):
        is_sw = (x > left) & (x >= right)
    idx = np.nonzero(is_sw)[0]
    conf = idx + k
    level = np.full(n, np.nan)
    prev = np.full(n, np.nan)
    bar = np.full(n, -1, dtype="int64")
    level[conf] = x[idx]
    bar[conf] = idx
    if len(idx) > 1:
        prev[conf[1:]] = x[idx[:-1]]
    level = pd.Series(level).ffill().to_numpy()
    prev = pd.Series(prev).ffill().to_numpy()
    bar = np.maximum.accumulate(bar)
    return level, bar, prev


def _day_and_blackout(b: pd.DataFrame):
    ts = b["time_server"].to_numpy().astype("int64")
    day = ts // 86_400
    first = pd.Series(ts).groupby(day).transform("first").to_numpy()
    return day, (ts - first) < BLACKOUT_MIN * 60


def _read(b, c, buy, buy_inv, sell, sell_inv, hold, kill_long=None, kill_short=None) -> np.ndarray:
    """Machine d'état commune : déclenchement, invalidation par clôture, durée maximale, bascule, jour."""
    day, blackout = _day_and_blackout(b)
    n = len(c)
    kl = kill_long if kill_long is not None else np.zeros(n, dtype=bool)
    ks = kill_short if kill_short is not None else np.zeros(n, dtype=bool)
    events = np.nonzero(buy | sell)[0]
    out = np.zeros(n, dtype="int64")
    # boucle seulement sur les déclenchements ; la lecture s'étend jusqu'à invalidation / durée / jour
    cl, dl, bl = c, day, blackout
    for i in events:
        if bl[i]:
            continue
        if buy[i] and sell[i]:
            out[i] = 0
            continue
        side = 1 if buy[i] else -1
        inv = buy_inv[i] if side > 0 else sell_inv[i]
        out[i] = side
        end = min(i + hold, n)
        for j in range(i + 1, end):
            if dl[j] != dl[i] or bl[j] or buy[j] or sell[j]:
                break  # nouveau jour, rollover, ou nouveau déclencheur (qui prend la main)
            if side > 0 and (cl[j] < inv or kl[j]):
                break
            if side < 0 and (cl[j] > inv or ks[j]):
                break
            out[j] = side
    return out


# ---------------------------------------------------------------- modèle 1 : cassure + déplacement + FVG

def _bos_side(o, h, l, c, cost, base, k, disp):
    sh = _shift(_swings(h, k)[0])  # dernier swing haut connu AVANT chaque bougie
    body = c - o
    rng = h - l
    with np.errstate(invalid="ignore"):
        displacement = (body >= disp * base) & (body >= COST_MULT * cost) & ((h - c) <= CLOSE_TOP * rng)
        breaks = (c > sh) & (_shift(c) <= sh)
        cand = displacement & breaks  # bougie 2
        trig = _shift(cand.astype(float), 1, 0.0) > 0  # vue depuis la bougie 3
        trig &= (l > _shift(h, 2)) & (c > _shift(sh))
    trig[:2] = False
    return trig, _shift(h, 2)


def bos_displacement(b: pd.DataFrame, k: int = 5, disp: float = 1.5, hold: int = 10) -> np.ndarray:
    o, h, l, c = _ohlc(b)
    cost, base = _cost(b), _base_vol(h, l, c)
    buy, buy_inv = _bos_side(o, h, l, c, cost, base, k, disp)
    sell, sell_inv = _bos_side(-o, -l, -h, -c, cost, base, k, disp)
    return _read(b, c, buy, buy_inv, sell, -sell_inv, hold)


# ---------------------------------------------------------------- modèle 2 : balayage + changement de caractère

def _sweep_side(o, h, l, c, cost, day, k, window):
    n = len(c)
    sig_low = -_shift(_swings(-l, k)[0])  # dernier swing bas significatif connu avant la bougie
    minor_high = _swings(h, MINOR)[0]  # dernier swing haut mineur connu à la clôture
    trig = np.zeros(n, dtype=bool)
    inv = np.full(n, np.nan)
    with np.errstate(invalid="ignore"):
        swept_bar = np.nonzero(l < sig_low)[0]
    if len(swept_bar) == 0:
        return trig, inv
    lo, cl, op, ds = l, c, o, day
    i_next = 0
    stale = None  # (niveau, extrême) d'un balayage expiré sans reprise
    for s in swept_bar:
        if s < i_next:
            continue  # déjà dans un balayage en cours
        if stale is not None and sig_low[s] == stale[0] and lo[s - 1] < stale[0] and lo[s] >= stale[1]:
            continue  # même balayage, prix resté sous le niveau, pas de nouvel extrême : la fenêtre ne repart pas
        swept, x, xbar, mss = sig_low[s], lo[s], s, minor_high[s]
        stale = None
        found = False
        i = s
        while i < n and i - xbar <= window and ds[i] == ds[s]:
            if lo[i] < x:
                x, xbar, mss = lo[i], i, minor_high[i]
            level = max(mss, swept)
            if cl[i] > level and cl[i] > op[i] and cl[i] - x >= COST_MULT * cost[i]:
                trig[i] = True
                inv[i] = x
                found = True
                i += 1
                break
            i += 1
        i_next = i
        if not found:
            stale = (swept, x)
    return trig, inv


def sweep_choch(b: pd.DataFrame, k: int = 10, window: int = 10, hold: int = 10) -> np.ndarray:
    o, h, l, c = _ohlc(b)
    cost = _cost(b)
    day, _ = _day_and_blackout(b)
    buy, buy_inv = _sweep_side(o, h, l, c, cost, day, k, window)
    sell, sell_inv = _sweep_side(-o, -l, -h, -c, cost, day, k, window)
    return _read(b, c, buy, buy_inv, sell, -sell_inv, hold)


# ---------------------------------------------------------------- modèle 3 : structure M5 + fin de repli M1

def _m5_bias(b: pd.DataFrame, o, h, l, c, k5: int) -> np.ndarray:
    """Structure M5 (+1 / -1 / 0) connue à la clôture de chaque barre M1 (bougies M5 terminées seulement)."""
    n = len(c)
    ts = b["time_server"].to_numpy().astype("int64")
    bucket = ts // 300
    new = np.ones(n, dtype=bool)
    new[1:] = bucket[1:] != bucket[:-1]
    first = np.nonzero(new)[0]
    last = np.append(first[1:] - 1, n - 1)
    h5 = np.maximum.reduceat(h, first)
    l5 = np.minimum.reduceat(l, first)
    c5 = c[last]
    complete_on_own = (ts[last] // 60) % 5 == 4
    done_at = np.where(complete_on_own, last, last + 1)  # barre M1 à la clôture de laquelle la M5 est finie
    sh5 = _shift(_swings(h5, k5)[0])
    sl5 = -_shift(_swings(-l5, k5)[0])
    with np.errstate(invalid="ignore"):
        up, down = c5 > sh5, c5 < sl5
    event = np.where(up & ~down, 1.0, np.where(down & ~up, -1.0, np.nan))
    bias5 = pd.Series(event).ffill().fillna(0.0).to_numpy()
    ok = done_at < n
    known = pd.Series(bias5[ok], index=done_at[ok]).groupby(level=0).last()
    out = np.full(n, np.nan)
    out[known.index.to_numpy()] = known.to_numpy()
    return pd.Series(out).ffill().fillna(0.0).to_numpy()


def _pullback_side(o, h, l, c, cost, bias, k1):
    level, bar, prev = _swings(h, k1)
    sh, sh_bar, prev_sh = _shift(level), _shift(bar, 1, -1), _shift(prev)
    rng = h - l
    with np.errstate(invalid="ignore"):
        trig = (bias > 0) & (sh < prev_sh) & (c > sh) & (_shift(c) <= sh) & (c > o)
        trig &= (rng >= COST_MULT * cost) & ((c - l) >= 0.5 * rng)
    trig[:1] = False
    inv = np.full(len(c), np.nan)
    for i in np.nonzero(trig)[0]:
        inv[i] = l[sh_bar[i]: i + 1].min()  # plus bas du repli depuis le sommet descendant
    return trig, inv


def m5_structure_pullback(b: pd.DataFrame, k5: int = 2, k1: int = 3, hold: int = 10) -> np.ndarray:
    o, h, l, c = _ohlc(b)
    cost = _cost(b)
    bias = _m5_bias(b, o, h, l, c, k5)
    buy, buy_inv = _pullback_side(o, h, l, c, cost, bias, k1)
    sell, sell_inv = _pullback_side(-o, -l, -h, -c, cost, -bias, k1)
    return _read(b, c, buy, buy_inv, sell, -sell_inv, hold, kill_long=bias <= 0, kill_short=bias >= 0)


MODELS = {
    "cassure de structure avec déplacement": (
        bos_displacement,
        [{"k": 5, "disp": 1.5, "hold": 10}, {"k": 10, "disp": 2.0, "hold": 15}, {"k": 3, "disp": 1.0, "hold": 10}],
    ),
    "balayage de liquidité puis changement de caractère": (
        sweep_choch,
        [{"k": 10, "window": 10, "hold": 10}, {"k": 20, "window": 15, "hold": 15}, {"k": 5, "window": 5, "hold": 10}],
    ),
    "structure M5 et fin de repli M1": (
        m5_structure_pullback,
        [{"k5": 2, "k1": 3, "hold": 10}, {"k5": 3, "k1": 5, "hold": 15}, {"k5": 2, "k1": 2, "hold": 5}],
    ),
}
