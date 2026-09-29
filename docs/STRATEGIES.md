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

## Expérience de scalping 5 s (demandes du 29/09/2026)

Points de départ **fournis par l'utilisateur**, pas des stratégies validées : l'expérience sert à observer de
vraies ouvertures et clôtures sur le compte démo (`scripts/run_scalp.py`, README §9). Code :
`src/goldbot/scalping/` ; le même moteur (`engine.py`) et les mêmes règles d'entrée (`policy.py`) servent au rejeu
et au bot démo. Réglages : section `scalping` de `config/settings.yaml`, figés pendant la collecte.

**Versions.** Chaque stratégie est identifiée par son nom, un numéro de version et l'empreinte de ses réglages
(8 caractères, calculée sur ses règles et les réglages communs) : par exemple `cassure v1 · a496a8fc`. Chaque
signal enregistre stratégie, version et empreinte ; la table `versions` de `data/scalp.sqlite` garde le JSON
complet des réglages. Modifier un réglage change l'empreinte (et le bot refuse de repartir en cours de collecte) ;
modifier une règle dans le code impose d'augmenter `version`.

### Règles communes (comparaison à risque égal)

- Bougies de 5 s au prix médian (bid + ask) / 2, heures de cotation seulement.
- Exécution au prix disponible au moment de la décision (ask à l'achat, bid à la vente).
- Stop côté serveur derrière la structure du signal (+ demi-spread + 5 points), entre 0,50 $ et 6 $, au-delà des
  distances minimales du broker ; objectif à 1,2 fois le stop ; refus si l'objectif est inférieur à 3 fois le coût
  estimé (spread + 2 × 5 points de glissement + commission).
- Sortie forcée à 120 s, même en perte ; pas d'entrée si le marché ferme avant ; signal périmé (plus de 5 s après
  sa bougie) refusé.
- 0,1 % de risque par trade (signal ignoré si le lot minimal dépasse ce budget), 5 trades ouverts et 0,5 % de
  risque cumulé au plus, 5 entrées au plus sur 60 s glissantes, 5 s entre deux entrées (une par bougie), plus
  d'entrée pour la journée à −1 %, marge libre après l'ordre d'au moins 50 % de l'equity.
- Nouveaux signaux, fractionnement et doublons (`policy.py`) : une **occasion** a une clé (niveau cassé pour la
  cassure, départ et sommet de l'impulsion pour l'impulsion-repli) ; la même clé ne peut pas être prise deux fois
  tant que son trade est ouvert. Un trade trop gros pour un seul ordre (volume maximal par ordre : 20 lots chez
  Axi) est **fractionné** en ordres égaux `tag#1`, `tag#2`… qui partagent un seul budget de risque et comptent
  pour une seule entrée. Le **même signal** revu (relance, réponse perdue du broker) n'est jamais renvoyé ; une
  position en double chez le broker est fermée ; les deux cas sont comptés (« doublons accidentels évités »).

### Stratégie « cassure » v1

Clôture au-delà du plus haut (ou plus bas) des 60 s précédentes, bougie de signal exclue (au moins 6 bougies dans
la fenêtre), puis une deuxième clôture au-delà du même niveau ; tendance M1 sur barres clôturées EMA20 > EMA50 pour
acheter, inverse pour vendre ; le côté est réarmé quand une clôture revient dans le range. Stop derrière le creux
(ou sommet) des 30 dernières secondes.

### Stratégie « impulsion-repli » v1

Principe public de GOLD Scalper PRO (« impulsion puis correction », `docs/RESEARCH.md` annexe F), réécrit en règles
numériques ici. **Ce n'est pas le code de ce produit commercial**, qui n'est pas public ; seules ses descriptions
publiques ont été lues, et ses résultats annoncés n'ont pas pu être vérifiés.

Achat (vente symétrique) :
1. **Impulsion** : entre le plus bas des 60 dernières secondes et le plus haut de la bougie qui vient de clôturer,
   au moins 1 ATR M1 (ATR 14 sur barres M1 clôturées). Le départ est le plus récent des plus bas égaux.
2. **Repli** : après le sommet, le creux descend de 30 % à 70 % de l'impulsion. Plus profond, l'impulsion est
   abandonnée ; un nouveau sommet avant un repli suffisant prolonge l'impulsion.
3. **Reprise** : une bougie clôture au-dessus du plus haut de la bougie précédente, au plus 60 s après le sommet.
   Entrée à l'ask ; stop sous le creux du repli.
4. Une seule entrée par impulsion ; l'impulsion suivante doit partir d'un point situé après le sommet précédent.

Paramètres libres (5) : `impulse_atr` 1,0, `impulse_max_s` 60, `retrace_min` 0,3, `retrace_max` 0,7,
`pullback_max_s` 60. Valeurs fixées **avant** le rejeu, par raisonnement, pas par optimisation.

