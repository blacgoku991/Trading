# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Lectures du marché « rafales d'élan et régime de volatilité » (sens achat / vente à la clôture de chaque barre M1).

Idée commune (raisonnement de scalpeur, fixé AVANT tout test) : le coût aller-retour (environ 0,19 $/oz) est
fixe en dollars, alors que l'amplitude des 10 minutes suivantes dépend du régime. Dans le calme, un mouvement
de 10 minutes vaut environ 1 $ : même avec un bon sens, le coût mange tout. La volatilité se regroupe (après
une rafale, les minutes suivantes restent agitées) et, pendant une rafale, le flux est persistant : ordres
stop déclenchés en cascade au-delà des niveaux évidents, dérive après une statistique américaine, arrivée de
Londres ou de New York. Donc on ne lit un sens QUE pendant ces moments, dans le sens de la rafale, et on se
met de côté (0) dans le hachis (faible efficacité, bougies qui se contredisent). Règles symétriques : la vente
est le miroir exact de l'achat.

Définitions communes (barres BID, clôturées) :
- « TR » = vrai range de la barre (après une coupure de plus de 10 min : haut - bas seulement).
- « base » = moyenne des TR des 240 barres précédentes, décalée de 5 barres (activité normale des 4 dernières
  heures, sans la rafale en cours).
- Coupures : une nouvelle séquence commence après un trou de plus de 10 min (coupure quotidienne, week-end).
  Tout retour en arrière de h barres (élan, efficacité, boîte) exige h barres dans la séquence en cours : jamais
  de mouvement mesuré à travers un gap. Seule la base (mesure d'activité) peut couvrir la fin de la séance
  précédente ; le TR de la première barre d'une séquence exclut le gap.
- « minutes » = barres M1 (les minutes manquantes à l'intérieur d'une séquence sont rarissimes, environ 0,006 %).
- Prix traités en points entiers de 0,01 $ : les égalités (clôture pile au tiers, pile au milieu, retracement
  pile à 50 %) sont tranchées de la même façon à l'achat et à la vente.
- Zone interdite (lecture 0 et état remis à zéro) : de 23:30 à 01:30 heure serveur (clôture/reprise quotidienne,
  spread qui explose, gap de réouverture).
- Plancher de coût : le mouvement déclencheur doit valoir au moins 1,5 $ (environ 8 fois le coût aller-retour),
  sinon ce n'est pas une rafale exploitable, quel que soit le rapport à la base.

Modèle 1 « rafale propre » (paramètres k, er_min, hold) :
- Achat déclenché à la clôture de la barre i si, sur les 5 dernières barres : la clôture a monté d'au moins
  max(k x base, 1,5 $) depuis la clôture d'il y a 5 barres ; le chemin a été propre (efficacité = |déplacement| /
  somme des |variations de clôture| >= er_min : peu de bougies contraires) ; et la bougie i clôture dans le tiers
  HAUT de son range (pas de mèche de rejet en haut : la rafale n'est pas refusée).
- Persistance : la lecture reste « achat » pendant `hold` minutes après le dernier déclenchement (un nouveau
  déclenchement dans le même sens prolonge). Le point d'origine de l'impulsion est la clôture d'avant la rafale
  (clôture d'il y a 5 barres au PREMIER déclenchement de la série ; un déclenchement qui prolonge ne la change
  pas) ; l'extrême est le plus haut atteint depuis. La barre qui déclenche n'est pas soumise à l'invalidation.
- Invalidation (-> 0) : une clôture rend plus de la moitié de l'impulsion (clôture < extrême - 50 % x (extrême -
  origine)) : le marché réagit contre, la lecture n'est plus valable.
- Retournement : un déclenchement vente (rafale propre vers le bas) remplace immédiatement l'achat, et inversement.
- Pourquoi ça pourrait battre 0,19 $ : on ne lit qu'après un déplacement de plusieurs $ en 5 minutes, dans un
  régime où l'écart-type des 10 minutes suivantes vaut plusieurs $ ; une continuation même faible (cascade de
  stops, flux d'une statistique) y vaut plus que le coût, alors qu'elle ne vaut rien dans le calme.

