# STRATEGIES.md — Stratégies testées

Chaque stratégie : règles 100 % codées, 3 à 5 paramètres libres, même code en backtest et en live
(`src/goldbot/strategies/`). Aucune n'est présumée rentable : tout se prouve sur les données Axi, frais compris.

## Méthode commune

- **Données** : barres M1 Axi du 22/07/2019 au 28/09/2026 (`docs/RESEARCH.md` annexe E).
- **Hors échantillon** : du 01/10/2025 au 28/09/2026, mis de côté. Il ne sert qu'une fois, pour la validation
  finale (`run_backtest.py --hors-echantillon`). Tout le développement se fait sur la période d'étude, du 22/07/2019
  au 30/09/2025.
- **Coûts** : spread de chaque barre (le plus petit de la minute), avec un plancher de 15 points ; 5 points de
  glissement défavorable par exécution au marché ou au SL ; swaps réels du compte (−61,6 / +40,5 points par lot et par
  nuit, triple le mercredi) ; pas de commission (compte Standard). Stress prévu : spread × 1,5 et glissement doublé.
- **Exécution** : signal à la clôture d'une barre, exécution à la barre suivante ; achat à l'ask, vente au bid ;
  SL et TP dans la même barre : SL.
- **Risque** : 0,5 % de l'equity par trade, arrondi vers le bas ; limites du CLAUDE.md §3.5 actives (perte
  journalière, positions, trades par jour, pertes d'affilée). En « mode recherche », l'arrêt total à −10 % est noté
  sans être appliqué, pour mesurer la stratégie sur tout l'historique.
- **Journal des essais** : chaque lancement de `run_backtest.py` ajoute une ligne à `reports/essais.csv`
  (paramètres et résultats), pour garder la trace de toutes les variantes testées.
- **Contrainte du petit compte** : avec 5 000 € et 0,5 % de risque, le lot minimal (0,01 lot = 1 once) limite la
  distance du SL à environ 28 $. Les signaux au SL plus large sont ignorés, jamais grossis.

## S1 — Cassure du range asiatique

**Idée** : la nuit européenne (session asiatique) est calme ; à l'ouverture de Londres, la liquidité arrive et le prix
sort souvent du range de la nuit.

**Règles** (heures de Londres) :
- range = plus haut et plus bas de 00:00 à 07:00 ; au moins la moitié des minutes doit avoir une barre ;
- filtre : taille du range entre `min_range_atr` et `max_range_atr` fois l'ATR journalier (14 jours précédents) ;
- à 07:00 : achat stop au plus haut + `buffer_atr` × ATR, vente stop au plus bas − `buffer_atr` × ATR ;
  un trade maximum par côté et par jour ;
- SL de l'autre côté du range ; TP à `tp_r` fois le risque ;
- ordres non déclenchés annulés à 11:00 ; positions fermées à 20:00.

**Paramètres libres** : `buffer_atr` = 0,05 ; `tp_r` = 1,5 ; `min_range_atr` = 0,15 ; `max_range_atr` = 0,8.
Valeurs choisies a priori, sans optimisation.

**Résultat sur la période d'étude** (22/07/2019 → 30/09/2025, mode recherche) :

| Trades | Profit factor | Espérance | Gagnants | Rendement | Drawdown max |
|---|---|---|---|---|---|
| 1 080 | 0,98 | −0,014 R | 42,9 % | −4,3 % | −12,8 % |

- Aucun avantage après frais : résultat proche de zéro sur 6 ans, années alternativement positives et négatives.
- Le compte réel aurait été arrêté le 27/01/2020 (drawdown de 10 %).
- **Verdict : rejetée en l'état.** Ne pas l'optimiser à l'aveugle : chercher d'abord une raison de marché
  (filtre de régime, horaires) sur la période d'étude, puis valider sans toucher au hors échantillon.

## Recherche du 29/09/2026 (période d'étude uniquement)