### Comparaison à risque égal sur les ticks Axi (`scripts/backtest_scalp.py`)

Du 30/08 au 28/09/2026 (21 jours de cotation, 7,7 M ticks), compte de 5 000 €, 0,1 % de risque par trade, mêmes
sorties et mêmes règles de compte pour les deux stratégies. Montants en euros ; E[R] = résultat moyen par trade en
multiples du risque ; t = résultat moyen divisé par son erreur-type.

Avec les règles du compte (limite de −1 % par jour comprise) :

| Stratégie | Glissement | Trades | Gagnants | Gain / perte moyens | Profit factor | Net | E[R] | t | Jours positifs |
|---|---|---|---|---|---|---|---|---|---|
| cassure v1 · a496a8fc | 0 | 2 177 | 42 % | +2,96 / −2,96 | 0,73 | −987 | −0,120 | −6,4 | 0 sur 21 |
| impulsion-repli v1 · adfe954a | 0 | 1 378 | 39 % | +4,11 / −3,82 | 0,69 | −994 | −0,184 | −6,6 | 0 sur 21 |
| les deux ensemble | 0 | 1 043 | 37 % | +3,12 / −3,33 | 0,55 | −989 | −0,245 | −8,8 | 0 sur 21 |
| cassure v1 | +10 points | 712 | 33 % | +2,65 / −3,40 | 0,38 | −1 012 | −0,366 | −11,4 | 0 sur 21 |
| impulsion-repli v1 | +10 points | 551 | 32 % | +3,66 / −4,41 | 0,39 | −999 | −0,457 | −10,5 | 0 sur 21 |
| les deux ensemble | +10 points | 664 | 32 % | +3,03 / −3,75 | 0,38 | −1 045 | −0,407 | −11,3 | 0 sur 21 |

La limite journalière arrête chaque jour les entrées vers −1 % : le net (environ −1 000 €, soit 20 jours × 1 %)
reflète surtout cette limite. Pour juger les signaux eux-mêmes, sans limite journalière :

| Stratégie | Glissement | Trades | Gagnants | Profit factor | E[R] | t | Durée moyenne |
|---|---|---|---|---|---|---|---|
| cassure v1 | 0 | 11 678 | 43 % | 0,78 | −0,104 | −11,3 | 81 s |
| impulsion-repli v1 | 0 | 7 751 | 39 % | 0,68 | −0,189 | −15,2 | 44 s |
| cassure v1 | +10 points | 8 521 | 39 % | 0,60 | −0,229 | −19,1 | 78 s |
| impulsion-repli v1 | +10 points | 6 413 | 38 % | 0,51 | −0,363 | −22,8 | 42 s |

**Diagnostic sans aucun coût** (exécution au prix médian, spread et glissement nuls, filtre de coût coupé ; ce
n'est pas un scénario réalisable, il mesure seulement si les signaux prédisent la direction) :

| Stratégie | Trades | Gagnants | Profit factor | E[R] | t |
|---|---|---|---|---|---|
| cassure v1 | 12 865 | 48 % | 0,98 | −0,010 | −1,1 |
| impulsion-repli v1 | 7 871 | 46 % | 0,90 | −0,057 | −4,5 |

**Sensibilité d'impulsion-repli** (sans limite journalière, prix exécutables ; toutes les variantes essayées sont
listées, conscience du multiple testing) :

| Impulsion | Repli 30-70 % | Repli 38,2-61,8 % | Repli 20-50 % |
|---|---|---|---|
| 1,0 ATR | PF 0,68 · 7 751 trades | PF 0,66 · 4 928 | PF 0,71 · 6 999 |
| 1,5 ATR | PF 0,69 · 3 097 | PF 0,65 · 1 893 | PF 0,71 · 3 105 |
| 2,0 ATR | PF 0,71 · 1 073 | PF 0,72 · 631 | PF 0,73 · 1 161 |

Lecture honnête :
- **Les deux stratégies perdent, nettement** (t entre −6 et −23 selon le scénario) : aucune journée positive sur
  21 avec les règles du compte, dans aucun scénario.
- **La cassure ne prédit pas la direction** : sans aucun coût, elle est à l'équilibre (profit factor 0,98, t −1,1).
  Le spread (environ 0,16 $ pour des stops de 0,5 à 1,5 $) en fait une perte.
- **L'impulsion-repli fait pire que le hasard** : elle perd même sans aucun coût (profit factor 0,90, t −4,5). À
  l'échelle de quelques secondes, la « reprise » après un repli a plutôt tendance à se retourner. Aucune des
  9 variantes (impulsion de 1 à 2 ATR, trois zones de repli) ne dépasse un profit factor de 0,73 : le problème
  n'est pas un réglage mal choisi.