Modèle 2 « compression puis cassure, ou piège » (paramètres n_box, q_max, hold) :
- Boîte = plus haut et plus bas des n_box barres précédant la barre i. Compression : hauteur de la boîte <=
  q_max x ATR journalier (moyenne des vrais ranges des 14 jours de cotation précédents) x racine(n_box / 1380)
  (c'est-à-dire au plus q_max fois ce qu'un marché à la volatilité du jour parcourt normalement en n_box
  minutes) et hauteur >= 1 $ (plus petit : bruit face au coût).
- Cassure haussière à la clôture de la barre i : clôture au-dessus du haut de la boîte, bougie de déplacement
  (range >= 2 x TR moyen de la boîte, corps haussier >= 50 % du range, clôture dans le tiers haut).
- Persistance : « achat » à chaque clôture au-dessus du haut de la boîte, pendant `hold` minutes au plus après
  la cassure. Clôture revenue dans la moitié haute de la boîte : 0 (hésitation, on attend) ; si une clôture
  repasse au-dessus du haut avant la fin des `hold` minutes, la lecture « achat » reprend.
- Piège (retournement) : si, dans ces `hold` minutes, une clôture repasse sous le MILIEU de la boîte, la cassure a
  échoué : les acheteurs de la cassure sont piégés et leurs stops sont sous la boîte -> lecture « vente » à
  chaque clôture sous le milieu, pendant `hold` minutes au plus après le piège ; 0 à une clôture entre le milieu
  et le haut (la vente reprend si la clôture repasse sous le milieu) ; si une clôture repasse au-dessus du haut
  de la boîte, plus aucune lecture (marché indécis).
