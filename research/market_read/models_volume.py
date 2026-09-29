# ruff: noqa: E741  (l = plus bas des bougies, notation OHLC)
"""Lecture du marché par la PARTICIPATION : volume de ticks, VWAP de séance et ses bandes (XAUUSD, barres M1 clôturées).

Sortie à la clôture de chaque barre M1 : +1 (côté achat), -1 (côté vente), 0 (pas de lecture claire).
Règles symétriques : chaque règle de vente est le miroir exact de la règle d'achat.

Mesures communes (toutes connues à la clôture de la barre i, mémoire finie) :
- PARTICIPATION (rang du volume) : place du volume de ticks de la barre parmi les volumes des mêmes minutes
  (heure serveur m-2 à m+2) des 10 jours de cotation précédents, soit jusqu'à 50 minutes de référence :
  rang = (nombre de références plus petites + 0,5 x nombre d'égales) / nombre de références (au moins 25).
  Rang 0,95 = la barre fait partie des 5 % de minutes les plus actives à cette heure-là. Le jour en cours
  n'entre jamais dans sa propre référence. Pourquoi un rang et pas un rapport au volume normal (« RVOL ») :
  le volume de l'or a un fort profil horaire (3 à 6 fois plus de ticks à l'ouverture de New York qu'en fin
  d'Asie), et chez Axi le volume de ticks PLAFONNE vers 550-590 ticks par minute depuis 2025 (médiane 250-300) :
  un rapport « 2 fois le normal » y devient presque impossible aux heures actives, alors qu'un rang reste
  comparable d'une année à l'autre.
- VWAP DE SÉANCE : prix typique (haut + bas + clôture) / 3 pondéré par le volume de ticks, cumulé depuis le
  dernier ancrage du jour serveur : début du jour serveur (18:00 New York, séance asiatique), 08:00 Londres,
  08:20 New York (ouverture du COMEX, juste avant les chiffres de 08:30). Écart-type σ : écart-type du prix
  typique autour du VWAP, pondéré par le volume, depuis l'ancrage. Bandes : VWAP ± k·σ.
- ATR M1(14) des barres précédentes (i-14 à i-1).
- Taille minimale d'une bougie d'événement (initiation, épuisement) : amplitude (haut - bas) >= 5 fois le
  spread de la barre (plancher 0,15 $, donc >= 0,75 $) : une bougie plus petite que 5 coûts n'annonce pas un
  mouvement qui paie le coût.
- Une bougie « fusionnée » (i-1, i) a pour ouverture celle de i-1, pour clôture celle de i, pour haut / bas les
  extrêmes des deux (les deux barres dans la même séance VWAP).
Toute lecture s'arrête à la fin du jour de cotation. Un nouvel événement (du même modèle) remplace la lecture
en cours, quel que soit son sens : la lecture peut donc passer de l'achat à la vente en une minute.

Modèle 1 — « initiation » (suivre la participation qui lance le mouvement). Achat à la clôture de i si :
  1. rang de participation de i >= vol_q : bien plus de participants que d'habitude à cette heure ;
  2. bougie haussière de déplacement : corps (clôture - ouverture) >= body_k x ATR, clôture dans le quart haut
     de la bougie ;
  3. sortie de l'équilibre : clôture au-dessus du plus haut des 15 barres précédentes ;
  4. clôture au-dessus du VWAP de séance (les acheteurs contrôlent l'enchère) ; amplitude >= 5 spreads.
  La lecture dure `hold` minutes tant que le prix tient : elle s'annule (0) dès qu'une bougie clôture sous le
  milieu du corps de la bougie d'initiation, ou sous le VWAP (même séance). Miroir pour la vente.

Modèle 2 — « épuisement » (s'opposer au climax). Vente à la clôture de i si, pour la bougie i seule ou pour
la bougie fusionnée (i-1, i) :
  1. étirement : son plus haut touche VWAP + band_k·σ (au moins 20 barres depuis l'ancrage) ;
  2. c'est un extrême : son plus haut >= plus haut des 30 barres qui la précèdent ;
  3. climax de participation : rang de participation >= vol_q (de i, ou de i-1 ou i pour la fusionnée) ;
  4. rejet : mèche haute >= 50 % de son amplitude, la barre i est baissière, et la clôture de i revient sous
     VWAP + band_k·σ (retour dans la bande) ; amplitude >= 5 spreads.
  La lecture dure `hold` minutes ; elle s'annule dès qu'une bougie clôture au-dessus de l'extrême du climax
  (les acheteurs n'étaient pas épuisés), et se termine quand le prix revient toucher le VWAP de la même séance
  (plus bas <= VWAP pour une vente : la valeur est atteinte, le repli est fait ; après un nouvel ancrage, seule
  la règle de l'extrême s'applique). Miroir pour l'achat (climax vendeur sous VWAP - band_k·σ, mèche basse,
  fin quand le plus haut touche le VWAP).

Modèle 3 — « défense du VWAP » (reprise de tendance à la valeur). Achat à la clôture de i si :
  1. séance acceptée au-dessus de la valeur : au moins `frac` des 30 clôtures précédentes au-dessus de leur
     VWAP (au moins 30 barres depuis l'ancrage) ;
  2. la séance s'écarte assez de sa valeur pour payer le coût : σ >= 5 spreads (>= 0,75 $) ;
  3. la bougie i vient tester la valeur : plus bas <= VWAP + 0,25·σ ;
  4. repli calme puis participation au test : rang de participation de i >= rang moyen des 5 barres
     précédentes + vol_d (le repli s'est fait sans vendeurs, les acheteurs arrivent à la valeur), et rang de
     participation de i >= 0,5 (au moins l'activité habituelle de l'heure) ; un rang inconnu (moins de 25
     références) compte pour 1 dans la moyenne du repli (prudent : le contraste exigé devient plus dur) ;
  5. rejet acheteur : clôture au-dessus du VWAP, bougie haussière, clôture dans les 40 % hauts de la bougie.
  La lecture dure `hold` minutes ; elle s'annule dès qu'une bougie clôture sous le plus bas de la bougie de
  rejet (la défense a cédé). Miroir pour la vente (séance sous le VWAP, rebond sans acheteurs, rejet vendeur).

Pourquoi cela pourrait battre un coût de 0,19 $ : lire le marché à tout moment (hasard, sens du jour, EMA) ne
donne presque aucun mouvement brut, car la plupart des minutes sont du bruit de quelques cents. Le volume de
ticks mesure l'activité : les minutes les plus actives de leur heure sont celles des chiffres, des ouvertures
et des cascades d'ordres, où l'or bouge de plusieurs dollars en quelques minutes ; la taille minimale
(5 spreads) écarte le reste. Le sens vient de la forme de la bougie par rapport à la valeur de la séance
(lecture « Market Profile / VSA ») : un grand corps qui clôture à son extrême, hors de l'équilibre et du bon
côté du VWAP = activité d'initiative (des ordres continuent d'arriver) ; un volume record avec une longue mèche
loin du VWAP = activité de réponse (les derniers acheteurs sont entrés, le prix revient vers la valeur) ; un
repli calme jusqu'au VWAP puis un rejet avec volume = les grands intervenants défendent leur prix moyen.
Rien n'est promis : le volume de ticks d'un CFD n'est qu'un nombre de cotations, pas un volume échangé, et il
plafonne chez Axi depuis 2025 (information perdue au-delà du plafond, justement aux minutes les plus actives).
Limite connue : pendant les 2 à 3 semaines où les heures d'été américaine et européenne diffèrent, la référence
de la minute d'ouverture de Londres est décalée d'une heure pendant quelques jours.

Paramètres libres (3 au plus par modèle ; 3 jeux déclarés a priori AVANT tout essai, le premier est le
meilleur a priori) :
- « initiation » (vol_q, body_k, hold) : 0,95 / 1,5 / 15 (5 % des minutes les plus actives de l'heure +
  déplacement net ; 15 min couvrent un scalp de 10 min) ; 0,99 / 2,0 / 20 (plus active que toutes les
  références : initiations rares de type chiffre, suivi plus long) ; 0,90 / 1,0 / 10 (plus fréquentes,
  lecture plus courte).
- « épuisement » (band_k, vol_q, hold) : 2,0 / 0,95 / 15 ; 2,5 / 0,99 / 20 (climax plus extrême, retour plus
  ample) ; 1,5 / 0,90 / 10 (plus fréquent, plus court).
- « défense du VWAP » (vol_d, frac, hold) : 0,30 / 0,8 / 15 (le test est nettement plus actif que le repli,
  séance au-dessus de sa valeur 80 % du temps) ; 0,45 / 0,9 / 20 (tendance plus nette, défense plus forte) ;
  0,20 / 0,7 / 10 (plus fréquent, plus court).
Réglages de conception faits AVANT toute mesure de mouvement, sur la fréquence des événements (août 2019) et
sur la distribution du volume (toutes années, sans les prix futurs) : (1) la première défense (repli exigé sous
le volume normal et bougie de rejet >= 5 spreads) ne donnait que 0,2 lecture par jour -> contraste de
participation test / repli et condition σ >= 5 spreads ; (2) les rapports de volume (RVOL >= 2, volume du test
>= 1,5 fois celui du repli) ne se déclenchaient presque plus en 2025-2026 à cause du plafond de ticks -> rangs
de participation (fréquences ensuite stables de 2020 à 2026).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from goldbot.indicators.sessions import LONDON, NEW_YORK
from harness import spread_price

# ---------------------------------------------------------------- constantes fixées a priori
ATR_BARS = 14
REF_DAYS = 10  # jours de cotation de référence pour le rang de participation
REF_HALF_WIDTH = 2  # minutes m-2 .. m+2
REF_MIN = 25  # au moins 25 minutes de référence (5 jours)
MIN_SPREADS = 5.0  # amplitude minimale d'une bougie d'événement / dispersion minimale de séance, en spreads
LONDON_ANCHOR = (8, 0)  # 08:00 Londres
NY_ANCHOR = (8, 20)  # 08:20 New York (ouverture du COMEX)
BALANCE_BARS = 15  # équilibre récent à quitter (initiation)
CLOSE_LOC_INIT = 0.75  # clôture dans le quart extrême (initiation)
EXTREME_BARS = 30  # l'épuisement doit faire un extrême de 30 barres
MIN_SINCE_BANDS = 20  # barres depuis l'ancrage avant d'utiliser les bandes
WICK = 0.5  # mèche de rejet >= 50 % de l'amplitude (épuisement)
TREND_BARS = 30  # fenêtre d'acceptation au-dessus / au-dessous du VWAP (défense)
PULLBACK_BARS = 5  # repli : rang moyen des 5 barres précédentes
MIN_RANK_DEF = 0.5  # la bougie de rejet a au moins l'activité habituelle de son heure
TEST_SIGMA = 0.25  # test de la valeur : plus bas <= VWAP + 0,25 sigma
CLOSE_LOC_DEF = 0.6  # clôture dans les 40 % extrêmes (défense)


# ---------------------------------------------------------------- aides (causales, mémoire finie, exactes)

def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(a), np.nan)
    if k < len(a):
        out[k:] = a[: len(a) - k]
    return out


def _window_mean_prev(x: np.ndarray, w: int) -> np.ndarray:
    """Moyenne exacte des valeurs i-w..i-1 (même résultat quel que soit le début de l'historique)."""
    out = np.full(len(x), np.nan)
    if len(x) > w:
        out[w:] = sliding_window_view(x, w)[:-1].mean(axis=1)
    return out


def _atr_prev(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    prev = _shift(c, 1)
    tr = np.fmax(h - l, np.fmax(np.abs(h - prev), np.abs(l - prev)))
    tr[0] = np.nan  # première barre sans clôture précédente
    return _window_mean_prev(tr, ATR_BARS)


def _utc_seconds(t: pd.Series) -> np.ndarray:
    return t.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy().astype("datetime64[s]").astype("int64")


def _anchor_utc(dates: pd.DatetimeIndex, tz: str, hm: tuple[int, int]) -> np.ndarray:
    local = (dates + pd.Timedelta(hours=hm[0], minutes=hm[1])).tz_localize(tz)
    return local.tz_convert("UTC").tz_localize(None).to_numpy().astype("datetime64[s]").astype("int64")


def _participation_rank(tv: np.ndarray, drow: np.ndarray, ndays: int, minute: np.ndarray) -> np.ndarray:
    """Rang du volume de la barre parmi les minutes m-2..m+2 des 10 jours de cotation précédents."""
    grid = np.full((ndays, 1440), np.nan)
    grid[drow, minute] = tv
    less = np.zeros(len(tv))
    equal = np.zeros(len(tv))
    total = np.zeros(len(tv))
    for lag in range(1, REF_DAYS + 1):
        rows = drow - lag
        row_ok = rows >= 0
        rows = np.where(row_ok, rows, 0)
        for dm in range(-REF_HALF_WIDTH, REF_HALF_WIDTH + 1):
            cols = minute + dm
            ok = row_ok & (cols >= 0) & (cols < 1440)
            ref = grid[rows, np.clip(cols, 0, 1439)]
            ok &= ~np.isnan(ref)
            total += ok
            less += ok & (ref < tv)
            equal += ok & (ref == tv)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(total >= REF_MIN, (less + 0.5 * equal) / total, np.nan)


def _features(b: pd.DataFrame) -> dict:
    o, h, l, c = (b[k].to_numpy(dtype="float64") for k in ("open", "high", "low", "close"))
    tv = b["tick_volume"].to_numpy(dtype="float64")
    n = len(b)
    ts = b["time_server"].to_numpy().astype("int64")
    day = ts // 86_400
    minute = (ts - day * 86_400) // 60
    utc = _utc_seconds(b["time"])

    # ancrages du VWAP : début du jour serveur, 08:00 Londres et 08:20 New York de la date serveur
    udays, drow = np.unique(day, return_inverse=True)
    dates = pd.DatetimeIndex(pd.to_datetime(udays * 86_400, unit="s"))
    lon_a = _anchor_utc(dates, LONDON, LONDON_ANCHOR)[drow]
    ny_a = _anchor_utc(dates, NEW_YORK, NY_ANCHOR)[drow]
    part = (utc >= lon_a).astype("int64") + (utc >= ny_a).astype("int64")
    seg = day * 3 + part
    newseg = np.ones(n, dtype=bool)
    newseg[1:] = seg[1:] != seg[:-1]
    seg_id = np.cumsum(newseg)  # numéro de séance (croissant)
    since = np.arange(n) - np.maximum.accumulate(np.where(newseg, np.arange(n), 0))

    tp = (h + l + c) / 3.0
    ref = pd.Series(tp).groupby(seg_id).transform("first").to_numpy()
    x = tp - ref  # centré : variance précise
    g = pd.DataFrame({"s": seg_id, "v": tv, "vx": tv * x, "vxx": tv * x * x}).groupby("s")
    cv = g["v"].cumsum().to_numpy()
    cvx = g["vx"].cumsum().to_numpy()
    cvxx = g["vxx"].cumsum().to_numpy()
    mean_x = cvx / cv
    vwap = ref + mean_x
    sigma = np.sqrt(np.maximum(cvxx / cv - mean_x * mean_x, 0.0))

    rank = _participation_rank(tv, drow, len(udays), minute)
    rng = h - l
    spr = spread_price(b)
    return dict(o=o, h=h, l=l, c=c, tv=tv, n=n, day=day, seg=seg_id, since=since, vwap=vwap, sigma=sigma,
                rank=rank, rng=rng, spr=spr, big=rng >= MIN_SPREADS * spr, atrp=_atr_prev(h, l, c))


def _persist(ev: np.ndarray, lvl: np.ndarray, x: dict, hold: int, mode: str) -> np.ndarray:
    """Transforme les événements en lecture qui dure : `hold` minutes au plus, jusqu'au prochain événement,
    jusqu'à la fin du jour, et tant que les règles d'annulation du modèle ne sont pas déclenchées."""
    n = x["n"]
    out = np.zeros(n, dtype=int)
    idx = np.flatnonzero(ev)
    if len(idx) == 0:
        return out
    c, h, l, vwap, seg, day = x["c"], x["h"], x["l"], x["vwap"], x["seg"], x["day"]
    nxt = np.r_[idx[1:], n]
    for s, e_next in zip(idx.tolist(), nxt.tolist()):
        side = int(ev[s])
        level = float(lvl[s])
        end = min(e_next, s + hold, n)
        k = s + 1
        while k < end:
            if day[k] != day[s]:
                break
            ck = c[k]
            same = seg[k] == seg[s]
            if mode == "initiation":
                if side > 0 and (ck < level or (same and ck < vwap[k])):
                    break
                if side < 0 and (ck > level or (same and ck > vwap[k])):
                    break
            elif mode == "fade":
                if side < 0 and (ck > level or (same and l[k] <= vwap[k])):
                    break
                if side > 0 and (ck < level or (same and h[k] >= vwap[k])):
                    break
            else:  # defense
                if (side > 0 and ck < level) or (side < 0 and ck > level):
                    break
            k += 1
        out[s:k] = side
    return out


# ---------------------------------------------------------------- modèle 1 : initiation

def initiation(b: pd.DataFrame, vol_q: float = 0.95, body_k: float = 1.5, hold: int = 15) -> np.ndarray:
    x = _features(b)
    o, h, l, c, rng, rank, vwap, atrp = (x[k] for k in ("o", "h", "l", "c", "rng", "rank", "vwap", "atrp"))
    hh = pd.Series(h).rolling(BALANCE_BARS, min_periods=BALANCE_BARS).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(BALANCE_BARS, min_periods=BALANCE_BARS).min().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        common = (rank >= vol_q) & x["big"]
        buy = common & (c - o >= body_k * atrp) & (c - l >= CLOSE_LOC_INIT * rng) & (c > hh) & (c > vwap)
        sell = common & (o - c >= body_k * atrp) & (h - c >= CLOSE_LOC_INIT * rng) & (c < ll) & (c < vwap)
    ev = np.where(buy, 1, np.where(sell, -1, 0))
    lvl = (o + c) / 2.0  # milieu du corps de la bougie d'initiation
    return _persist(ev, lvl, x, int(hold), "initiation")


# ---------------------------------------------------------------- modèle 2 : épuisement

def exhaustion(b: pd.DataFrame, band_k: float = 2.0, vol_q: float = 0.95, hold: int = 15) -> np.ndarray:
    x = _features(b)
    o, h, l, c, rank, vwap, sigma, seg, spr = (
        x[k] for k in ("o", "h", "l", "c", "rank", "vwap", "sigma", "seg", "spr"))
    upper = vwap + band_k * sigma
    lower = vwap - band_k * sigma
    min_rng = MIN_SPREADS * spr
    hh = pd.Series(h).rolling(EXTREME_BARS, min_periods=EXTREME_BARS).max().shift(1).to_numpy()
    ll = pd.Series(l).rolling(EXTREME_BARS, min_periods=EXTREME_BARS).min().shift(1).to_numpy()
    o1, h1, l1, rank1 = _shift(o, 1), _shift(h, 1), _shift(l, 1), _shift(rank, 1)
    seg1 = np.r_[-1, seg[:-1]]
    ok = x["since"] >= MIN_SINCE_BANDS
    with np.errstate(invalid="ignore"):
        # bougie seule i
        r1 = h - l
        s_sell = ((h >= upper) & (h >= hh) & (rank >= vol_q) & (h - np.fmax(o, c) >= WICK * r1)
                  & (r1 >= min_rng))
        s_buy = ((l <= lower) & (l <= ll) & (rank >= vol_q) & (np.fmin(o, c) - l >= WICK * r1)
                 & (r1 >= min_rng))
        # bougie fusionnée (i-1, i), les deux dans la même séance
        H2, L2 = np.fmax(h1, h), np.fmin(l1, l)
        r2 = H2 - L2
        climax2 = np.fmax(rank1, rank) >= vol_q
        same = seg1 == seg
        m_sell = (same & (H2 >= upper) & (H2 >= _shift(hh, 1)) & climax2
                  & (H2 - np.fmax(o1, c) >= WICK * r2) & (r2 >= min_rng))
        m_buy = (same & (L2 <= lower) & (L2 <= _shift(ll, 1)) & climax2
                 & (np.fmin(o1, c) - L2 >= WICK * r2) & (r2 >= min_rng))
        sell = ok & (c < o) & (c < upper) & (s_sell | m_sell)
        buy = ok & (c > o) & (c > lower) & (s_buy | m_buy)
    ev = np.where(sell, -1, np.where(buy, 1, 0))
    # extrême du climax : celui de la bougie fusionnée si elle a déclenché, sinon celui de la bougie seule
    lvl = np.where(sell, np.where(m_sell, H2, h), np.where(buy, np.where(m_buy, L2, l), np.nan))
    return _persist(ev, lvl, x, int(hold), "fade")


# ---------------------------------------------------------------- modèle 3 : défense du VWAP

def vwap_defense(b: pd.DataFrame, vol_d: float = 0.3, frac: float = 0.8, hold: int = 15) -> np.ndarray:
    x = _features(b)
    o, h, l, c, rng, rank, vwap, sigma, spr = (
        x[k] for k in ("o", "h", "l", "c", "rng", "rank", "vwap", "sigma", "spr"))
    frac_above = _window_mean_prev((c > vwap).astype("float64"), TREND_BARS)
    frac_below = _window_mean_prev((c < vwap).astype("float64"), TREND_BARS)
    # rang moyen du repli (rang inconnu -> 1 : pas de contraste possible, pas d'événement)
    pullback_rank = _window_mean_prev(np.nan_to_num(rank, nan=1.0), PULLBACK_BARS)
    with np.errstate(invalid="ignore"):
        wide = sigma >= MIN_SPREADS * spr  # la séance s'écarte assez de sa valeur pour payer le coût
        ok = (x["since"] >= TREND_BARS) & wide & (rank - pullback_rank >= vol_d) & (rank >= MIN_RANK_DEF)
        buy = (ok & (frac_above >= frac) & (l <= vwap + TEST_SIGMA * sigma) & (c > vwap) & (c > o)
               & (c - l >= CLOSE_LOC_DEF * rng))
        sell = (ok & (frac_below >= frac) & (h >= vwap - TEST_SIGMA * sigma) & (c < vwap) & (c < o)
                & (h - c >= CLOSE_LOC_DEF * rng))
    ev = np.where(buy, 1, np.where(sell, -1, 0))
    lvl = np.where(buy, l, np.where(sell, h, np.nan))  # plus bas (plus haut) de la bougie de rejet
    return _persist(ev, lvl, x, int(hold), "defense")


MODELS = {
    "initiation (volume + déplacement)": (initiation, [
        {"vol_q": 0.95, "body_k": 1.5, "hold": 15},
        {"vol_q": 0.99, "body_k": 2.0, "hold": 20},
        {"vol_q": 0.90, "body_k": 1.0, "hold": 10},
    ]),
    "épuisement (climax aux bandes VWAP)": (exhaustion, [
        {"band_k": 2.0, "vol_q": 0.95, "hold": 15},
        {"band_k": 2.5, "vol_q": 0.99, "hold": 20},
        {"band_k": 1.5, "vol_q": 0.90, "hold": 10},
    ]),
    "défense du VWAP": (vwap_defense, [
        {"vol_d": 0.30, "frac": 0.8, "hold": 15},
        {"vol_d": 0.45, "frac": 0.9, "hold": 20},
        {"vol_d": 0.20, "frac": 0.7, "hold": 10},
    ]),
}