- Ses trades sont plus courts (44 à 47 s) : ils touchent plus souvent le stop ou l'objectif que la durée maximale.
- Combiner les deux n'aide pas : les signaux perdants se cumulent et la limite journalière arrive plus vite.

**Verdict** : aucune des deux n'est une stratégie rentable ; l'impulsion-repli v1 est moins bonne que la cassure.
Les deux restent dans l'expérience démo **à la demande de l'utilisateur**, pour mesurer en réel le glissement,
l'écart entre résultat estimé et exécuté, et la tenue de la chaîne d'exécution avec plusieurs entrées par minute.
Quelques clôtures en bénéfice sur le compte démo ne changeront pas ce verdict : il faudrait des centaines de trades
et un résultat nettement positif, confirmé sur des données nouvelles, pour le remettre en cause.

### Analyse des erreurs et apprentissage en avançant (`scripts/analyse_scalp.py`)

Demande de l'utilisateur (29/09/2026) : une stratégie « qui s'auto-améliore » et « comprend ses erreurs ».
Protocole, fixé avant de regarder les résultats :
- chaque signal jouable (plan accepté) est simulé seul, sans les limites du compte, résultat en R (mouvement
  divisé par la distance du stop, spread compris) ; meilleure et pire excursion pendant le trade ;
- 7 contextes à l'entrée : heure de Paris, sens, stop / ATR M1, coût / risque, ATR M1, entrée au-delà du niveau
  clé, trades du même sens déjà ouverts ;
- **apprentissage en avançant** : chaque jour, un filtre est appris sur les jours précédents seulement (résultat
  moyen de chaque tranche, rapproché de la moyenne générale quand la tranche compte peu de trades, écarts
  additionnés), puis appliqué au jour suivant ; un signal n'est pris que si son résultat attendu est positif.
  16 jours testés ainsi sur les 21.

| Stratégie | Signaux | Tous les signaux | 16 jours testés, sans filtre | Avec le filtre appris |
|---|---|---|---|---|
| cassure v1 | 12 812 | −0,105 R (t −13,2) | −0,107 R, 0 jour positif | 509 trades, −0,147 R (t −4,2), 2 jours positifs |
| impulsion-repli v1 | 7 992 | −0,195 R (t −16,3) | −0,194 R, 0 jour positif | 102 trades, −0,119 R (t −1,2), 7 jours positifs |

Avec 10 points de glissement : cassure −0,221 R sans filtre, −0,164 R avec ; impulsion-repli −0,359 R et −0,275 R.

Lecture :
- **Aucune tranche d'aucun contexte n'est positive**, même mesurée sur toute la période : heure, sens, taille du
  stop, volatilité, coût, entrée tardive, trades groupés dans le même sens. Les moins mauvaises restent entre
  −0,02 et −0,07 R, sur de petits effectifs.
- **L'apprentissage n'invente pas d'avantage.** Pour la cassure, le filtre appris fait pire que sans filtre : il
  retient des tranches favorables par hasard, qui ne le restent pas le jour suivant. Pour l'impulsion-repli, il
  réduit la perte sans la rendre positive, sur trop peu de trades pour conclure.
- **Les sorties ne sauveraient pas les perdants** : seuls 19 % (cassure) et 24 % (impulsion-repli) des trades
  perdants ont d'abord gagné 0,5 R, et 3 à 5 % un R entier ; remonter le stop au prix d'entrée ou suivre le prix
  n'y changerait presque rien. À l'inverse, 30 à 42 % des gagnants ont d'abord perdu 0,5 R : un stop plus serré
  couperait surtout des gagnants.
- À cet horizon (quelques secondes à 2 minutes), le spread pèse 10 à 30 % du risque de chaque trade et les
  signaux ne prédisent pas la direction : **plus de trades, c'est plus de pertes**. L'auto-apprentissage n'est
  donc pas ajouté au bot démo. L'outil reste disponible pour tester de nouvelles idées sans se tromper soi-même.

### Apprentissage à chaque trade (v2, `scripts/analyse_scalp.py`)

Demande de l'utilisateur : que le bot comprenne son erreur « à chaque trade » et la corrige au trade suivant, par
exemple « si les achats perdent en ce moment, chercher des ventes, et inversement si ça se retourne ».
Protocole (`src/goldbot/scalping/learning.py`, fixé avant les résultats) :
- chaque signal est simulé avec 18 **variantes** : stop x1, x1,5 ou x2 la distance de base, objectif 0,8, 1,2 ou
  2 R, dans le sens du signal ou **à l'envers** (vendre quand le signal dit d'acheter) ;
- après **chaque** trade simulé fermé, le score de sa variante est mis à jour : moyenne des résultats en R, les
  trades récents pesant plus (demi-vie de 20, 100 ou 500 trades) ; scores communs à la stratégie, ou **séparés
  pour les achats et les ventes** ;