- Une nouvelle cassure (dans un sens ou l'autre) remplace toujours l'état en cours.
- Pourquoi ça pourrait battre 0,19 $ : la volatilité revient à la moyenne (une compression précède une
  expansion) et les stops s'accumulent juste au-delà d'une boîte serrée ; la cassure franche ou son échec
  désignent le côté où ces stops vont être exécutés, au moment précis où l'amplitude augmente.

Modèle 3 « élan multi-horizons aux heures actives » (paramètres t_in, er_min, v_min) :
- Seulement de 07:00 à 10:00 heure de Londres (ouverture européenne, fixing de 10:30 exclu) et de 08:20 à 11:00
  heure de New York (ouverture COMEX 08:20, statistiques 08:30, actions 09:30, fixing de l'après-midi) ; 0 ailleurs.
- Élan normalisé z_h = (clôture_i - clôture_{i-h}) / (base x racine(h)) pour h = 1, 5, 15 et 60 minutes, chacun
  borné à [-3, 3] ; score = moyenne des quatre (1 minute = la dernière bougie, 60 minutes = le contexte).
- Entrée achat : score >= t_in, efficacité des 15 dernières minutes >= er_min, expansion (TR moyen des 15
  dernières barres / base) >= v_min, et hausse d'au moins 1,5 $ sur 15 minutes.
- Persistance par hystérésis : la lecture « achat » reste tant que score >= t_in / 2 ; en dessous -> 0.
- Retournement : si les conditions d'entrée vente sont remplies, la lecture passe directement à « vente ».
  Fin de fenêtre -> 0.
- Pourquoi ça pourrait battre 0,19 $ : le flux institutionnel et les statistiques arrivent à ces heures ; quand
  les quatre horizons vont dans le même sens avec des bougies qui s'enchaînent et s'agrandissent, le mouvement
  a le plus de chances de continuer à une échelle (plusieurs $) qui dépasse le coût.

Aucune logique de martingale, de grille ni de moyenne à la baisse : ce module ne produit qu'un sens.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from harness import daily_atr  # importe d'abord le banc : il ajoute src/ au chemin

from goldbot.indicators.sessions import LONDON, NEW_YORK  # noqa: E402

SEGMENT_GAP_S = 600  # trou > 10 min = nouvelle séquence (coupure quotidienne, week-end, incident)
BASE_BARS = 240  # 4 heures d'activité normale
BASE_LAG = 5  # la base exclut la rafale en cours
TICK = 0.01  # $ : pas de cotation (2 décimales) ; calculs internes en points entiers, sans bruit de flottant
COST_FLOOR = 1.5  # $ : mouvement déclencheur minimal (environ 8 x le coût aller-retour)
MIN_BOX = 1.0  # $ : hauteur minimale de boîte
COST_FLOOR_PTS = round(COST_FLOOR / TICK)
MIN_BOX_PTS = round(MIN_BOX / TICK)
DAY_MINUTES = 1380  # minutes de cotation par jour (23 h)
BLACKOUT_FROM = 23 * 3600 + 30 * 60  # 23:30 heure serveur
BLACKOUT_TO = 1 * 3600 + 30 * 60  # 01:30 heure serveur


def _shift(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def _common(b: pd.DataFrame) -> dict[str, np.ndarray]:
    # prix en points entiers (1 point = 0,01 $) : sommes et différences exactes, donc égalités (clôture pile au
    # tiers, pile au milieu, retracement pile à 50 %) traitées de façon strictement symétrique achat / vente
    o, h, l, c = (np.round(b[k].to_numpy(dtype="float64") / TICK) for k in ("open", "high", "low", "close"))
    ts = b["time_server"].to_numpy(dtype="int64")
    n = len(b)
    new_seg = np.ones(n, dtype=bool)
    new_seg[1:] = np.diff(ts) > SEGMENT_GAP_S
    starts = np.flatnonzero(new_seg)
    seg = np.cumsum(new_seg) - 1
    since = np.arange(n) - starts[seg]  # barres depuis le début de la séquence
    prev_c = _shift(c, 1)
    prev_c[new_seg] = np.nan
    hl = h - l
    with np.errstate(invalid="ignore"):
        tr = np.where(np.isnan(prev_c), hl, np.maximum(hl, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c))))
    base = pd.Series(tr).rolling(BASE_BARS, min_periods=BASE_BARS).mean().shift(BASE_LAG).to_numpy()
    sod = ts % 86_400
    blackout = (sod >= BLACKOUT_FROM) | (sod < BLACKOUT_TO)
    dc = np.abs(c - prev_c)  # nan au début de séquence
    with np.errstate(invalid="ignore", divide="ignore"):
        loc = np.where(hl > 0, (c - l) / np.where(hl > 0, hl, 1.0), 0.5)  # position de la clôture dans la bougie
    return {"o": o, "h": h, "l": l, "c": c, "since": since, "new_seg": new_seg, "tr": tr, "base": base,
            "blackout": blackout, "dc": dc, "loc": loc, "n": n}


def _efficiency(c: np.ndarray, dc: np.ndarray, h: int) -> tuple[np.ndarray, np.ndarray]:
    move = c - _shift(c, h)
    path = pd.Series(dc).rolling(h, min_periods=h).sum().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        er = np.where(path > 0, np.abs(move) / np.where(path > 0, path, 1.0), 0.0)
    return move, er


# ---------------------------------------------------------------- modèle 1 : rafale propre

def burst_read(b: pd.DataFrame, k: float = 4.0, er_min: float = 0.6, hold: int = 15) -> np.ndarray:
    X = _common(b)
    c, h, l, base, n = X["c"], X["h"], X["l"], X["base"], X["n"]
    move5, er5 = _efficiency(c, X["dc"], 5)
    alive = ~X["blackout"] & ~X["new_seg"]
    with np.errstate(invalid="ignore"):
        valid = alive & (X["since"] >= 5) & np.isfinite(base) & np.isfinite(move5)
        big = np.abs(move5) >= np.maximum(k * base, COST_FLOOR_PTS)
        clean = er5 >= er_min
        up = valid & big & clean & (move5 > 0) & (X["loc"] >= 2 / 3)
        dn = valid & big & clean & (move5 < 0) & (X["loc"] <= 1 / 3)
    origin = _shift(c, 5)

    out = np.zeros(n, dtype=np.int8)
    active = np.flatnonzero(up | dn)
    if len(active) == 0:
        return out.astype(int)
    alive_l, up_l, dn_l = alive.tolist(), up.tolist(), dn.tolist()
    c_l, h_l, l_l, o_l = c.tolist(), h.tolist(), l.tolist(), origin.tolist()
    s, t_last, p0, ext = 0, 0, 0.0, 0.0
    i = int(active[0])
    while i < n:
        if not alive_l[i]:
            s = 0
        elif up_l[i]:
            if s != 1:
                s, p0, ext = 1, o_l[i], h_l[i]
            else:
                ext = max(ext, h_l[i])
            t_last = i
        elif dn_l[i]:
            if s != -1:
                s, p0, ext = -1, o_l[i], l_l[i]
            else:
                ext = min(ext, l_l[i])
            t_last = i
        elif s == 1:
            ext = max(ext, h_l[i])
            if i - t_last > hold or c_l[i] < ext - 0.5 * (ext - p0):
                s = 0
        elif s == -1:
            ext = min(ext, l_l[i])
            if i - t_last > hold or c_l[i] > ext + 0.5 * (p0 - ext):
                s = 0
        out[i] = s
        if s == 0:  # saut direct au prochain déclenchement
            nxt = np.searchsorted(active, i + 1)
            if nxt >= len(active):
                break
            i = int(active[nxt])
        else:
            i += 1
    return out.astype(int)


# ---------------------------------------------------------------- modèle 2 : compression puis cassure, ou piège

def squeeze_read(b: pd.DataFrame, n_box: int = 30, q_max: float = 0.6, hold: int = 20) -> np.ndarray:
    X = _common(b)
    o, c, h, l, n = X["o"], X["c"], X["h"], X["l"], X["n"]
    hi = pd.Series(h).rolling(n_box, min_periods=n_box).max().shift(1).to_numpy()
    lo = pd.Series(l).rolling(n_box, min_periods=n_box).min().shift(1).to_numpy()
    box_tr = pd.Series(X["tr"]).rolling(n_box, min_periods=n_box).mean().shift(1).to_numpy()
    datr = daily_atr(b, 14) / TICK  # en points, comme les prix
    height = hi - lo
    rng = h - l
    body = c - o
    alive = ~X["blackout"] & ~X["new_seg"]
    with np.errstate(invalid="ignore"):
        box_ok = (alive & (X["since"] >= n_box + 1) & np.isfinite(datr) & np.isfinite(height)
                  & (height <= q_max * datr * np.sqrt(n_box / DAY_MINUTES)) & (height >= MIN_BOX_PTS))
        disp = box_ok & (rng >= 2.0 * box_tr) & (np.abs(body) >= 0.5 * rng)
        up = disp & (c > hi) & (body > 0) & (X["loc"] >= 2 / 3)
        dn = disp & (c < lo) & (body < 0) & (X["loc"] <= 1 / 3)

    out = np.zeros(n, dtype=np.int8)
    active = np.flatnonzero(up | dn)
    if len(active) == 0:
        return out.astype(int)
    alive_l, up_l, dn_l, c_l = alive.tolist(), up.tolist(), dn.tolist(), c.tolist()
    hi_l, lo_l = hi.tolist(), lo.tolist()
    s, trapped, t0, top, bottom = 0, False, 0, 0.0, 0.0
    i = int(active[0])
    while i < n:
        r = 0
        if not alive_l[i]:
            s = 0
        elif up_l[i] or dn_l[i]:
            s = 1 if up_l[i] else -1
            trapped, t0, top, bottom = False, i, hi_l[i], lo_l[i]
            r = s
        elif s != 0:
            if i - t0 > hold:
                s = 0
            else:
                mid = 0.5 * (top + bottom)
                edge = top if s > 0 else bottom
                beyond = s * (c_l[i] - edge) > 0  # clôture au-delà du bord cassé
                through = s * (c_l[i] - mid) < 0  # clôture revenue au-delà du milieu de la boîte
                if not trapped:
                    if beyond:
                        r = s
                    elif through:
                        trapped, t0, r = True, i, -s
                else:
                    if beyond:
                        s = 0
                    elif through:
                        r = -s
        out[i] = r
        if s == 0:
            nxt = np.searchsorted(active, i + 1)
            if nxt >= len(active):
                break
            i = int(active[nxt])
        else:
            i += 1
    return out.astype(int)


# ---------------------------------------------------------------- modèle 3 : élan multi-horizons aux heures actives

def _window(b: pd.DataFrame) -> np.ndarray:
    t = b["time"]
    lon = t.dt.tz_convert(LONDON)
    ny = t.dt.tz_convert(NEW_YORK)
    m_lon = (lon.dt.hour * 60 + lon.dt.minute).to_numpy()
    m_ny = (ny.dt.hour * 60 + ny.dt.minute).to_numpy()
    wd = ny.dt.weekday.to_numpy()
    weekday = wd < 5
    return weekday & (((m_lon >= 7 * 60) & (m_lon < 10 * 60)) | ((m_ny >= 8 * 60 + 20) & (m_ny < 11 * 60)))


def multi_momentum_read(b: pd.DataFrame, t_in: float = 1.2, er_min: float = 0.4, v_min: float = 1.3) -> np.ndarray:
    X = _common(b)
    c, base, n = X["c"], X["base"], X["n"]
    zs = []
    for hz in (1, 5, 15, 60):
        with np.errstate(invalid="ignore", divide="ignore"):
            z = (c - _shift(c, hz)) / (base * np.sqrt(hz))
        zs.append(np.clip(z, -3.0, 3.0))
    score = np.mean(np.vstack(zs), axis=0)
    move15, er15 = _efficiency(c, X["dc"], 15)
    expansion = pd.Series(X["tr"]).rolling(15, min_periods=15).mean().to_numpy() / base
    ok = _window(b) & ~X["blackout"] & (X["since"] >= 60) & np.isfinite(base) & np.isfinite(score)
    with np.errstate(invalid="ignore"):
        gate = ok & (er15 >= er_min) & (expansion >= v_min)
        up = gate & (score >= t_in) & (move15 >= COST_FLOOR_PTS)
        dn = gate & (score <= -t_in) & (move15 <= -COST_FLOOR_PTS)
        keep_up = ok & (score >= 0.5 * t_in)
        keep_dn = ok & (score <= -0.5 * t_in)

    out = np.zeros(n, dtype=np.int8)
    active = np.flatnonzero(up | dn)
    if len(active) == 0:
        return out.astype(int)
    up_l, dn_l, ku_l, kd_l = up.tolist(), dn.tolist(), keep_up.tolist(), keep_dn.tolist()
    s = 0
    i = int(active[0])
    while i < n:
        if up_l[i]:
            s = 1
        elif dn_l[i]:
            s = -1
        elif s == 1 and not ku_l[i]:
            s = 0
        elif s == -1 and not kd_l[i]:
            s = 0
        out[i] = s
        if s == 0:
            nxt = np.searchsorted(active, i + 1)
            if nxt >= len(active):
                break
            i = int(active[nxt])
        else:
            i += 1
    return out.astype(int)


MODELS = {
    "rafale propre": (burst_read, [
        {"k": 4.0, "er_min": 0.6, "hold": 15},
        {"k": 6.0, "er_min": 0.7, "hold": 10},
        {"k": 3.0, "er_min": 0.5, "hold": 20},
    ]),
    "compression puis cassure ou piège": (squeeze_read, [
        {"n_box": 30, "q_max": 0.6, "hold": 20},
        {"n_box": 60, "q_max": 0.6, "hold": 30},
        {"n_box": 15, "q_max": 0.5, "hold": 10},
    ]),
    "élan multi-horizons aux heures actives": (multi_momentum_read, [
        {"t_in": 1.2, "er_min": 0.4, "v_min": 1.3},
        {"t_in": 1.6, "er_min": 0.5, "v_min": 1.5},
        {"t_in": 0.9, "er_min": 0.35, "v_min": 1.1},
    ]),
}
