# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Lecture du marché par la RÉACTION DES BOUGIES aux niveaux de liquidité (XAUUSD, barres M1 clôturées).

Sortie à la clôture de chaque barre M1 : +1 (côté achat), -1 (côté vente), 0 (pas de lecture claire).
Règles symétriques : chaque règle de vente est le miroir exact de la règle d'achat.

Niveaux de liquidité (là où dorment les stops), tous connus AVANT la bougie examinée :
- « veille » : plus haut et plus bas du jour de cotation précédent (jour serveur, reprise à 18:00 New York) ;
- « asie » : plus haut et plus bas de la séance asiatique du jour (du début du jour serveur à 07:00 Londres),
  utilisés seulement après 07:00 Londres, quand le range est terminé ;
- « jour » : plus haut (plus bas) du jour formé il y a au moins 30 minutes et jamais retouché depuis
  (le plus haut de séance « établi ») ;
- « ronds » (seulement dans le jeu « cles+ronds ») : multiples de 10 $.
Jeux de niveaux (paramètre `levels`) : "veille+asie", "cles" = veille + asie + jour, "cles+ronds" = cles + ronds.

Constantes fixées a priori (non optimisées) :
- fenêtre d'événements : de 07:00 Londres à 16:00 New York (liquidité Londres puis New York ; la nuit
  asiatique fait des mouvements trop petits pour un coût de 0,19 $) ;
- taille minimale de la bougie de réaction : amplitude >= max(ATR M1(14) des barres précédentes,
  5 x le spread de la barre) — une bougie plus petite que 5 fois le coût n'est que du bruit ;