- au signal suivant : la variante qui marche le mieux en ce moment (jugée après 20 trades), ou pas de trade si
  toutes perdent (sinon, en mode « trade toujours », la moins mauvaise) ;
- chaque décision n'utilise que des trades déjà fermés : rien du futur. 24 réglages essayés par stratégie, tous
  listés ci-dessous (meilleur et pire) : le meilleur des 24 est flatté par le hasard.

| Stratégie | Règles v1 | Meilleure variante fixe, connue après coup | 24 réglages d'apprentissage | Ton idée : achats et ventes séparés, inversion permise |
|---|---|---|---|---|
| cassure | −0,105 R | −0,048 R (stop x2) | de −0,021 R (504 trades) à −0,102 R | de −0,044 à −0,094 R |
| impulsion-repli | −0,195 R | −0,067 R (stop x2, objectif 2 R, à l'envers) | de −0,066 R à −0,141 R | de −0,066 à −0,095 R |

Lecture :
- **L'apprentissage réduit toujours la perte**, surtout en choisissant des stops plus larges (le spread pèse alors
  moins dans chaque trade) et, pour l'impulsion-repli, en jouant les signaux à l'envers.
- **Aucun réglage ne gagne**, même le meilleur des 48 : la variante fixe la meilleure, connue seulement après
  coup, perd encore. L'apprentissage ne peut pas faire mieux que la meilleure façon de jouer ces signaux.
- **« Si les achats perdent, vendre »** : sur 21 jours, le sens qui a perdu récemment ne prédit pas assez la
  direction des minutes suivantes pour battre le spread.

**Mis dans le bot démo quand même**, à la demande de l'utilisateur, comme version « + apprentissage v1 » (scores
séparés achats / ventes, inversion permise, demi-vie de 100 trades, jugé après 20, pas de trade quand toutes les
variantes perdent). Rejeu, signaux simulés seuls : cassure −0,087 R sur 4 129 trades, impulsion-repli −0,066 R sur
2 770 trades, contre −0,105 R et −0,195 R sans apprentissage. **Perd moins, mais perd.**

Rejeu avec les règles du compte (compte de 5 000 €, 0,1 % par trade, limite de −1 % par jour ;
`scripts/backtest_scalp.py`, option `--sans-apprentissage` pour v1) :

| Prix exécutables | v1, sans apprentissage | v2, + apprentissage v1 |
|---|---|---|
| cassure seule | 2 177 trades, PF 0,73, −987 € | 3 041 trades, PF 0,80, −819 €, 1 jour positif sur 21 |
| impulsion-repli seule | 1 378 trades, PF 0,69, −994 € | 2 143 trades, PF 0,83, −627 €, 1 jour positif sur 21 |
| les deux ensemble (comme en démo) | 1 043 trades, PF 0,55, −989 € | 3 363 trades, PF 0,81, −958 €, 0 jour positif sur 21 |
| les deux ensemble, +10 points de glissement | 664 trades, PF 0,38, −1 045 € | 1 180 trades, PF 0,52, −996 € |

Le total dépend surtout de la limite journalière (environ −1 % par jour sur 20 jours) : v2 perd moins par trade,
donc trade plus longtemps avant d'atteindre la limite. Aucune journée n'est positive quand les deux tournent
ensemble.

### Cadence liée au bénéfice (« + cadence v1 », demande du 29/09/2026)

Demande de l'utilisateur : « augmente le nombre de trades par minute, pas de limite tant qu'il arrive à générer du
bénéfice, juste qu'il prenne intelligemment ».

**D'abord, la limite par minute freine-t-elle le bot ?** Rejeu des 4 semaines (les deux stratégies ensemble,
prix exécutables, compte de 5 000 €, 0,1 % par trade, limite de −1 % par jour) :

| Limites | v2 + apprentissage | v1 sans apprentissage |
|---|---|---|
| actuelles : 5 entrées/min, 5 positions, 0,5 % de risque ouvert | 3 363 trades, PF 0,81, −958 € | 1 043 trades, PF 0,55, −989 € |
| 10/min, 10 positions, 1 % | 3 467 trades, PF 0,81, −957 € | 948 trades, PF 0,51, −993 € |
| 30/min, 20 positions, 2 % | 3 467 trades, PF 0,81, −957 € | 948 trades, PF 0,51, −993 € |
| sans limite (1 000/min, 1 000 positions) | 3 467 trades, PF 0,81, −957 € | 948 trades, PF 0,51, −993 € |

Non : environ 160 trades par jour sont déjà pris, et ce qui retient le bot, ce sont les signaux eux-mêmes et
l'apprentissage, qui refuse 13 905 signaux quand toutes les variantes perdent. Lever toutes les limites n'ajoute
que 3 % de trades, sans rien changer au résultat. Supprimer aussi la perte maximale du jour **double la perte**
(6 457 trades, −1 757 €) : cette limite protège le compte.

