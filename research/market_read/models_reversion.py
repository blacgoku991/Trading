# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Lectures du marché « épuisement et retour à la moyenne » (XAUUSD, barres M1 BID clôturées).

Sortie à la clôture de chaque barre M1 : -1 (côté vente), +1 (côté achat), 0 (pas de lecture claire).
Toutes les règles sont écrites pour la VENTE (on efface une poussée haussière) ; l'achat est le miroir exact,
obtenu par le même code appliqué aux prix retournés (haut <-> bas, bougie verte <-> rouge).
Paramètres choisis AVANT tout test (raisonnement + distribution des variables sur août 2019, sans résultat).

Définitions communes (constantes fixées a priori, non optimisées) :
- « vol » = moyenne des vrais ranges M1 des 60 barres PRÉCÉDENTES (unité de mesure des étirements ; la barre
  examinée n'y entre pas, pour qu'une bougie climatique ne gonfle pas sa propre mesure).
- « coût » = spread de la barre (plancher 0,15 $), c'est-à-dire le coût aller-retour.
- Fenêtre des nouvelles lectures : de 02:00 à 23:30 heure serveur (pas de lecture autour de la coupure
  quotidienne de 00:00-01:00 : spread qui explose, gap de réouverture), et tout ce que la règle regarde en
  arrière (au moins 61 barres) doit appartenir au même jour de cotation (jamais de calcul à travers un gap).
- Potentiel minimal : la distance entre la clôture du signal et l'objectif de retour (moyenne, 50 % de la
  jambe, milieu du range) doit valoir au moins 4 fois le coût. Sinon, même un retour parfait ne paie pas.
- Persistance : la lecture reste active tant que (a) l'objectif de retour n'est pas atteint (plus bas <=
  objectif pour une vente), (b) l'extrême de l'épuisement n'est pas dépassé (plus haut > extrême : les
  acheteurs avaient raison -> 0, on se met de côté), (c) moins de `hold` barres depuis le signal, (d) même
  jour de cotation. Un nouveau signal dans le même sens renouvelle la lecture ; un signal de sens contraire
  la RETOURNE immédiatement (le sens peut donc changer en quelques minutes) ; deux signaux contraires sur la
  même barre -> 0.

Modèle 1 « élastique EMA » (paramètres span, k, hold) — la poussée s'est trop éloignée de sa moyenne courte
et une bougie la rejette. Vente à la clôture de la barre i si :
  1. l'extrême E = plus haut des barres i-2..i est aussi le plus haut des 20 dernières barres (on est au
     sommet d'une poussée, pas au milieu d'un repli) ;
  2. étirement : E - EMA(span) des clôtures >= k x vol ; l'EMA est recalculée à partir de la première
     clôture de chaque jour de cotation : elle ne traverse jamais la coupure ni le week-end ;
  3. rejet : la barre i est rouge et clôture dans les 40 % bas du range des deux dernières barres
     (i-1 et i) : mèche haute de rejet (étoile filante) ou retournement en deux bougies ;
  4. potentiel : clôture - EMA >= 4 x coût.
  Objectif : l'EMA elle-même (mobile, recalculée à chaque barre). Invalidation : plus haut > E.

Modèle 2 « épuisement de série » (paramètres n, k, hold) — une série de bougies de même couleur se termine
par une bougie qui efface la dernière. Vente à la clôture de la barre i si :
  1. les barres précédentes forment une série d'au moins n bougies vertes consécutives (clôture > ouverture)
     se terminant en i-1 (série comptée jusqu'à 15 barres) ;
  2. jambe : E - O >= k x vol, où O = ouverture de la première bougie verte de la série (des 15 dernières
     au plus si la série est plus longue) et E = plus haut atteint pendant ces bougies et la barre i ;
  3. retournement : la barre i est rouge et clôture sous l'ouverture de la dernière bougie verte (elle
     efface tout son corps : avalement baissier) ;
  4. potentiel : clôture - objectif >= 4 x coût.
  Objectif : retour de 50 % de la jambe, figé (E - 0,5 x (E - O)). Invalidation : plus haut > E.

Modèle 3 « faux départ du range » (paramètres w, r, hold) — cassure d'un range récent qui échoue (les
acheteurs de cassure sont piégés). Vente à la clôture de la barre i si :
  1. range = plus haut H et plus bas B des w barres i-w-3..i-4 ; c'est un vrai range, pas une tendance :
     H - B <= r x vol (r choisi près de la médiane de ce rapport : on garde la moitié des ranges les plus
     serrés) ;
  2. tentative : au moins une des barres i-3..i-1 a CLÔTURÉ au-dessus de H (achats de cassure déclenchés) ;
  3. échec : la barre i est rouge et clôture de nouveau sous H (retour dans le range) ;
  4. potentiel : clôture - milieu du range >= 4 x coût.
  Objectif : milieu du range (H + B) / 2, figé. Invalidation : plus haut des barres i-3..i dépassé.