- fraîcheur d'un niveau pour un balayage : aucune des barres i-29 à i-2 ne l'a touché (environ 30 minutes
  sans contact : la liquidité n'a pas encore été prise) ; pour « jour », le niveau est le plus haut du jour
  jusqu'à la barre i-30, non retouché par les barres i-29 à i-2 ;
- bougie de déplacement : clôture dans les 30 % extrêmes de son amplitude.

Événement A — BALAYAGE REJETÉ (lecture contraire au balayage). Vente si :
  1. un plus haut de liquidité frais (non touché pendant les 30 min précédentes) est dépassé par la mèche
     de la bougie i, ou par la bougie i-1 qui avait clôturé au-dessus (ou exactement sur le niveau) ;
  2. la bougie i CLÔTURE SOUS le niveau (retour dans le range : les acheteurs de cassure sont piégés) ;
  3. la bougie de réaction (i seule si i a percé ; i-1 et i fusionnées si i-1 a percé ; l'une des deux
     formes suffit) a une mèche haute >= `wick` x son amplitude, et une amplitude >= la taille minimale ;
  4. bougie i dans la fenêtre d'événements, bougie i-1 du même jour de cotation.
  Miroir pour l'achat (plus bas balayé, clôture au-dessus, mèche basse).
  La lecture dure `hold` minutes, et s'annule (0) dès que l'extrême du balayage (plus haut des deux
  dernières bougies) est dépassé : les vendeurs ont eu tort.

Événement B — CASSURE ACCEPTÉE (lecture dans le sens de la cassure). Achat si :
  1. bougie i de déplacement : clôture précédente <= niveau < clôture de i, corps haussier
     >= `body_k` x ATR M1(14) des barres précédentes, clôture dans les 30 % hauts, amplitude >= taille minimale,
     dans la fenêtre d'événements, bougie i-1 du même jour ; si plusieurs niveaux sont franchis, on retient
     le plus proche de la clôture (le plus haut) ;
  2. SUIVI : la bougie i+1 clôture encore au-dessus du niveau ET au-dessus du milieu de la bougie de cassure
     (le marché accepte le prix ; sur l'or, une grosse bougie M1 seule a plutôt tendance à revenir).
  La lecture commence à la clôture de i+1, dure `hold` minutes, s'annule dès qu'une bougie clôture sous le
  niveau cassé ou passe sous le bas de la bougie de cassure. Miroir pour la vente.

Modèle « réaction aux niveaux » (A + B + piège) : la dernière réaction l'emporte (la lecture peut donc
changer de sens en quelques minutes) ; deux réactions opposées sur la même bougie (balayage d'un côté,
cassure acceptée de l'autre) -> 0 ; balayage et cassure du même côté -> règles d'annulation de la cassure.
PIÈGE : si une
cassure acceptée échoue par une bougie baissière de taille minimale, dans la fenêtre d'événements, qui
clôture sous le niveau cassé (pendant la lecture), les acheteurs de cassure sont piégés -> lecture vente
pendant `hold` minutes, annulée si le plus haut atteint depuis la bougie de cassure est dépassé (miroir pour
une cassure baissière).
Durée (tous les modèles) : la lecture vaut à la clôture de la bougie d'événement puis aux `hold` clôtures
suivantes, sauf annulation ; une nouvelle réaction la remplace. Toute lecture s'arrête à la fin du jour de
cotation.

Pourquoi cela pourrait battre un coût de 0,19 $ : le hasard, le sens du jour et la tendance EMA ne donnent
presque aucun mouvement brut à 5-15 min, car ils lisent le marché à tout moment. Ici, on ne lit qu'aux
rares instants où des stops se déclenchent sur un niveau surveillé par tous (Osler 2003 : les ordres
stop se regroupent juste au-delà des niveaux et des chiffres ronds, les prises de bénéfice juste avant ;
« Turtle Soup » de L. Raschke : faux départ au-delà d'un extrême). Après un balayage rejeté, les
acheteurs de cassure piégés doivent sortir ; après une cassure acceptée, la cascade de stops continue.
Ces moments concentrent des mouvements de plusieurs dollars en quelques minutes, et la taille minimale
de bougie (>= 5 spreads) écarte les réactions trop petites pour payer le coût. Rien n'est promis :
les tests antérieurs du projet (balayage / cassure du plus haut de la veille sans critère de bougie)
étaient négatifs ; ce module teste si la FORME de la réaction (mèche + clôture de retour, ou
déplacement + suivi) sépare les bons cas des mauvais.

Paramètres libres (3 au plus par modèle, 3 jeux déclarés a priori AVANT tout essai, le premier est le
meilleur a priori) :
- « balayage rejeté » (hold, wick, levels) : 15 min / 0,5 / cles (un retournement après chasse aux stops se
  joue en 5 à 20 min ; une mèche d'au moins la moitié de la bougie = rejet net) ; 30 min / 0,5 / veille+asie
  (niveaux les plus surveillés -> retournements plus amples et plus longs) ; 10 min / 0,6 / cles+ronds
  (prises de bénéfice aux chiffres ronds : réactions plus courtes, rejet plus net exigé).
- « cassure acceptée » (hold, body_k, levels) : 15 min / 1,5 / cles ; 30 min / 2,0 / veille+asie (déplacement
  plus fort sur les niveaux majeurs -> cascade plus longue) ; 10 min / 1,5 / cles+ronds (cascade de stops
  juste après un chiffre rond, courte).
- « réaction aux niveaux » (hold, wick, body_k), niveaux « cles » : 15 / 0,5 / 1,5 (a priori des deux
  événements) ; 30 / 0,5 / 2,0 (lecture plus longue, cassures plus sélectives) ; 10 / 0,6 / 1,2 (lecture
  courte qui change plus souvent de sens).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from goldbot.indicators.sessions import LONDON, NEW_YORK
from harness import spread_price

# ---------------------------------------------------------------- constantes fixées a priori
FRESH_BARS = 30  # minutes sans contact avant un balayage ; ancienneté minimale d'un extrême du jour
ATR_BARS = 14
CLOSE_LOC = 0.7  # clôture dans les 30 % extrêmes de la bougie de déplacement
MIN_SPREADS = 5.0  # amplitude minimale de la bougie de réaction, en spreads
ROUND_STEP = 10.0  # chiffres ronds : multiples de 10 $
LONDON_START = 7 * 60  # 07:00 Londres
NY_END = 16 * 60  # 16:00 New York
ASIA_MAX_ELAPSED = 12 * 3600  # la séance asiatique est au début du jour serveur

LEVEL_SETS = {
    "veille+asie": ("veille", "asie"),
    "cles": ("veille", "asie", "jour"),
    "cles+ronds": ("veille", "asie", "jour", "ronds"),
}

NONE, FADE, BREAK, CONFLICT = 0, 1, 2, 3


# ---------------------------------------------------------------- aides (causales, mémoire finie, exactes)

def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[: len(a) - k]
    return out


def _atr_prev(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    """ATR M1(14) en moyenne simple des barres i-14..i-1 (somme exacte sur la fenêtre : même valeur quel que
    soit le début de l'historique chargé)."""
    prev = _shift(c, 1)
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev), np.abs(l - prev)))
    atr = np.full(len(h), np.nan)
    if len(h) >= ATR_BARS:
        atr[ATR_BARS - 1:] = sliding_window_view(tr, ATR_BARS).mean(axis=1)
    atr[:ATR_BARS] = np.nan  # première barre sans clôture précédente
    return _shift(atr, 1)


def _context(b: pd.DataFrame, levels: str) -> dict:
    if levels not in LEVEL_SETS:
        raise ValueError(f"jeu de niveaux inconnu : {levels}")
    fams = LEVEL_SETS[levels]
    o, h, l, c = (b[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    n = len(b)
    ts = b["time_server"].to_numpy().astype("int64")
    day = ts // 86_400
    elapsed = ts - day * 86_400
    lon_t = b["time"].dt.tz_convert(LONDON)
    ny_t = b["time"].dt.tz_convert(NEW_YORK)
    lon = (lon_t.dt.hour * 60 + lon_t.dt.minute).to_numpy()
    ny = (ny_t.dt.hour * 60 + ny_t.dt.minute).to_numpy()
    window = (lon >= LONDON_START) & (ny < NY_END)

    rng = h - l
    atrp = _atr_prev(h, l, c)
    gate = np.fmax(atrp, MIN_SPREADS * spread_price(b))  # NaN d'ATR -> taille du spread seulement
    gate = np.where(np.isnan(atrp), np.nan, gate)  # pas d'événement avant 15 barres d'historique

    h1, l1, c1, o1 = _shift(h, 1), _shift(l, 1), _shift(c, 1), _shift(o, 1)
    same1 = np.zeros(n, dtype=bool)
    same1[1:] = day[1:] == day[:-1]

    # plus haut / plus bas des barres i-29..i-2 (fraîcheur d'un niveau avant la bougie de réaction)
    w = FRESH_BARS - 2
    fr_hi = pd.Series(h).rolling(w, min_periods=w).max().shift(2).to_numpy()
    fr_lo = pd.Series(l).rolling(w, min_periods=w).min().shift(2).to_numpy()

    hi_levels, lo_levels = [], []  # niveaux fixes connus avant la barre i
    if "veille" in fams:
        g = pd.DataFrame({"d": day, "h": h, "l": l}).groupby("d").agg(h=("h", "max"), l=("l", "min"))
        hi_levels.append(g["h"].shift(1).reindex(day).to_numpy())
        lo_levels.append(g["l"].shift(1).reindex(day).to_numpy())
    if "asie" in fams:
        asian = (elapsed < ASIA_MAX_ELAPSED) & ((lon < LONDON_START) | (lon >= 20 * 60))
        s = pd.DataFrame({"d": day[asian], "h": h[asian], "l": l[asian]}).groupby("d").agg(
            h=("h", "max"), l=("l", "min"))
        ah = s["h"].reindex(day).to_numpy().copy()
        al = s["l"].reindex(day).to_numpy().copy()
        ah[asian], al[asian] = np.nan, np.nan  # range pas terminé : pas encore un niveau
        hi_levels.append(ah)
        lo_levels.append(al)
    if "jour" in fams:
        cm_hi = pd.Series(h).groupby(day).cummax().to_numpy()
        cm_lo = pd.Series(l).groupby(day).cummin().to_numpy()
        g_ok = np.zeros(n, dtype=bool)
        g_ok[FRESH_BARS:] = day[FRESH_BARS:] == day[:-FRESH_BARS]
        jh = np.where(g_ok, _shift(cm_hi, FRESH_BARS), np.nan)
        jl = np.where(g_ok, _shift(cm_lo, FRESH_BARS), np.nan)
        # établi = pas retouché depuis (sinon ce n'est plus le plus haut de séance)
        jh = np.where(fr_hi < jh, jh, np.nan)
        jl = np.where(fr_lo > jl, jl, np.nan)
        hi_levels.append(jh)
        lo_levels.append(jl)
    rounds = "ronds" in fams
    return dict(o=o, h=h, l=l, c=c, h1=h1, l1=l1, c1=c1, o1=o1, rng=rng, atrp=atrp, gate=gate, day=day,
                same1=same1, window=window, fr_hi=fr_hi, fr_lo=fr_lo, hi_levels=hi_levels,
                lo_levels=lo_levels, rounds=rounds, n=n)


def _sweep_events(x: dict, wick: float) -> tuple[np.ndarray, np.ndarray]:
    """Événement A : balayage rejeté. Renvoie (côté, extrême du balayage)."""
    o, h, l, c, h1, l1, c1, o1 = (x[k] for k in ("o", "h", "l", "c", "h1", "l1", "c1", "o1"))
    rng, gate = x["rng"], x["gate"]
    base = x["window"] & x["same1"]
    with np.errstate(invalid="ignore"):
        # bougie seule (i)
        up_wick = h - np.fmax(o, c)
        dn_wick = np.fmin(o, c) - l
        single_sell = (up_wick >= wick * rng) & (rng >= gate)
        single_buy = (dn_wick >= wick * rng) & (rng >= gate)
        # bougies i-1 et i fusionnées
        ch, cl = np.fmax(h1, h), np.fmin(l1, l)
        crng = ch - cl
        comp_sell = (ch - np.fmax(o1, c) >= wick * crng) & (crng >= gate)
        comp_buy = (np.fmin(o1, c) - cl >= wick * crng) & (crng >= gate)

        hi_levels = list(x["hi_levels"])
        lo_levels = list(x["lo_levels"])
        if x["rounds"]:
            hi_levels.append(np.floor(c / ROUND_STEP) * ROUND_STEP + ROUND_STEP)  # rond juste au-dessus
            lo_levels.append(np.ceil(c / ROUND_STEP) * ROUND_STEP - ROUND_STEP)  # rond juste au-dessous

        sell = np.zeros(x["n"], dtype=bool)
        buy = np.zeros(x["n"], dtype=bool)
        for L in hi_levels:
            fresh = x["fr_hi"] < L
            back = c < L
            pierce_i = (h > L) & single_sell
            pierce_prev = (h1 > L) & (c1 >= L) & comp_sell
            sell |= fresh & back & (pierce_i | pierce_prev)
        for L in lo_levels:
            fresh = x["fr_lo"] > L
            back = c > L
            pierce_i = (l < L) & single_buy
            pierce_prev = (l1 < L) & (c1 <= L) & comp_buy
            buy |= fresh & back & (pierce_i | pierce_prev)
    sell &= base
    buy &= base
    side = np.where(sell & ~buy, -1, np.where(buy & ~sell, 1, 0))
    conflict = sell & buy
    ext = np.where(side < 0, np.fmax(h1, h), np.where(side > 0, np.fmin(l1, l), np.nan))
    side = np.where(conflict, 2, side)  # 2 = réactions opposées
    return side, ext


def _break_events(x: dict, body_k: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Événement B : cassure acceptée, datée à la barre de SUIVI (i+1).
    Renvoie (côté, niveau cassé, extrême opposé de la bougie de cassure, indice de la bougie de cassure)."""
    o, h, l, c, c1 = (x[k] for k in ("o", "h", "l", "c", "c1"))
    rng, gate, atrp = x["rng"], x["gate"], x["atrp"]
    n = x["n"]
    with np.errstate(invalid="ignore"):
        disp_up = (c - o >= body_k * atrp) & (c - l >= CLOSE_LOC * rng) & (rng >= gate) & x["window"] & x["same1"]
        disp_dn = (o - c >= body_k * atrp) & (h - c >= CLOSE_LOC * rng) & (rng >= gate) & x["window"] & x["same1"]
        hi_levels = list(x["hi_levels"])
        lo_levels = list(x["lo_levels"])
        if x["rounds"]:
            hi_levels.append(np.floor(c / ROUND_STEP) * ROUND_STEP)  # rond franchi à la hausse
            lo_levels.append(np.ceil(c / ROUND_STEP) * ROUND_STEP)  # rond franchi à la baisse
        lvl_up = np.full(n, np.nan)
        lvl_dn = np.full(n, np.nan)
        for L in hi_levels:  # cassure vers le haut d'un plus haut de liquidité
            cross = (c1 <= L) & (c > L) & disp_up
            lvl_up = np.where(cross, np.fmax(lvl_up, L), lvl_up)  # le niveau cassé le plus proche du prix
        for L in lo_levels:
            cross = (c1 >= L) & (c < L) & disp_dn
            lvl_dn = np.where(cross, np.fmin(lvl_dn, L), lvl_dn)
        up_ev = ~np.isnan(lvl_up)
        dn_ev = ~np.isnan(lvl_dn)
        both = up_ev & dn_ev
        up_ev &= ~both
        dn_ev &= ~both
        mid = (h + l) / 2
        # suivi à la barre i+1 (même jour) : clôture au-delà du niveau et du milieu de la bougie de cassure
        side = np.zeros(n, dtype=np.int64)
        lvl = np.full(n, np.nan)
        ref = np.full(n, np.nan)
        start = np.full(n, -1, dtype=np.int64)
        if n > 1:
            nxt_same = x["same1"][1:]
            cn = c[1:]
            ok_up = up_ev[:-1] & nxt_same & (cn > lvl_up[:-1]) & (cn > mid[:-1])
            ok_dn = dn_ev[:-1] & nxt_same & (cn < lvl_dn[:-1]) & (cn < mid[:-1])
            side[1:] = np.where(ok_up, 1, np.where(ok_dn, -1, 0))
            lvl[1:] = np.where(ok_up, lvl_up[:-1], np.where(ok_dn, lvl_dn[:-1], np.nan))
            ref[1:] = np.where(ok_up, l[:-1], np.where(ok_dn, h[:-1], np.nan))
            start[1:] = np.where(ok_up | ok_dn, np.arange(n - 1), -1)
    return side, lvl, ref, start


def _run(x: dict, kind: np.ndarray, side: np.ndarray, ext: np.ndarray, lvl: np.ndarray, ref: np.ndarray,
         start: np.ndarray, hold: int, trap: bool) -> np.ndarray:
    """Machine à états : la dernière réaction l'emporte ; durée `hold` ; annulations ; piège (option)."""
    o, h, l, c, day, window, gate = (x[k] for k in ("o", "h", "l", "c", "day", "window", "gate"))
    n = x["n"]
    out = np.zeros(n, dtype=np.int64)
    if n == 0:
        return out
    # dernière barre du même jour de cotation
    change = np.flatnonzero(np.diff(day) != 0)
    last = np.empty(n, dtype=np.int64)
    bounds = np.concatenate([change, [n - 1]])
    starts = np.concatenate([[0], change + 1])
    for s0, e0 in zip(starts, bounds):
        last[s0:e0 + 1] = e0
    idx = np.flatnonzero(kind != NONE)
    m = len(idx)
    k = 0
    cur = None
    while True:
        if cur is None:
            if k >= m:
                break
            e = int(idx[k])
            k += 1
            cur = (e, int(side[e]), int(kind[e]), ext[e], lvl[e], ref[e], int(start[e]))
        e, s, kd, ex, lv, rf, st = cur
        cur = None
        if kd == CONFLICT:
            continue  # réactions opposées : pas de lecture (la lecture précédente s'est arrêtée avant)
        nxt = int(idx[k]) if k < m else n
        end = min(e + hold, int(last[e]), nxt - 1)
        out[e] = s
        if end <= e:
            continue
        seg = slice(e + 1, end + 1)
        if kd == FADE:
            bad = h[seg] > ex if s < 0 else l[seg] < ex
            fail_close = np.zeros(end - e, dtype=bool)
        else:
            if s > 0:
                fail_close = c[seg] < lv
                bad = fail_close | (l[seg] < rf)
            else:
                fail_close = c[seg] > lv
                bad = fail_close | (h[seg] > rf)
        hits = np.flatnonzero(bad)
        if hits.size == 0:
            out[e + 1:end + 1] = s
            continue
        j = e + 1 + int(hits[0])
        out[e + 1:j] = s  # la barre j annule la lecture (valeur 0 sauf piège)
        if trap and kd == BREAK and fail_close[hits[0]] and window[j]:
            bearish_against = c[j] < o[j] if s > 0 else c[j] > o[j]
            if bearish_against and h[j] - l[j] >= gate[j]:  # bougie d'échec de taille minimale
                if s > 0:
                    new_ext = float(np.max(h[st:j + 1]))
                else:
                    new_ext = float(np.min(l[st:j + 1]))
                cur = (j, -s, FADE, new_ext, np.nan, np.nan, j)
    return out


def _events(x: dict, use_sweep: bool, use_break: bool, wick: float, body_k: float):
    n = x["n"]
    kind = np.zeros(n, dtype=np.int64)
    side = np.zeros(n, dtype=np.int64)
    ext = np.full(n, np.nan)
    lvl = np.full(n, np.nan)
    ref = np.full(n, np.nan)
    start = np.full(n, -1, dtype=np.int64)
    if use_sweep:
        s_side, s_ext = _sweep_events(x, wick)
        m = (s_side == 1) | (s_side == -1)
        kind[m], side[m], ext[m] = FADE, s_side[m], s_ext[m]
        kind[s_side == 2] = CONFLICT
    if use_break:
        b_side, b_lvl, b_ref, b_start = _break_events(x, body_k)
        m = b_side != 0
        opposite = m & (kind == FADE) & (side != b_side)
        conflict_prev = m & (kind == CONFLICT)
        take = m & ~opposite & ~conflict_prev  # même sens : la cassure (invalidation par le niveau) l'emporte
        kind[take], side[take], lvl[take], ref[take], start[take] = BREAK, b_side[take], b_lvl[take], \
            b_ref[take], b_start[take]
        kind[opposite] = CONFLICT
        side[opposite] = 0
    return kind, side, ext, lvl, ref, start


# ---------------------------------------------------------------- modèles

def sweep_reject(b: pd.DataFrame, hold: int = 15, wick: float = 0.5, levels: str = "cles") -> np.ndarray:
    """Balayage rejeté seul (lecture contraire au balayage)."""
    x = _context(b, levels)
    kind, side, ext, lvl, ref, start = _events(x, True, False, wick, 1.5)
    return _run(x, kind, side, ext, lvl, ref, start, int(hold), trap=False)


def accepted_break(b: pd.DataFrame, hold: int = 15, body_k: float = 1.5, levels: str = "cles") -> np.ndarray:
    """Cassure acceptée seule (déplacement + suivi, lecture dans le sens de la cassure)."""
    x = _context(b, levels)
    kind, side, ext, lvl, ref, start = _events(x, False, True, 0.5, body_k)
    return _run(x, kind, side, ext, lvl, ref, start, int(hold), trap=False)


def level_reaction(b: pd.DataFrame, hold: int = 15, wick: float = 0.5, body_k: float = 1.5) -> np.ndarray:
    """Balayages rejetés + cassures acceptées + pièges sur les niveaux « cles » ; la dernière réaction l'emporte."""
    x = _context(b, "cles")
    kind, side, ext, lvl, ref, start = _events(x, True, True, wick, body_k)
    return _run(x, kind, side, ext, lvl, ref, start, int(hold), trap=True)


MODELS = {
    "balayage rejeté": (sweep_reject, [
        {"hold": 15, "wick": 0.5, "levels": "cles"},
        {"hold": 30, "wick": 0.5, "levels": "veille+asie"},
        {"hold": 10, "wick": 0.6, "levels": "cles+ronds"},
    ]),
    "cassure acceptée": (accepted_break, [
        {"hold": 15, "body_k": 1.5, "levels": "cles"},
        {"hold": 30, "body_k": 2.0, "levels": "veille+asie"},
        {"hold": 10, "body_k": 1.5, "levels": "cles+ronds"},
    ]),
    "réaction aux niveaux": (level_reaction, [
        {"hold": 15, "wick": 0.5, "body_k": 1.5},
        {"hold": 30, "wick": 0.5, "body_k": 2.0},
        {"hold": 10, "wick": 0.6, "body_k": 1.2},
    ]),
}