**Règle ajoutée** (`policy.py`, classe `Cadence`, même code dans le rejeu et le bot démo) : après chaque trade
fermé, si les 30 derniers trades sont en bénéfice net (frais compris) **et que ce trade est gagnant**, les limites
de base (entrées par minute, délai entre entrées, positions, risque ouvert) passent au palier suivant, x2 puis x4
puis x6, au plus un palier par série complète de 30 trades depuis le dernier passage en perte. Dès que les 30
derniers trades sont en perte nette, retour immédiat à la base. Plafonds absolus : une entrée par bougie de 5 s,
soit 12 par minute (les signaux d'une même bougie sont décidés au même instant ; limite des requêtes au serveur ;
Axi n'accepte pas le HFT), et un risque ouvert jamais au-delà de la perte maximale du jour (1 %). Le risque de
chaque trade (0,1 %) ne change jamais : l'activité augmente après des gains, jamais après des pertes. Ce n'est donc
pas une logique de récupération (règle 4). **Budget du jour** (ajouté après la relecture critique) : une entrée est
refusée si, tous les stops touchés, la journée dépasserait sa perte maximale (réalisé du jour moins risque ouvert,
sans compter les gains latents qui peuvent disparaître). Au pire, une journée perd donc environ −1 %, plus le
glissement éventuel sur les stops.

Rejeu avec cette règle (avant l'ajout du budget du jour ; avec lui, réglages démo d'alors : 3 165 trades, PF 0,81,
−892 €) :

| Réglage | Trades | Résultat | Trades par palier (résultat moyen) |
|---|---|---|---|
| fenêtre de 30 trades (retenu) | 3 365 | PF 0,81, −957 € | base 2 755 (−0,34 €), x2 566 (−0,19 €), x4 44 (−0,98 €) |
| fenêtre de 10 trades | 3 391 | PF 0,81, −955 € | base 2 441, x2 702, x4 179, x6 69 |
| fenêtre de 30, sans perte maximale du jour | 6 457 | PF 0,80, −1 757 € | base 5 121, x2 1 110, x4 156, x6 70 |

Lecture : la cadence monte après des séries gagnantes, mais les trades pris aux paliers supérieurs perdent aussi
en moyenne. Le résultat est inchangé (−957 € contre −958 €). La règle fait ce qui est demandé (plus de trades
seulement tant que les derniers rapportent), mais **elle ne crée pas d'avantage** : une série de 30 trades
gagnante sur ces stratégies relève surtout du hasard. Elle est mise en démo à la demande de l'utilisateur, sans
être présentée comme rentable.

### Trades démo du 29/09 et corrections testées (sorties v2)

**Diagnostic** (capture de l'utilisateur, 31 trades de 18:22 à 19:01 heure serveur, lisibles : −13,17 € ; le compte
affiche −22,70 € avec des trades hors capture) :
- 10 trades à l'objectif (+47,81 €), 15 au stop (−64,82 €), 5 à la durée maximale (+2,60 €) : un gain rapporte
  à peu près ce que coûte une perte (objectif 1,2 x le stop), il faut donc plus de 45 % de gagnants ; il y en a 40 % ;
- stops serrés : les 12 trades au stop de 1,30 $ ou moins font −19,25 € (4 gagnants) ; 5 stops touchés en moins de
  20 s (le bruit du prix, pas une erreur de direction) ;
- achats contre la baisse : 5 achats, 4 perdants, −17,02 € ; les ventes (26) font +3,85 €, avec des pertes
  d'affilée quand le prix tourne en rond ;
- glissement sur 3 stops sur 15 : jusqu'à 0,44 $ au-delà du stop (le rejeu en suppose 0,05 $).

**Corrections testées** sur les 4 semaines de ticks Axi (les deux stratégies, apprentissage et cadence, compte de
5 000 €, 0,1 % par trade). Toutes les variantes sont listées : 21 essais sur les mêmes données, la meilleure est
donc flattée par le hasard.

| Variante | Trades | PF | Résultat | Espérance | +10 pts de glissement |
|---|---|---|---|---|---|
| démo d'avant (stop ≥ 0,50 $, objectif 1,2 x, 120 s) | 3 165 | 0,81 | −892 € | −0,071 R | PF 0,51, −929 € |
| stop ≥ 1 $ / 1,50 $ / 2 $ | 2 861 / 2 699 / 2 725 | 0,78 / 0,80 / 0,86 | −904 / −680 / −460 € | −0,083 / −0,075 / −0,046 R | PF 0,60 / 0,64 / 0,67 |
| stop ≥ 3 $ / 4 $ | 796 / 301 | 0,85 / 0,79 | −156 / −98 € | −0,063 / −0,074 R | PF 0,74 / 0,71 |
| au plus 1 / 2 trades ouverts par sens | 2 860 / 3 716 | 0,80 / 0,83 | −880 / −951 € | −0,081 / −0,064 R | PF 0,60 / 0,56 |
| pause 60 s / 180 s dans le sens d'un stop touché en moins de 20 s | 3 281 / 3 974 | 0,80 / 0,84 | −958 / −958 € | −0,074 / −0,062 R | PF 0,51 / 0,52 |
| pause 15 min / 60 min après 3 pertes d'affilée dans un sens | 2 801 / 2 086 | 0,79 / 0,83 | −893 / −554 € | −0,085 / −0,068 R | PF 0,62 / 0,61 |
| apprentissage plus rapide (demi-vie 20, jugé après 10) | 3 051 | 0,80 | −988 € | −0,084 R | PF 0,55 |
| objectif 3 x le stop, 120 s, sans apprentissage | 1 468 | 0,68 | −1 004 € | −0,173 R | PF 0,31 |
| objectif 3 x le stop, 10 min, sans apprentissage | 1 494 | 0,77 | −1 031 € | −0,178 R | PF 0,53 |
| objectif 3 x le stop, 10 min, apprentissage (objectifs 2 / 3 / 4 R) | 3 469 | 0,90 | −795 € | −0,054 R | PF 0,82 |
| **stop ≥ 2 $ + objectif 3 x, 10 min, apprentissage 2 / 3 / 4 R (retenu)** | 2 218 | **0,905** | −428 € | −0,055 R | **PF 0,84**, −640 € |
| stop ≥ 2 $ + pause 60 min après 3 pertes | 1 686 | 0,80 | −404 € | −0,066 R | PF 0,68 |
| retenu + pause 60 min après 3 pertes | 1 442 | 0,82 | −542 € | −0,104 R | PF 0,76 |

Lecture :
- **Rien ne rend le scalper gagnant.** Toutes les variantes perdent, avec ou sans glissement.
- **Un stop plus large aide** (le spread et le bruit pèsent moins dans chaque trade), jusqu'à 2 $ ; au-delà, il
  reste trop peu de signaux pour que la perte par trade baisse encore.
- **Objectif à 3 x le stop (« 1 gain = 3 pertes »)** : seul, c'est pire (le prix revient au stop plus souvent
  qu'il n'atteint 3 fois la distance : 25 à 35 % de gagnants). Avec l'apprentissage et 10 minutes pour l'atteindre,
  c'est mieux que l'objectif de 1,2 x (PF 0,90 contre 0,81).