Pourquoi cela pourrait battre un coût de 0,19 $ : les lectures naïves (hasard, sens du jour, tendance EMA)
lisent le marché à tout moment et ne produisent presque aucun mouvement brut ; la tendance EMA20/50 M1 est
même légèrement CONTRARIANTE à 5 minutes, signe que l'or M1 revient vers sa moyenne après les poussées. Ici
on ne lit qu'aux rares instants où une poussée est à la fois (i) extrême par rapport à la volatilité récente
(étirement de plusieurs vol, série longue, ou cassure d'un range serré), (ii) refusée par le marché (bougie
de rejet, avalement, retour dans le range : les derniers acheteurs sont piégés et leurs stops alimentent le
retour) et (iii) assez grande pour que le retour visé vaille au moins 4 fois le coût. Références de
praticiens : « snapback » vers la moyenne (L. Raschke, bandes de Keltner), avalement après une série,
« upthrust » de Wyckoff et « failed breakout » d'Al Brooks (la plupart des cassures d'un range échouent).
La lecture s'arrête dès que le retour est fait (plus rien à gagner) ou dès que l'extrême casse (le marché a
réagi dans l'autre sens) : elle ne reste donc active que pendant l'épuisement lui-même. Rien de tout cela
n'est présumé rentable : c'est une hypothèse à tester, avec tous les essais journalisés.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from harness import atr_m1, spread_price

VOL_BARS = 60             # vol = moyenne des vrais ranges des 60 barres précédentes
OPEN_FROM = 2 * 3600      # nouvelles lectures à partir de 02:00 heure serveur
OPEN_UNTIL = 23 * 3600 + 30 * 60  # ... et jusqu'à 23:30 heure serveur
POTENTIAL_COST = 4.0      # objectif de retour >= 4 x coût aller-retour
REJECT_ZONE = 0.4         # modèle 1 : clôture dans les 40 % bas du range des deux dernières barres
RUN_CAP = 15              # modèle 2 : série comptée jusqu'à 15 bougies
RETRACE = 0.5             # modèle 2 : objectif = 50 % de la jambe
ATTEMPT = 3               # modèle 3 : la tentative de cassure a lieu dans les 3 barres avant le rejet


# ---------------------------------------------------------------- outils communs

def _context(b: pd.DataFrame, lookback: int) -> dict:
    ts = b["time_server"].to_numpy().astype("int64")
    day = ts // 86_400
    tod = ts % 86_400
    n = len(b)
    back = max(lookback, VOL_BARS + 1)
    same_day = np.zeros(n, dtype=bool)
    if n > back:
        same_day[back:] = day[back:] == day[:-back]
    allowed = same_day & (tod >= OPEN_FROM) & (tod < OPEN_UNTIL)
    vol = pd.Series(atr_m1(b, VOL_BARS)).shift(1).to_numpy()
    return {
        "o": b["open"].to_numpy(dtype="float64"),
        "h": b["high"].to_numpy(dtype="float64"),
        "l": b["low"].to_numpy(dtype="float64"),
        "c": b["close"].to_numpy(dtype="float64"),
        "day": day,
        "allowed": allowed & np.isfinite(vol),
        "vol": vol,
        "cost": spread_price(b),
    }


def _shift(x: np.ndarray, k: int, fill=None) -> np.ndarray:
    """x décalé de k barres vers le futur (valeur de la barre i-k en i) ; NaN (ou `fill`) au début."""
    if fill is None:
        out = np.full(len(x), np.nan, dtype="float64")
    else:
        out = np.full(len(x), fill, dtype=x.dtype)
    if k < len(x):
        out[k:] = x[: len(x) - k]
    return out


def _rmax(x: np.ndarray, w: int) -> np.ndarray:
    return pd.Series(x).rolling(w, min_periods=w).max().to_numpy()


def _rmin(x: np.ndarray, w: int) -> np.ndarray:
    return pd.Series(x).rolling(w, min_periods=w).min().to_numpy()


def _both_sides(event_fn, ctx: dict, **params):
    """Applique la règle de vente aux prix tels quels (vente) et aux prix retournés (achat, miroir exact)."""
    sell, sell_ext, sell_tgt = event_fn(ctx["o"], ctx["h"], ctx["l"], ctx["c"], ctx, **params)
    buy, buy_ext_m, buy_tgt_m = event_fn(-ctx["o"], -ctx["l"], -ctx["h"], -ctx["c"], ctx, **params)
    return sell, sell_ext, sell_tgt, buy, -buy_ext_m, -buy_tgt_m


def _persist(sell, sell_ext, sell_tgt, buy, buy_ext, buy_tgt, h, l, day, hold: int) -> np.ndarray:
    """Machine à états commune : signal -> lecture qui dure jusqu'à objectif / invalidation / hold / fin du jour.

    Extrême et objectif sont figés à la barre du signal (modèles 2 et 3). Le modèle 1 (objectif mobile = EMA
    courante) utilise `_persist_dynamic`. Un signal renouvelle ou retourne la lecture ; deux signaux
    contraires sur la même barre -> 0.
    """
    n = len(h)
    out = np.zeros(n, dtype=np.int64)
    sell_l, buy_l = sell.tolist(), buy.tolist()
    se, st, be, bt = sell_ext.tolist(), sell_tgt.tolist(), buy_ext.tolist(), buy_tgt.tolist()
    hl, ll, dl = h.tolist(), l.tolist(), day.tolist()
    side, ext, tgt, start, d0 = 0, 0.0, 0.0, 0, 0
    for i in range(n):
        s, bb = sell_l[i], buy_l[i]
        if s and bb:  # deux lectures contraires sur la même barre : rien
            side = 0
            continue
        if s:
            side, ext, tgt, start, d0 = -1, se[i], st[i], i, dl[i]
            out[i] = -1
            continue
        if bb:
            side, ext, tgt, start, d0 = 1, be[i], bt[i], i, dl[i]
            out[i] = 1
            continue
        if side == 0:
            continue
        if dl[i] != d0 or i - start >= hold:
            side = 0
        elif side < 0:
            if hl[i] > ext or ll[i] <= tgt:
                side = 0
        else:
            if ll[i] < ext or hl[i] >= tgt:
                side = 0
        out[i] = side
    return out


def _persist_dynamic(sell, sell_ext, buy, buy_ext, mean, h, l, day, hold: int) -> np.ndarray:
    """Variante à objectif mobile : la lecture s'arrête quand le prix touche la moyenne courante."""
    n = len(h)
    out = np.zeros(n, dtype=np.int64)
    sell_l, buy_l = sell.tolist(), buy.tolist()
    se, be, ml = sell_ext.tolist(), buy_ext.tolist(), mean.tolist()
    hl, ll, dl = h.tolist(), l.tolist(), day.tolist()
    side, ext, start, d0 = 0, 0.0, 0, 0
    for i in range(n):
        s, bb = sell_l[i], buy_l[i]
        if s and bb:
            side = 0
            continue
        if s:
            side, ext, start, d0 = -1, se[i], i, dl[i]
            out[i] = -1
            continue
        if bb:
            side, ext, start, d0 = 1, be[i], i, dl[i]
            out[i] = 1
            continue
        if side == 0:
            continue
        if dl[i] != d0 or i - start >= hold:
            side = 0
        elif side < 0:
            if hl[i] > ext or ll[i] <= ml[i]:
                side = 0
        else:
            if ll[i] < ext or hl[i] >= ml[i]:
                side = 0
        out[i] = side
    return out


# ---------------------------------------------------------------- modèle 1 : élastique EMA

def _ema(x: np.ndarray, span: int, day: np.ndarray) -> np.ndarray:
    """EMA recalculée à chaque jour de cotation (départ à la première clôture du jour) : jamais de moyenne à
    travers la coupure ou le week-end, et mémoire strictement finie (identique quel que soit l'historique chargé)."""
    ema = pd.Series(x).groupby(day).ewm(span=span, adjust=False).mean()
    return ema.droplevel(0).sort_index().to_numpy()


def _elastic_sell(o, h, l, c, ctx, *, k: float, mean: np.ndarray):
    ext = _rmax(h, 3)                          # extrême de la poussée (barres i-2..i)
    top = ext >= _rmax(h, 20)                  # c'est le plus haut des 20 dernières barres
    with np.errstate(invalid="ignore"):
        stretch = (ext - mean) >= k * ctx["vol"]
        hi2 = np.maximum(h, _shift(h, 1))
        lo2 = np.minimum(l, _shift(l, 1))
        reject = (c < o) & (c <= lo2 + REJECT_ZONE * (hi2 - lo2))
        potential = (c - mean) >= POTENTIAL_COST * ctx["cost"]
    fire = top & stretch & reject & potential & ctx["allowed"]
    return fire, ext, np.full(len(c), np.nan)


def elastic_ema(b: pd.DataFrame, span: int = 20, k: float = 3.0, hold: int = 15) -> np.ndarray:
    ctx = _context(b, lookback=20)
    mean = _ema(ctx["c"], span, ctx["day"])
    sell, se, _ = _elastic_sell(ctx["o"], ctx["h"], ctx["l"], ctx["c"], ctx, k=k, mean=mean)
    buy, be_m, _ = _elastic_sell(-ctx["o"], -ctx["l"], -ctx["h"], -ctx["c"], ctx, k=k, mean=-mean)
    return _persist_dynamic(sell, se, buy, -be_m, mean, ctx["h"], ctx["l"], ctx["day"], hold)


# ---------------------------------------------------------------- modèle 2 : épuisement de série

def _run_sell(o, h, l, c, ctx, *, n: int, k: float):
    size = len(c)
    green = c > o
    # longueur de la série verte se terminant à chaque barre (plafonnée à RUN_CAP), calcul à mémoire finie
    run_at = np.zeros(size, dtype=np.int64)
    alive = np.ones(size, dtype=bool)
    for m in range(RUN_CAP):
        alive &= _shift(green, m, fill=False)
        run_at += alive
    run = _shift(run_at, 1, fill=0)            # série terminée en i-1
    idx = np.arange(size)
    first = np.clip(idx - run, 0, size - 1)    # première bougie verte de la série
    origin = o[first]
    ext = h.copy()                             # plus haut de la série et de la barre i
    for m in range(1, RUN_CAP + 1):
        hm = _shift(h, m)
        use = run >= m
        ext = np.where(use & (hm > ext), hm, ext)
    target = ext - RETRACE * (ext - origin)
    with np.errstate(invalid="ignore"):
        leg = (ext - origin) >= k * ctx["vol"]
        reversal = (c < o) & (c < _shift(o, 1))
        potential = (c - target) >= POTENTIAL_COST * ctx["cost"]
    fire = (run >= n) & leg & reversal & potential & ctx["allowed"]
    return fire, ext, target


def run_exhaustion(b: pd.DataFrame, n: int = 4, k: float = 3.0, hold: int = 15) -> np.ndarray:
    ctx = _context(b, lookback=RUN_CAP + 1)
    sell, se, st, buy, be, bt = _both_sides(_run_sell, ctx, n=n, k=k)
    return _persist(sell, se, st, buy, be, bt, ctx["h"], ctx["l"], ctx["day"], hold)


# ---------------------------------------------------------------- modèle 3 : faux départ du range

def _failed_break_sell(o, h, l, c, ctx, *, w: int, r: float):
    box_hi = _shift(_rmax(h, w), ATTEMPT + 1)   # barres i-w-3 .. i-4
    box_lo = _shift(_rmin(l, w), ATTEMPT + 1)
    tried = _shift(_rmax(c, ATTEMPT), 1)        # plus haute clôture des barres i-3..i-1
    ext = _rmax(h, ATTEMPT + 1)                 # plus haut des barres i-3..i
    mid = 0.5 * (box_hi + box_lo)
    with np.errstate(invalid="ignore"):
        is_range = (box_hi - box_lo) <= r * ctx["vol"]
        attempt = tried > box_hi
        failed = (c < o) & (c < box_hi)
        potential = (c - mid) >= POTENTIAL_COST * ctx["cost"]
    fire = is_range & attempt & failed & potential & ctx["allowed"]
    return fire, ext, mid


def failed_breakout(b: pd.DataFrame, w: int = 30, r: float = 6.0, hold: int = 20) -> np.ndarray:
    ctx = _context(b, lookback=w + ATTEMPT + 1)
    sell, se, st, buy, be, bt = _both_sides(_failed_break_sell, ctx, w=w, r=r)
    return _persist(sell, se, st, buy, be, bt, ctx["h"], ctx["l"], ctx["day"], hold)


MODELS = {
    "élastique EMA": (elastic_ema, [
        {"span": 20, "k": 3.0, "hold": 15},
        {"span": 20, "k": 4.0, "hold": 15},
        {"span": 50, "k": 5.0, "hold": 30},
    ]),
    "épuisement de série": (run_exhaustion, [
        {"n": 4, "k": 3.0, "hold": 15},
        {"n": 3, "k": 4.0, "hold": 15},
        {"n": 5, "k": 3.0, "hold": 20},
    ]),
    "faux départ du range": (failed_breakout, [
        {"w": 30, "r": 6.0, "hold": 20},
        {"w": 60, "r": 8.0, "hold": 30},
        {"w": 15, "r": 4.0, "hold": 10},
    ]),
}