Environ 175 variantes examinées au total : 164 backtests complets journalisés dans `reports/essais_recherche.csv`,
plus une dizaine de tris rapides. Ce nombre compte : plus on essaie, plus on risque de tomber sur un résultat
chanceux. C'est pour cela que les réglages sont pris sur des zones stables et validés hors échantillon.

### Idées sans avantage après frais (rejetées)

Rendement futur net de frais après le signal, de 5 minutes à 4 heures, toutes sessions :

| Idée | Constat |
|---|---|
| Achat juste après la reprise quotidienne (18:00 à New York) | Faux effet : le premier prix de la journée est tiré vers le bas par un spread très large. Dès 18:05, plus rien après frais |
| Balayage du plus haut / plus bas de la veille puis retour (retournement) | Négatif dans les 3 sessions |
| Cassure du plus haut / plus bas de la veille (suivi) | Négatif |
| Grosse bougie d'une minute (suivi) | Négatif : ces bougies ont plutôt tendance à revenir, trop peu pour couvrir les frais |
| RSI(2) extrême en M15 (retour à la moyenne) | Négatif, les frais dominent |
| Cassure Donchian 20 en H1 (tendance) | Négatif |
| Dérive avant le fixing de Londres (10:30, 15:00) | Rien de significatif |

Sur l'or, entre 1 et 15 minutes, les mouvements prévisibles sont plus petits que le coût d'un aller-retour
(environ 0,30 $ par once : spread et glissement). Il faut viser des mouvements de plusieurs dollars.

### S3 — Cassure du range d'ouverture de New York (12 variantes)

- Range de 08:30 (chiffres américains) : négatif dans toutes les variantes (profit factor 0,78 à 0,91). Les chiffres
  provoquent des faux départs.
- Range de 09:30 (ouverture des actions) sur 30 minutes : légèrement positif (profit factor 1,02 à 1,06), trop faible
  pour être distingué du hasard. **Non retenue.**

### S7 — Momentum intraday (Londres et New York)

**Idée** (documentée sur les actions et les matières premières) : le mouvement depuis l'ouverture de la journée tend à
se prolonger quand il est net.

**Règles** : à `signal_time` (heure locale de la place), mouvement = prix − ouverture du jour de cotation. S'il dépasse
`min_move_atr` × ATR journalier : ordre au marché dans son sens, SL à `sl_atr` × ATR, pas de TP, sortie à `exit_time`.
Un trade par jour et par place.