- **Contrer les pertes d'affilée dans un sens** : une pause de 60 minutes après 3 pertes réduit la perte seule,
  mais ajoutée aux réglages retenus elle l'aggrave (−542 € contre −428 €) ; elle n'est donc pas activée. Le choix
  « acheter ou vendre » d'après les derniers résultats reste fait par l'apprentissage (scores séparés par sens).
- Limiter les trades par sens, faire une pause après un stop rapide, apprendre plus vite : sans effet utile.

**Retenu pour la démo** (sorties v2, « + apprentissage v2 ») : stop d'au moins 2 $, objectif 3 x le stop,
10 minutes au plus, variantes apprises avec des objectifs de 2, 3 ou 4 R. Meilleur profit factor et meilleure
tenue au glissement des 21 essais, mais **perdant** : environ −0,055 R par trade au rejeu. Les options « trades
par sens » et « pauses » restent dans le code, désactivées.

Rejeu officiel de ces réglages (`scripts/backtest_scalp.py`), par stratégie :

| Prix exécutables | Trades | Gagnants | Gain moyen / perte moyenne | PF | Résultat | Espérance (t) |
|---|---|---|---|---|---|---|
| cassure seule | 2 437 (118 par jour) | 41 % | +4,66 / −3,36 € | 0,97 | −145 € | −0,018 R (−0,6) |
| impulsion-repli seule | 416 (20 par jour) | 28 % | +7,97 / −3,67 € | 0,84 | −175 € | −0,120 R (−1,5) |
| les deux ensemble | 2 218 (107 par jour) | 39 % | +4,74 / −3,29 € | 0,90 | −428 € | −0,055 R (−2,0) |
| cassure seule, +10 points | 1 943 | 40 % | +4,53 / −3,41 € | 0,87 | −531 € | −0,072 R (−2,6) |

Le gain moyen dépasse maintenant la perte moyenne (+4,66 € contre −3,36 € pour la cassure). **La cassure seule est
proche de l'équilibre avant glissement** (t de −0,6 : impossible à distinguer de zéro, ni gain ni perte
démontrés) et perdante avec 10 points de glissement. L'impulsion-repli perd dans toutes ses versions depuis le
début : **elle est désactivée en démo** (`pullback.enabled: false`, code conservé). Les prochains jours de démo,
sur des données jamais vues et avec le vrai glissement, diront si ce classement tient.

**TP fixe en dollars et lots plus gros** (question de l'utilisateur, 29/09 au soir : « le TP est loin avec un petit
lot ; un prix fixe avec un plus gros lot ? »). Rejeu de la cassure seule, sans apprentissage pour isoler l'effet
(6 essais de plus, total 27) :

| Objectif | Gagnants | Gain moyen / perte moyenne | PF | Résultat | +10 pts |
|---|---|---|---|---|---|
| TP fixe 1 $ | 68 % | +1,27 / −3,52 € | 0,76 | −812 € | PF 0,63 |
| TP fixe 2 $ | 55 % | +2,53 / −3,53 € | 0,86 | −688 € | PF 0,75 |
| TP fixe 3 $ | 46 % | +3,68 / −3,51 € | 0,89 | −570 € | PF 0,82 |
| TP fixe 5 $ | 38 % | +5,23 / −3,52 € | 0,92 | −411 € | PF 0,82 |
| 3 x le stop (sans apprentissage) | 35 % | +6,18 / −3,57 € | 0,95 | −234 € | PF 0,86 |
| réglages démo (3 x le stop, apprentissage) | 41 % | +4,66 / −3,36 € | 0,97 | −145 € | PF 0,87 |

Un TP proche fait gagner plus souvent, mais le gain moyen fond plus vite que la fréquence ne monte : plus il est
proche, plus la perte est grande. Les réglages démo restent les moins mauvais. La taille du lot ne change rien à
ce classement : elle multiplie gains et pertes par le même nombre (même espérance en R), et plusieurs positions
sur le même signal reviennent à un seul lot plus gros. Tant que l'espérance est négative, un lot plus gros fait
seulement perdre plus vite ; il ne s'augmente (en % de risque, plafond dur 1 %) qu'une fois un avantage démontré.

**« Fermer dès que c'est en profit »** (question de l'utilisateur : des bots vus ailleurs gardent leurs positions
quelques secondes et ferment dès le premier gain). Rejeu de la cassure seule, stop d'au moins 2 $, 0,1 % par trade
(3 essais de plus) :

| Sortie | Gagnants | Gain moyen / perte moyenne | Durée moyenne | PF | Résultat |
|---|---|---|---|---|---|
| fermer dès +0,30 $ | 82 % | +0,40 / −3,64 € | 36 s | 0,52 | −499 € (arrêt à −10 % au 12e jour) |
| fermer dès +0,50 $ | 78 % | +0,66 / −3,62 € | 48 s | 0,65 | −498 € (arrêt au 12e jour) |
| fermer dès +0,50 $, 30 s au plus | 58 % | +0,61 / −1,55 € | 19 s | 0,55 | −503 € (arrêt au 12e jour) |

Beaucoup de trades gagnants et courts, mais chaque gain rapporte environ 9 fois moins qu'une perte ne coûte : il
faudrait plus de 90 % de gagnants juste pour être à zéro, et le spread (0,16 $) mange la moitié d'un gain de
0,30 $. C'est la plus mauvaise sortie testée (t de −6,5 à −9 : perte nette, pas du hasard). Les bots qui semblent
réussir ainsi montrent la série de petits gains ; la perte rare mais grosse (ou un stop absent, une martingale,
une grille) n'apparaît pas sur les captures.

**0,2 lot, sortie à +10 ou +30 pips** (idée de l'utilisateur ; or : 1 pip = 0,10 $). Rejeu de la cassure seule :

| Réglage | Trades | Gagnants | Gain moyen / perte moyenne | PF | Résultat |
|---|---|---|---|---|---|
| 0,2 lot, +10 pips, limites démo | 123 | 65 % | +17,60 / −42,91 € | 0,76 | −437 €, arrêt à −10 % au 5e jour |
| 0,2 lot, +30 pips, limites démo | 87 | 44 % | +51,50 / −42,90 € | 0,93 | arrêt à −10 % au 2e jour |
| 0,2 lot, +10 pips, sans limite (recherche) | 1 496 | 69 % | +17,57 / −50,09 € | 0,79 | −4 974 € : compte vidé en 7 jours |
| 0,2 lot, +30 pips, sans limite (recherche) | 1 177 | 45 % | +51,06 / −51,22 € | 0,83 | −5 669 € : compte vidé en 5 jours |

**Protéger le gain au lieu de le plafonner** (« intelligent et calculé ») : stop remonté à l'entrée (+0,20 $ pour
le spread) dès que le trade gagne 10 ou 30 pips, puis stop suiveur. 0,1 % par trade, stop modifié au tick suivant :