**Carte de robustesse** (profit factor sur la période d'étude, 48 variantes par place) :
- Londres : **les 48 variantes sont gagnantes** (1,02 à 1,44), d'autant plus que le mouvement du matin est grand ;
- New York : signal à 09:30 ou 10:00 gagnant (1,05 à 1,39) ; à 10:30 à peine (1,02 à 1,08) ; à 11:00 perdant.
  L'effet se concentre sur l'ouverture de Wall Street.

**Réglages retenus** (milieu des zones stables, pas le maximum) : Londres 10:00 → 16:00 ; New York 09:30 → 16:00 ;
`min_move_atr` = 0,5 ; `sl_atr` = 0,3 ; pas de TP.

### S8 — Retour en session asiatique

Le même momentum à Tokyo est **perdant** dans les 12 variantes (0 à 2 années positives sur 7) : en séance asiatique,
peu liquide, le mouvement du matin a tendance à se retourner. L'inverse (vendre la hausse, acheter la baisse) est
gagnant dans 13 variantes sur 16. **Attention : hypothèse tirée des données** (on a inversé un résultat), donc plus
fragile : le hors échantillon tranchera.

Réglages retenus : signal à 11:00 heure de Tokyo, sortie à 15:00, `min_move_atr` = 0,25, `sl_atr` = 0,3, pas de TP.

### Portefeuille retenu (S8 Asie + S7 Londres + S7 New York)

Figé dans `config/settings.yaml` (section `strategies.session_momentum`) **avant** le test hors échantillon.

Période d'étude (22/07/2019 → 30/09/2025), risque de 0,5 % par trade :

| Coûts | Trades | Profit factor | Espérance | Rendement | Drawdown max | Années positives |
|---|---|---|---|---|---|---|
| Réels | 871 | 1,34 | +0,129 R | +62,9 % | −7,5 % | 6/7 |
| Spread × 1,5 | 871 | 1,30 | +0,120 R | +55,2 % | −8,6 % | 5/7 |
| Glissement 10 points | 871 | 1,28 | +0,116 R | +51,3 % | −8,6 % | 5/7 |
| Les deux | 871 | 1,24 | +0,107 R | +42,7 % | −9,5 % | 5/7 |

- Par stratégie : Asie +0,095 R (259 trades), Londres +0,159 R (203), New York +0,136 R (409).
- Corrélation des résultats quotidiens : Asie ≈ 0 avec les deux autres ; Londres / New York 0,28.
- Monte Carlo (1 000 tirages, ordre mélangé, 10 % des trades sautés) : drawdown médian −7,8 %, 95e centile −12,0 %,
  pire −16,7 %.
- 17 trimestres positifs sur 25, 9 semestres sur 13 ; les 5 meilleurs trades font 25 % du profit (critère < 30 %).
- Aucun arrêt au drawdown maximal de 10 % sur la période.

### Test hors échantillon (01/10/2025 → 28/09/2026), lancé une seule fois

Portefeuille figé (commit `cc4ebd9`), coûts réels, compte de 5 000 € (5 700 $) :

| Trades | Profit factor | Espérance | Rendement | Drawdown max | Signaux ignorés |
|---|---|---|---|---|---|
| 111 | 1,54 | +0,217 R | +10,3 % | −4,8 % | 98 à 104 |

Détail par stratégie : Londres +0,39 R (27 trades), New York +0,32 R (35), **Asie −0,06 R (44)**.
Avec spread × 1,5 et 10 points de glissement : profit factor 1,57, +10,6 %.

Lecture honnête :
- **S8 (Asie) échoue** hors échantillon : désactivée (`enabled: false`). C'était l'hypothèse la plus fragile.
- **S7 (Londres, New York) tient**, mais le chiffre ci-dessus est flatté par la taille du compte. En 2026, l'or est
  très volatil (vers 4 000 $) : le SL de 0,3 ATR dépasse souvent 28 $, et avec 5 000 € le lot minimal (0,01) ferait
  alors risquer plus de 0,5 %. Le bot ignore ces signaux (règle : jamais d'arrondi du risque vers le haut). Or ces
  jours très agités ont été plutôt perdants cette année.
- Sans signal ignoré (compte simulé plus gros), Londres + New York donnent sur la même année :

| Compte | Trades | Ignorés | Profit factor | Espérance | Rendement | Drawdown |
|---|---|---|---|---|---|---|
| 5 000 € | 63 | 58 | 1,82 | +0,349 R | +9,7 % | −3,9 % |
| 10 000 € | 115 | 6 | 1,24 | +0,071 R | +5,2 % | −4,3 % |
| 20 000 € | 121 | 0 | 1,14 | +0,067 R | +3,8 % | −5,6 % |

  Même période d'étude pour Londres + New York seuls : 614 trades, profit factor 1,29 à 1,31, +0,141 R ; les
  5 meilleurs trades font 32 à 34 % du profit (critère 30 % : limite).

**Verdict** : S7 Londres + New York montre un avantage réel mais **modeste** (profit factor d'environ 1,15 à 1,3
selon la période, sans filtre). Il est suffisant pour passer en **démo**, qui servira de test « en avant » sur des
données futures, mais pas pour promettre des gains. Environ 10 trades par mois au rythme actuel.

## Expérience de scalping 5 s (demande du 29/09/2026)

Point de départ **fourni par l'utilisateur**, pas une stratégie validée : il sert à observer de vraies ouvertures
et clôtures sur le compte démo (`scripts/run_scalp.py`, README §9). Code : `src/goldbot/scalping/` (le même moteur
sert au rejeu et au bot démo). Réglages : section `scalping` de `config/settings.yaml`, figés pendant la collecte.

Règles :
- bougies de 5 s au prix médian (bid + ask) / 2, heures de cotation seulement ;
- signal : clôture au-delà du plus haut (ou plus bas) des 60 s précédentes, bougie de signal exclue (au moins
  6 bougies dans la fenêtre), puis une deuxième clôture au-delà du même niveau ; tendance M1 sur barres clôturées
  EMA20 > EMA50 pour acheter, inverse pour vendre ; le côté est réarmé quand une clôture revient dans le range ;
  10 s au moins entre deux entrées ;
- exécution au prix disponible au moment de la décision (ask à l'achat, bid à la vente) ;
- stop côté serveur derrière la structure des 30 dernières secondes (+ demi-spread + 5 points), entre 0,50 $ et
  6 $, au-delà des distances minimales du broker ; objectif à 1,2 fois le stop ; refus si l'objectif est
  inférieur à 3 fois le coût estimé (spread + 2 × 5 points de glissement + commission) ;
- sortie forcée à 120 s, même en perte ; pas d'entrée si le marché ferme avant ; signal périmé (plus de 5 s
  après sa bougie) refusé ;
- 0,1 % de risque par trade (signal ignoré si le lot minimal dépasse ce budget), 3 positions, 0,3 % de risque
  cumulé, plus d'entrée pour la journée à −1 %.

### Rejeu sur les ticks Axi (`scripts/backtest_scalp.py`)

Du 30/08 au 28/09/2026 (21 jours de cotation, 7,7 M ticks), compte de 5 000 €, mêmes refus qu'en direct :

| Hypothèse | Trades | Gagnants | Gain / perte moyens | Profit factor | Net | Pire baisse | Jours positifs |
|---|---|---|---|---|---|---|---|
| Prix exécutables, sans glissement | 2 281 | 43 % | +2,98 € / −2,97 € | 0,75 | −983 € | −21,5 % | 0 sur 21 |
| + 10 points de glissement | 767 | 34 % | +2,70 € / −3,39 € | 0,42 | −1 000 € | −20,0 % | 0 sur 21 |
| Au plus une entrée par minute | 1 828 | 41 % | +3,01 € / −3,03 € | 0,70 | −978 € | −20,7 % | 0 sur 21 |
| Sans la limite de −1 % par jour | 11 531 | 43 % | +2,14 € / −2,07 € | 0,78 | −3 042 € | −61,8 % | 0 sur 21 |

Sorties (prix exécutables) : objectif 477, stop 829, durée maximale 975. Durée moyenne : 83 s.

Lecture honnête :
- **La perte n'est pas du bruit** : −0,43 € par trade en moyenne, écart-type 3,35 €, soit environ 6 erreurs-types
  sous zéro. Aucune journée positive sur 21 : la limite de −1 % est atteinte chaque jour et plafonne les dégâts.
- Gain moyen ≈ perte moyenne (objectif à 1,2 fois le stop, moins le spread) : il faudrait plus de 50 % de
  gagnants, on en a 43 %. Sur l'or à l'échelle de la minute, la cassure des 60 s ne se prolonge pas assez : 43 %
  des trades finissent à la durée maximale sans avoir touché ni stop ni objectif.
- Le coût (spread d'environ 0,16 $ plus le glissement) pèse lourd face au mouvement typique de 2 minutes :
  10 points de glissement font tomber le profit factor de 0,75 à 0,42.

**Verdict** : rejeté comme stratégie. Gardé comme **expérience démo** à la demande de l'utilisateur, pour mesurer
en réel le glissement, l'écart entre résultat estimé et exécuté, et vérifier la chaîne d'exécution. Tant que la
cadence acceptée par Axi n'est pas confirmée, au plus un ordre par minute part au broker ; les autres signaux
acceptés sont simulés (bilan 2).