| Sortie | Gagnants | Gain moyen / perte moyenne | PF | Résultat | +10 pts |
|---|---|---|---|---|---|
| objectif 3 x le stop, sans protection (référence) | 35 % | +6,18 / −3,57 € | 0,95 | −234 € | PF 0,79 |
| break-even dès +10 pips | 62 % | +1,68 / −3,14 € | 0,88 | −432 € | PF 0,71 |
| break-even dès +10 pips puis stop suiveur à 10 pips | 66 % | +1,62 / −3,50 € | 0,88 | −448 € | PF 0,69 |
| idem, sans objectif | 66 % | +1,61 / −3,50 € | 0,88 | −446 € | PF 0,70 |
| break-even dès +1 R puis suiveur à 1 R, sans objectif | 47 % | +3,93 / −3,64 € | 0,95 | −257 € | PF 0,83 |
| break-even dès +30 pips puis suiveur à 10 pips | 46 % | +3,98 / −3,61 € | 0,93 | −380 € | PF 0,80 |

Protéger tôt fait gagner plus souvent mais moins : le stop remonté à l'entrée est touché par le bruit avant que le
mouvement se développe. Protéger à +1 R ne change rien. Aucune des 36 sorties ou tailles testées ne rend le
scalper gagnant : les sorties ne font que redistribuer les résultats (beaucoup de petits gains ou peu de gros) ;
ce qui manque, c'est une **entrée** qui prévoit les secondes suivantes mieux que le spread.

**Lot fixe de 0,1** (décision de l'utilisateur, 29/09 au soir : « mettre les lots à 0,1 ou 0,2 »). Mis en démo
dans les limites dures : un trade est refusé si 0,1 lot risquerait plus de 1 % de l'equity au stop (stop de plus de
5,70 $ environ sur 5 000 €) ; 0,2 lot aurait dépassé 1 % dès 2,84 $ de stop, donc refusé la plupart du temps. Perte
maximale du jour portée à −2 % (défaut du CLAUDE.md, §3.5 ; −1 % bloquait après deux pertes) ; **arrêt total à
−10 % depuis le plus haut de l'expérience** ajouté au scalper (il manquait ; règle 5), avec relance manuelle
seulement (`--nouvelle-experience`). Rejeu de la cassure seule avec ces réglages :

| Prix exécutables | Trades | Gagnants | Gain moyen / perte moyenne | PF | Résultat | Arrêt total |
|---|---|---|---|---|---|---|
| 0,1 lot fixe | 42 | 21 % | +35,44 / −23,49 € | 0,41 | −456 € | au 5e jour (−10,8 %) |
| 0,1 lot fixe, +10 points | 76 | 32 % | +42,69 / −26,26 € | 0,75 | −341 € | au 4e jour (−10,2 %) |

Chaque trade pèse 4 à 5 fois plus qu'à 0,1 % de risque : sur ces 4 semaines, un mauvais départ suffit à atteindre
−10 % en quelques jours, et l'arrêt total coupe l'expérience. Le lot ne change pas l'espérance en R (toujours
négative) ; il raccourcit le chemin jusqu'à la limite. Mis en démo parce que c'est le choix de l'utilisateur, avec
ce résultat annoncé avant le lancement.

Réponse du serveur perdue (relevé par la relecture critique, corrigé) : si la position a déjà touché son stop quand
le bot la cherche, l'ordre est retrouvé dans l'historique des deals (même magic, même commentaire `tag#n`) et son
vrai résultat entre dans les bilans, la perte du jour et la cadence, au lieu d'être noté « échec ». Même
correction dans le bot principal.

### Historique des réglages

- 29/09/2026 matin (cassure seule, avant versionnage) : 3 positions, 0,3 % de risque cumulé, 10 s entre deux
  entrées, au plus un ordre par minute envoyé au broker. Rejeu : 2 281 trades, 43 % de gagnants, profit factor
  0,75, −983 €, aucune journée positive (0,42 avec 10 points de glissement ; 0,70 avec une entrée par minute ;
  0,78 et −3 042 € sans limite journalière).
- 29/09/2026 après-midi : versions `cassure v1` et `impulsion-repli v1`, règles communes ci-dessus (jusqu'à
  5 entrées par minute, autorisation de l'utilisateur).
- 29/09/2026 soir : « + apprentissage v1 » (variantes apprises à chaque trade).
- 29/09/2026 fin de journée : « + cadence v1 » (cadence liée au bénéfice, 12 entrées par minute au plus, budget
  du jour), puis sorties v2 d'après les trades démo : stop d'au moins 2 $, objectif 3 x le stop, 10 minutes au
  plus, « + apprentissage v2 » (objectifs 2 / 3 / 4 R) ; impulsion-repli désactivée. Voir la section suivante.
