# RESEARCH.md — Phase 0 : recherche préalable

> Projet : bot de trading XAUUSD · MetaTrader 5 · Axi (voir `CLAUDE.md`).
> Date : 2026-09-28. Statut : **Phase 0 terminée avec réserves** (voir §0.1).
> Docs en français, identifiants (fonctions, constantes, champs) en anglais.

## Sommaire

0. [Lire d'abord](#0-lire-dabord) : conditions de la recherche, légende, synthèse, risques
1. [MetaTrader 5 et le package Python `MetaTrader5`](#1-metatrader-5-et-le-package-python-metatrader5)
2. [Axi](#2-axi)
3. [Le marché de l'or](#3-le-marché-de-lor)
4. [Méthodologie quant](#4-méthodologie-quant)
5. [Conséquences pour la conception](#5-conséquences-pour-la-conception-propositions-rien-nest-codé)
6. [Questions ouvertes](#6-questions-ouvertes)
7. [À relire quand l'accès réseau sera ouvert](#7-à-relire-quand-laccès-réseau-sera-ouvert)
- [Annexe A : référence API vérifiée sur le package officiel](#annexe-a--référence-api-vérifiée-sur-le-package-officiel-)
- [Annexe B : changements d'heure et heures clés](#annexe-b--changements-dheure-et-heures-clés--calculés-avec-la-base-iana-via-zoneinfo)
- [Annexe C : bibliographie et sources](#annexe-c--bibliographie-et-sources)
- [Annexe D : constats sur le compte démo Axi](#annexe-d--constats-sur-le-compte-démo-axi-check-du-29092026)

---

## 0. Lire d'abord

### 0.1 Conditions de la recherche

Le proxy réseau de l'environnement cloud utilisé pour cette phase **bloque la lecture directe** de presque tous les sites :
- `www.mql5.com`, `metatrader5.com` ;
- `axi.com`, `help.axi.com` ;
- `lbma.org.uk`, `cmegroup.com`, `gold.org` ;
- `ssrn.com`, `web.archive.org`, etc.

Seuls PyPI et GitHub répondent. Aucun contournement n'a été tenté (ni miroir, ni cache, ni archive). Les informations viennent donc de trois sources :

1. **Artefacts officiels analysés localement** (fiabilité maximale) :
   - le wheel `MetaTrader5` 5.0.6231 publié par MetaQuotes sur PyPI (code Python des constantes, puis chaînes du module natif : signatures, noms de champs, messages d'erreur, imports Windows) ;
   - la base IANA des fuseaux horaires (`zoneinfo`).
2. **Moteur de recherche web** : environ 200 requêtes (6 agents en parallèle), ciblées sur les domaines officiels.
   - On ne voit que des **extraits ou résumés** générés par le moteur, jamais la page complète.
   - Le quota de recherche de la session s'est épuisé en fin de parcours. Certaines sous-parties reposent donc sur des connaissances générales, marquées ⚪.
3. **Connaissances générales**, toujours signalées ⚪.

➡️ Tout ce qui n'est pas ✅ doit être **confirmé** de l'une de deux façons :
- en relisant les pages (§7) une fois l'accès réseau ouvert ;
- **empiriquement** sur le compte démo en Phase 1 (`scripts/check_connection.py`).

C'est de toute façon la règle 9 du `CLAUDE.md` : « en cas de doute : test sur démo, pas de supposition ».

### 0.2 Légende

| Marque | Signification |
|---|---|
| ✅ | Vérifié sur un artefact officiel analysé localement (wheel PyPI `MetaTrader5`, base IANA), ou calcul reproductible |
| 🟡 | Vu dans un extrait de recherche provenant d'une **page officielle** (URL donnée) ; la page n'a pas été lue en entier |
| 🟠 | Source tierce (forum ou CodeBase mql5.com, revue de broker, presse, blog), vue en extrait |
| ⚪ | Connaissance générale, **non confirmée** par la recherche |
| 🧪 | À vérifier empiriquement sur le compte démo (Phase 1) |

### 0.3 Synthèse : ce qui change la conception

1. **Heure serveur** : les timestamps renvoyés par MT5 (barres, ticks, deals) sont l'**heure serveur encodée comme un epoch**, alors que la doc parle d'UTC (🟡 doc contre 🟠 forums).
   - Chez Axi, l'heure serveur vaut GMT+2 ou GMT+3 selon l'heure d'été **de New York** (🟡). Donc **heure serveur = heure de New York + 7 h**, toute l'année (✅ calcul).
   - Conversion : heure de New York = heure serveur − 7 h, localisée en `America/New_York`, puis convertie en UTC.
   - Au démarrage, contrôler que le décalage mesuré respecte cette règle.
2. **Calendrier économique** : le package Python n'a aucune fonction calendrier (✅), et les fonctions MQL5 sont interdites dans le Strategy Tester (🟡).
   - Il faut un petit **service MQL5** qui exporte les événements en CSV.
   - Piège : l'historique est horodaté avec le décalage serveur **courant**, pas celui de la date de l'événement (🟡 Book, 🟠 CodeBase). Mal converti, environ la moitié des événements est décalée d'1 h.
3. **Remplissage (filling)** : deux jeux de constantes à ne pas confondre.
   - Dans la requête : `ORDER_FILLING_*` (0 à 3).
   - Côté symbole : le masque `SYMBOL_FILLING_*` (1, 2, 4), absent du package Python.
   - La doc officielle se contredit sur `RETURN` en exécution « Market ». → Choisir par `order_check`, avec repli sur l'erreur 10030.
4. **`order_check` réussi renvoie `retcode == 0`** (`comment='Done'`), et non 10009 (🟠 exemple). `order_send` et `order_check` peuvent renvoyer `None`.
5. **`deviation` est ignoré en exécution Market** (🟡 Book). Le slippage n'est pas plafonné : il faut le mesurer et le journaliser.
6. **Les helpers `Buy()`, `Sell()` et `Close()` du package** envoient des ordres **sans SL ni magic** (✅). `Close()` touche aussi les trades manuels. → Interdits.
7. **L'API Python n'expose pas les sessions de trading** (✅ : aucun équivalent de `SymbolInfoSessionTrade`). Les horaires Axi (pause de 23:59 à 01:01 heure serveur, etc.) iront dans la config.
8. **Axi : MT5 n'est pas disponible dans tous les pays** (🟡).
   - L'entité qui ouvrirait le compte d'un résident français serait **AxiTrader Ltd, à Saint-Vincent-et-les-Grenadines** (🟠), avec un levier jusqu'à 1:1000, un stop-out à 20 % et aucune protection ESMA.
   - → **Risque bloquant n° 1**, à lever avant la Phase 1.
9. **Coûts Axi incertains** : selon les pages officielles, la commission Pro sur l'or vaut 7 $ ou 4,50 $ l'aller-retour (🟡, contradictoire).
   - → À **mesurer** avec l'ordre de test (`--test-order`).
   - Le spread est à mesurer heure par heure sur les ticks.
10. **Granularité** : 0,01 lot = 1 oz, donc **1 $ de mouvement = 1 $ de P&L par 0,01 lot** (✅ calcul). Avec 0,5 % de risque, un SL de 20 $ exige au moins 4 000 $ d'equity pour prendre même 0,01 lot (§3.10).
11. **Budget d'essais** : avec environ 10 ans de données, au-delà d'environ 45 configurations indépendantes, un Sharpe in-sample de 0,7 peut apparaître **par pur hasard** (MinBTL, ✅ calcul d'après Bailey et al.).
    - → **Registre de tous les essais**, grilles grossières, Deflated Sharpe Ratio (DSR) et Probability of Backtest Overfitting (PBO).
12. **Critères de passage en démo** du `CLAUDE.md` : sains mais à renforcer. « Majorité des fenêtres walk-forward positives » est un critère faible : 7 fenêtres positives sur 12 arrivent 39 % du temps à pile ou face (✅ calcul). Propositions en §4.5.

### 0.4 Risques bloquants ou majeurs

| # | Risque | Impact | Levée |
|---|---|---|---|
| R1 | MT5 (réel ou démo) indisponible chez Axi pour ton pays ou ton entité | L'architecture Python/MT5 tombe (MT4 n'a pas d'API Python officielle ⚪) | **Levé pour la démo** (compte MT5 démo actif le 29/09/2026). À reconfirmer pour un compte réel |
| R2 | Entité offshore (SVG) | Stop-out à 20 %, levier élevé, pas de protection ESMA | Q1. Le bot applique **ses propres** limites (§3 du `CLAUDE.md`) quel que soit le levier offert |
| R3 | Faits non relus à la source (proxy) | Erreurs de détail possibles | Phase 1 : `check_connection.py` dumpe `symbol_info`, `account_info` et `terminal_info` ; relecture du §7 |
| R4 | Profondeur d'historique Axi inconnue (M1 et ticks) | Backtest court, budget d'essais faible (§4.3) | Phase 2 : inventaire de l'historique, Dukascopy en complément (Q8) |
| R5 | Mise à jour automatique du terminal | Erreur « Incompatible versions » (-5), bot à l'arrêt | Version du package figée, `version()` journalisée, procédure de mise à jour (Phase 6) |

---

## 1. MetaTrader 5 et le package Python `MetaTrader5`

La référence exhaustive (signatures, codes, champs, constantes), vérifiée sur le binaire officiel, est en **Annexe A**. Cette section résume la sémantique et les pièges.

### 1.1 Le package

**Ce qu'on sait**
- Version actuelle : **5.0.6231**, publiée le 2026-09-27. 39 versions au total, avec une cadence rapide : 5.0.6070 (28/07), 6090 (01/08), 6147 (27/08), 6162 (03/09), 6180 (05/09), 6231 (27/09/2026). ✅ https://pypi.org/project/MetaTrader5/
- Uniquement des wheels **`win_amd64`**, pour CPython 3.6 à 3.14, sans sdist. ✅ → Installation impossible sous Linux/Codespaces, Python 64 bits obligatoire.
- Seule dépendance : `numpy>=1.7`. Le binaire référence à la fois `numpy._core` (NumPy 2) et `numpy.core` (NumPy 1). ✅ 🧪
- Le module natif dialogue avec `terminal64.exe` par **named pipe local**, et peut **lancer le terminal** lui-même. ✅ (imports Windows du binaire) ; 🟡 doc : « If required, the MetaTrader 5 terminal is launched to establish connection when executing the initialize() call » (https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py).
- Doc officielle : https://www.mql5.com/en/docs/python_metatrader5 (🟡, non lisible ici).
- Écosystème au 2026-09-28 (✅ PyPI) : `numpy` 2.5.3 exige Python ≥ 3.12, `pandas` 3.0.6 ≥ 3.11, `pyarrow` 25.0.1 ≥ 3.10, `pydantic` 2.13.5 ≥ 3.9, `matplotlib` 3.11.2 ≥ 3.11, `pytest` 9.1.1 ≥ 3.10.

**Pièges**
- Après une mise à jour automatique du terminal : erreur « Terminal: Incompatible versions, please install the latest version of Terminal and Python module » (✅ chaîne du binaire ; 🟠 https://www.mql5.com/en/forum/438477).
  - Journaliser `mt5.version()` et `MetaTrader5.__version__` au démarrage.
  - Mettre à jour le terminal et le package ensemble.
- Helpers `Buy()`, `Sell()` et `Close()` : **interdits** (Annexe A.5).

**Proposition** : Python **3.13** sur Windows et sur Codespaces. Le wheel `cp313` existe, et la version est supportée plus longtemps que la 3.12 par l'écosystème NumPy. → Q5

### 1.2 Connexion : `initialize`, `login`, `shutdown`, `version`, `last_error`

| Fonction | Ce qu'on sait | Source |
|---|---|---|
| `initialize(path, login=, password=, server=, timeout=, portable=)` | `path` est le 1er argument, positionnel. `timeout` est en **millisecondes**, défaut **60 000**. `portable` vaut `False` par défaut. Si `password` ou `server` est omis, le terminal prend ceux qu'il a enregistrés. Peut lancer le terminal. Renvoie `True` ou `False` | ✅ signature ; 🟡 https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py · https://www.mql5.com/en/book/advanced/python/python_init |
| `login(login, password=, server=, timeout=)` | Change de compte **dans le terminal déjà connecté** : le terminal entier bascule, y compris ses EA | ✅ ; 🟠 https://www.mql5.com/en/forum/449894 ; ⚪ |
| `shutdown()` | Ferme la connexion IPC, sans forcément fermer le terminal | ✅ ; ⚪ |
| `version()` | Version du **terminal**, au format `(version, build, date)`. Celle du package est `MetaTrader5.__version__` | ✅ ; ⚪ format |
| `last_error()` | Tuple `(code, description)`, codes `RES_*` (Annexe A.2) | ✅ |

**Pièges**
- Le MQL5 Book écrit `account=`, mais le binaire n'accepte que **`login=`** (✅).
- Toujours passer le **chemin absolu** du terminal Axi dédié.
  - Sinon, le module choisit lui-même le terminal, par un algorithme non documenté (🟡 Book).
  - Un 2e `initialize()` sur un autre terminal casse la connexion au premier (🟠 https://www.mql5.com/en/forum/351590).
- Erreurs rapportées :
  - `(-6, 'Terminal: Authorization failed')` : login, mot de passe ou nom exact du serveur (🟠 https://www.mql5.com/en/forum/368902).
  - `-10003` : « IPC initialize failed », « MetaTrader 5 x64 not found » ou « Process create failed » (🟠 https://www.mql5.com/en/forum/439845 · https://www.mql5.com/en/forum/455101).
  - `-10005 IPC timeout`, intermittent (🟠 https://www.mql5.com/en/forum/447937 · https://www.mql5.com/en/forum/443248 · https://www.mql5.com/en/forum/428075). Correctifs rapportés :
    - autoriser l'algo trading dans le terminal ;
    - **décocher « Disable algorithmic trading via external Python API »** ;
    - se connecter une première fois à la main au serveur ;
    - passer un `path` explicite ;
    - en mode portable, copier `servers.dat`.
- Les comptes protégés par **OTP ou certificat** ne sont pas supportés : le binaire contient « Unsupported authorization mode, OTP or certificate password needed » (✅). → Ne pas activer d'OTP sur le compte du bot 🧪.
- Appeler `last_error()` **immédiatement** après l'échec (⚪).
- **Constaté sur le PC de l'utilisateur (29/09/2026)** : `initialize(path, login=, password=, server=)` échouait à chaque essai en **-10005 IPC timeout**, alors que le terminal était ouvert et connecté au bon compte. S'attacher d'abord avec `initialize(path, timeout=)`, sans identifiants, puis appeler `login()` seulement si `account_info().login` diffère : connexion en 3 secondes (✅). C'est la méthode retenue par `MT5Broker` (Annexe D).

**🧪 À vérifier**
- Terminal fermé : `initialize(path)` le lance-t-il, et en combien de temps ?
- Un `initialize` sans mot de passe fonctionne-t-il après un premier login ?
- Quels codes exacts pour un mauvais mot de passe ou un mauvais serveur ?

### 1.3 État du terminal et du compte : vérifications au démarrage

**`terminal_info()`**

| Champ | Signification | Source |
|---|---|---|
| `connected` | Connexion au serveur de trading. `False` ne veut **pas** dire que l'IPC est perdue | 🟡 https://www.mql5.com/en/docs/constants/environment_state/terminalstatus |
| `trade_allowed` | Bouton **Algo Trading**. Désactivé : `order_send` renvoie 10027 `CLIENT_DISABLES_AT`. Impossible à activer par code | 🟡 https://www.mql5.com/en/docs/runtime/tradepermission ; 🟠 https://www.mql5.com/en/forum/376255 · https://www.mql5.com/en/forum/399347 |
| `tradeapi_disabled` | Option « Disable algorithmic trading via external Python API » (Outils > Options > Expert Advisors). `True` = bloqué. Erreur probable `(-8, 'Auto-Trading using Python API disabled in Terminal')` | 🟠 forums ci-dessus ; ✅ chaîne ; ⚪ lien |
| `maxbars` | Réglage « Max bars in chart », qui borne `copy_rates_*` | 🟡 https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrompos_py |
| `ping_last` | Dernier ping vers le serveur, **en microsecondes** (exemple observé : 38103 ≈ 38 ms) | 🟡/🟠 terminalstatus ; 🧪 |
| `retransmission` | % de paquets TCP retransmis **pour toute la machine**, pas seulement MT5 | 🟡 terminalstatus · https://www.mql5.com/en/book/common/environment/env_connectivity |
| `data_path`, `commondata_path` | Dossier de données du terminal et dossier `Common` partagé, utile pour le CSV du calendrier | 🟡 https://www.mql5.com/en/docs/python_metatrader5/mt5terminalinfo_py |

**`account_info()`**

| Champ | Signification | Source |
|---|---|---|
| `trade_mode` | 0 démo, 1 concours, 2 réel → **garde-fou n° 1** du `CLAUDE.md` | ✅ valeurs ; 🟡 https://www.mql5.com/en/book/automation/account/account_real_demo_contest |
| `margin_mode` | 0 netting, 1 exchange, 2 hedging | ✅ ; 🟡 https://www.mql5.com/en/book/automation/account/account_netting_hedge |
| `trade_allowed` | Faux si la connexion utilise le mot de passe investisseur | 🟡 https://www.mql5.com/en/book/automation/account/account_limits_and_restrictions |
| `trade_expert` | Le broker peut interdire le trading automatique en gardant le trading manuel. Conséquence probable : 10026 `SERVER_DISABLES_AT` | 🟡 idem ; ⚪ code |
| `margin_so_mode`, `margin_so_call`, `margin_so_so` | Mode 0 = % ou 1 = montant ; les niveaux s'expriment dans cette unité | 🟡 https://www.mql5.com/en/docs/constants/environment_state/accountinformation ; ⚪ |
| `fifo_close`, `limit_orders` | Clôture FIFO obligatoire (toujours `False` hors hedging) ; nombre max d'ordres en attente (0 = illimité ⚪) | 🟡 Book |
| `company`, `server`, `leverage`, `currency` | `company` = nom légal de la contrepartie, donc **l'entité Axi réelle** | ⚪ ; 🧪 |

**Pièges**
- Deux interrupteurs indépendants bloquent le trading : le terminal (`trade_allowed`, `tradeapi_disabled`) et le serveur (`trade_expert`). Les vérifier **au démarrage et avant chaque envoi d'ordre**.
- Ces tuples sont des instantanés : relire l'equity et la marge avant chaque décision.

### 1.4 Symboles et détection de l'or

**Ce qu'on sait**
- `symbols_get(group=...)` (🟡 https://www.mql5.com/en/docs/python_metatrader5/mt5symbolsget_py · https://www.mql5.com/en/book/advanced/python/python_symbols) :
  - le filtre porte sur le **nom** ;
  - `*` est un joker, `!` une exclusion ;
  - plusieurs conditions séparées par des virgules, appliquées dans l'ordre : inclusions d'abord, exclusions ensuite. Exemple : `"*XAU*"`.
  - Écrire les jokers explicitement : `!*EUR*` plutôt que `!EUR`, les deux formulations circulent.
- `symbol_info(s)` renvoie `None` en cas d'erreur. Le symbole doit être sélectionné dans le Market Watch (🟡 extrait ; 🟠 https://www.mql5.com/en/forum/388480). Motif sûr : `symbol_select(s, True)`, puis `symbol_info(s)` (⚪ 🧪).
- `symbol_info_tick(s)` : `time` en secondes et `time_msc` en millisecondes, en **heure serveur**. Marché fermé : renvoie le dernier tick connu, périmé (⚪).
- Chez Axi (🟡 https://support.axi.com/hc/en-us/articles/4403602104601 · https://help.axi.com/hc/en-us/articles/47480882755225-Which-instruments-can-I-trade-on-MT5) :
  - symboles or : XAUUSD, XAUEUR, XAUAUD, XAUCHF, XAUGBP, plus des futures CFD sur métaux ;
  - sur un compte Pro, les symboles portent le suffixe **`.pro`** et ceux sans suffixe sont grisés. C'est établi pour EURUSD.pro ; ⚪ pour l'or.

**Procédure de détection proposée**
1. `symbols_get(group="*XAU*")`.
2. Garder les noms en `XAUUSD*` avec `trade_mode == SYMBOL_TRADE_MODE_FULL` et `currency_profit == "USD"`.
3. S'il en reste plusieurs, exiger un **choix explicite dans la config**.
4. Enregistrer le dump JSON complet de `symbol_info`.

**Pièges**
- Sur un CFD, `last` et `volume` valent probablement 0 (⚪).
- Un tick récent ne garantit pas que le marché soit tradable : vérifier aussi `trade_mode`.
- `spread` s'exprime en **points** : 0,18 $ = 18 points si `digits`=2, 180 si `digits`=3. Ne rien coder en dur.

### 1.5 Données : barres et ticks

**Ce qu'on sait**
- `copy_rates_from(symbol, tf, date_from, count)` : `count` barres dont l'ouverture est ≤ `date_from`, en remontant le temps (⚪, sémantique de `CopyRates`).
- `copy_rates_from_pos(symbol, tf, start_pos, count)` : **l'index 0 est la barre courante, non clôturée** (⚪ ; doc probable). → Pour les signaux, prendre `start_pos=1` ou retirer la dernière ligne.
- `copy_rates_range(symbol, tf, date_from, date_to)` : barres telles que `date_from ≤ ouverture ≤ date_to` (⚪ 🧪).
- Retour : tableau numpy structuré (`time, open, high, low, close, tick_volume, spread, real_volume`, ✅ noms), trié par date croissante (⚪), ou `None` en cas d'erreur.
- Borné par « Max bars in chart » (🟡). → Régler ce paramètre au maximum sur la machine d'export. L'historique est téléchargé à la demande : un premier appel peut être partiel (⚪).
- `copy_ticks_from(symbol, date_from, count, flags)` (avance vers le présent ⚪) et `copy_ticks_range(...)` :
  - `COPY_TICKS_ALL`=-1, `COPY_TICKS_INFO`=1 (bid/ask), `COPY_TICKS_TRADE`=2 (last/volume) (✅ valeurs ; ⚪ sens) ;
  - pour un CFD, utiliser `INFO` ou `ALL` ;
  - découper les plages **par jour**.
- `date_from` et `date_to` sont **positionnels uniquement** (✅). Un `datetime` naïf est interprété en heure locale (⚪). → Passer des entiers ou des `datetime` UTC avec fuseau, sur l'échelle « epoch serveur » (§1.12).
- Timeframes codés en bits : `TIMEFRAME_H1` = 16385 (✅). Ne jamais passer un nombre de minutes.

**Pièges**
- Le `spread` d'une barre est-il le minimal ou le dernier de la barre ? Non trouvé 🧪.
- Une plage plus grande que `maxbars` renvoie `None` (⚪).
- pandas 3 : `to_datetime(ints, unit='s')` donne du `datetime64[s]`, et `.astype('int64')` ne renvoie plus des nanosecondes (🟡 https://pandas.pydata.org/docs/whatsnew/v3.0.0.html).

**🧪 À vérifier** : ordre des lignes, dtype, présence de la barre 0, inclusion des bornes, profondeur réelle M1 et ticks chez Axi, alignement de la barre D1 sur 17:00 New York.

### 1.6 Positions, ordres, historique

- `positions_get(symbol=|group=|ticket=)` et `orders_get(...)` :
  - filtres **par mot-clé uniquement** (✅ « Unnamed arguments not allowed ») ;
  - **aucun filtre `magic`** : on filtre côté client (règle 6) ;
  - `None` (erreur) ≠ `()` (aucune position) (⚪ 🧪).
- `history_orders_get` / `history_deals_get` acceptent trois formes (✅) : `(date_from, date_to, group=)` avec les dates en positionnel, `ticket=`, ou `position=`.
  - Pour `history_deals_get`, **`ticket=` est un ticket d'ORDRE** (DEAL_ORDER) ; `position=` correspond à DEAL_POSITION_ID (⚪ 🧪).
- Piège classique : `date_to = now()` (heure locale ou UTC) est **en retard** sur l'heure serveur, et les derniers deals manquent. → `date_to = now + 1 jour` (🟠/⚪).
- P&L réalisé d'une position = Σ(`profit` + `commission` + `swap` + `fee`) de ses deals, obtenus via `position=` (⚪). `position.profit` n'inclut pas la commission (⚪).
- Clôture par SL, TP ou stop-out : `deal.reason` vaut `DEAL_REASON_SL` (4), `_TP` (5) ou `_SO` (6) (✅ valeurs).

### 1.7 Calcul de marge et de profit

- `order_calc_margin(action, symbol, volume, price)` et `order_calc_profit(action, symbol, volume, price_open, price_close)` (⚪) :
  - résultat en **devise du compte**, conversion comprise ;
  - `action` = `ORDER_TYPE_BUY` ou `ORDER_TYPE_SELL` ;
  - `None` en cas d'erreur ;
  - marge calculée sans tenir compte des positions existantes.
- `order_calc_profit` n'inclut ni commission ni swap (⚪). → C'est la bonne base du sizing du `CLAUDE.md` : `perte_pour_1_lot = order_calc_profit(sens, sym, 1.0, entrée, SL)`.
- Axi applique une **marge dynamique par paliers** : le levier baisse quand la taille augmente (🟡 https://support.axi.com/hc/en-us/articles/44105575139865-Important-Changes-to-Leverage-and-Margin-Structure). → Toujours `order_calc_margin` puis `order_check`, jamais de formule maison en live.
- Formules MQL5 par `trade_calc_mode`, par exemple pour un CFD : marge = lots × contrat × prix × taux ; profit = (clôture − ouverture) × contrat × lots (⚪). Elles ne serviront qu'au SimBroker, calibré sur les valeurs réelles.

### 1.8 Requêtes de trading : `order_check` et `order_send`

Champs par action. La structure est 🟡 (https://www.mql5.com/en/docs/constants/structures/mqltraderequest · https://www.mql5.com/en/book/automation/experts/experts_mqltraderequest) ; le détail est ⚪ et à confirmer 🧪.

| Action | Champs |
|---|---|
| Ouverture au marché (`TRADE_ACTION_DEAL`) | `action, symbol, volume` (float), `type, type_filling, sl, tp, magic, comment`. `price` et `deviation` ne servent qu'en Request/Instant ; ils sont **ignorés en Market** (🟡 https://www.mql5.com/en/book/automation/experts/experts_market_buy_sell) |
| Clôture en hedging (`DEAL`) | Les mêmes champs, plus `position` = ticket, avec le type opposé et un volume ≤ celui de la position. **Sans `position`, une nouvelle position opposée s'ouvre** (⚪) |
| Ordre en attente (`PENDING`) | `action, symbol, volume, price, sl, tp, type` (BUY_STOP…), `type_filling, type_time, expiration, magic, comment` |
| Modifier SL/TP (`SLTP`) | `action, symbol, position, sl, tp`. **Omettre `tp` efface le TP** (⚪) : toujours envoyer les deux |
| Modifier un ordre (`MODIFY`) | `action, order, price, sl, tp, type_time, expiration` |
| Supprimer un ordre (`REMOVE`) | `action, order` |
| `CLOSE_BY` | `action, position, position_by` |

**`order_check(request)`** renvoie un `OrderCheckResult` (✅ champs).
- **Succès : `retcode == 0` et `comment == 'Done'`**, pas 10009 (🟠 exemple de sortie ; https://github.com/skyphusion-labs/mt5-risk-bot/issues/8).
- Échec : un code `TRADE_RETCODE_*` (🟡 https://www.mql5.com/en/book/automation/experts/experts_mqltradecheckresult).
- Un check réussi ne garantit pas l'exécution (🟡 https://www.mql5.com/en/docs/trading/ordercheck).
- Peut renvoyer `None` (🟠 https://www.mql5.com/en/forum/388137).

**`order_send(request)`** renvoie un `OrderSendResult` (✅ champs).
- Succès = 10009 `DONE`, ou 10008 `PLACED` pour un ordre en attente 🧪.
- Peut renvoyer `None` : lire alors `last_error()` (🟠 https://www.mql5.com/en/forum/335058).
- `result.volume` est le volume confirmé par le broker ; `retcode_external` dépend du broker (🟡 https://www.mql5.com/en/docs/constants/structures/mqltraderesult).

**Autres contraintes**
- `volume` doit être un **float** (🟠 https://www.mql5.com/en/forum/430288). `magic` est un entier (ulong ⚪).
- Longueur maximale de `comment` : non trouvée (31 caractères souvent cités ⚪) 🧪. Elle compte pour l'id de signal (idempotence).

### 1.9 Codes retour : politique proposée

Les libellés sont 🟡 (https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes) ou ⚪. Aucune classification officielle « réessayable / définitif » n'a été trouvée : ce qui suit est une **proposition**, à confirmer sur démo.

| Classe | Codes | Réaction du bot |
|---|---|---|
| Succès | 10009 DONE ; 10008 PLACED (ordre en attente) ; 10010 DONE_PARTIAL (reliquat annulé en IOC) ; 10025 NO_CHANGES sur un SLTP (rien à faire) | Journaliser, réconcilier |
| Réessai immédiat (tick frais, 2 à 3 fois au plus) | 10004 REQUOTE, 10020 PRICE_CHANGED, 10021 PRICE_OFF | Recalculer prix et SL, vérifier que le signal reste valide |
| Réessai avec backoff | 10024 TOO_MANY_REQUESTS, 10028 LOCKED | Backoff exponentiel |
| **État incertain : réconcilier AVANT tout renvoi** | 10012 TIMEOUT, 10031 CONNECTION, 10011 ERROR, retour `None` | `positions_get`, `orders_get` et `history_deals_get` filtrés par magic et id de signal. Jamais de double ordre (🟠 https://www.mql5.com/en/blogs/post/770910) |
| Trading bloqué | 10026 SERVER_DISABLES_AT, 10027 CLIENT_DISABLES_AT (Algo Trading coupé), 10017 TRADE_DISABLED, 10032 ONLY_REAL | Pause et alerte |
| Marché fermé | 10018 MARKET_CLOSED | Attendre la session |
| Fonds insuffisants | 10019 NO_MONEY | Pas de réessai, alerte |
| Paramètres invalides | 10013 à 10016, 10022, 10035, 10038 ; 10030 INVALID_FILL (→ filling suivant) | Bug : corriger, ne pas boucler |
| Limites | 10033 LIMIT_ORDERS, 10034 LIMIT_VOLUME, 10040 LIMIT_POSITIONS | Alerte |
| Position ou ordre | 10036 POSITION_CLOSED, 10039 CLOSE_ORDER_EXIST, 10029 FROZEN (réessayer hors zone de gel) | Réconcilier |
| Règles du symbole ou du compte | 10042 à 10045 ; 10046 HEDGE_PROHIBITED (existe en MQL5, absent du package 🟠 https://www.mql5.com/en/articles/13052) | Alerte |

### 1.10 Exécution, remplissage, expiration, niveaux de stops

- **Mode d'exécution** `trade_exemode` : REQUEST 0, INSTANT 1, MARKET 2, EXCHANGE 3 (✅). En Market et Exchange, le `price` d'un DEAL est ignoré et `deviation` n'a aucun effet (🟡 Book, lien au §1.8).
- **Remplissage** (🟡 https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties) :
  - FOK : tout ou rien ;
  - IOC : le volume maximal disponible, reliquat annulé ;
  - RETURN : le reliquat reste actif ;
  - BOC : carnet uniquement, pour les ordres limite.
- Le **masque `filling_mode`** du symbole vaut FOK=1, IOC=2, BOC=4 (🟡/⚪ valeurs ; https://www.mql5.com/en/book/automation/symbols/symbols_execution_filling).
  - En Request et Instant, FOK est toujours utilisé pour les ordres au marché (🟡 https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants).
  - RETURN en Market : **deux pages officielles se contredisent** (🟡 ; 🟠 https://www.mql5.com/en/forum/463767).
  - → **Algorithme** :
    1. Candidats = FOK si le bit 1 est présent, IOC si le bit 2 est présent, puis RETURN.
    2. Valider par `order_check`, et mémoriser le premier accepté.
    3. Si l'envoi renvoie 10030, passer au candidat suivant.
  - Erreur 10030 fréquente avec les exemples de la doc (🟠 https://www.mql5.com/en/forum/368425).
- **Expiration** :
  - le masque `expiration_mode` vaut GTC=1, DAY=2, SPECIFIED=4, SPECIFIED_DAY=8 (🟡 https://www.mql5.com/en/book/automation/symbols/symbols_expiration ; 🟠). À ne pas confondre avec `ORDER_TIME_*` (0 à 3, ✅) ;
  - `expiration` est un timestamp entier en **heure serveur** (⚪ 🧪) ;
  - `order_gtc_mode` : 0 = GTC ; 1 = DAILY, **ordres en attente et SL/TP supprimés au changement de jour** ; 2 = DAILY_NO_STOPS (✅ valeurs ; 🟡 sens) 🧪.
- **Masque `order_mode`** : MARKET 1, LIMIT 2, STOP 4, STOP_LIMIT 8, SL 16, TP 32, CLOSEBY 64 (⚪ 🧪).
- **Stops level** (🟡 marketinfoconstants ; https://www.mql5.com/en/articles/2555) : distance minimale, en points.
  - SL et TP d'un **achat** se mesurent depuis le **Bid**, ceux d'une **vente** depuis l'**Ask**.
  - BUY_STOP > Ask, SELL_STOP < Bid.
  - Les SL/TP d'un ordre en attente se mesurent depuis son prix.
  - La signification de la valeur **0 n'est pas documentée** (le forum se contredit : 🟠 https://www.mql5.com/en/forum/383447). → Garder au moins un spread de marge et gérer 10016.
- **Freeze level** (🟡 article 2555 ; https://www.mql5.com/en/book/automation/symbols/symbols_spreads_levels) : un ordre ou une position est « gelé » quand le prix courant est à moins de `freeze_level` points :
  - du prix d'ouverture, pour un ordre en attente ;
  - du SL ou du TP, pour une position.
  - Gelé = ni modification, ni suppression, ni clôture → 10029.
- **Normalisation** :
  - prix, SL et TP arrondis au multiple de `trade_tick_size` (⚪) ;
  - volume **arrondi vers le bas** au `volume_step` (règle du `CLAUDE.md`), puis arrondi décimal pour éviter du type 0.30000000004 ;
  - trade ignoré si le volume obtenu est < `volume_min`.
- Un ordre stop activé devient un ordre au marché. Le slippage est possible, aggravé par un gap. → Recalculer le risque sur le prix réellement obtenu (⚪).

### 1.11 Hedging, netting, deals et commissions

- **Hedging** : plusieurs positions par symbole, clôture par `position=` ou `CLOSE_BY`.
- **Netting** : une seule position par symbole ; un deal opposé la réduit ou la retourne. **Le magic ne suffit plus à isoler les trades manuels** (⚪ ; 🟡 Book netting_hedge).
- Mode des comptes MT5 Axi : **non trouvé** → lire `account_info().margin_mode` 🧪.
- Deals : `entry` vaut IN 0, OUT 1, INOUT 2 ou OUT_BY 3 ; les champs `commission`, `swap`, `fee` et `profit` existent (✅).
- Commission Axi Pro : « en partie à l'ouverture, en partie à la clôture » (🟡, lien au §2.3). → À mesurer sur les deals IN et OUT 🧪.
- En cas de liquidation, Axi clôture d'abord la **position la plus perdante** (🟡 https://support.axi.com/hc/en-us/articles/38981462022425-What-are-margin-stop-out-levels).

### 1.12 Heure serveur et timestamps

**Ce qu'on sait**
- Doc officielle : « MetaTrader 5 stores tick and bar open time in UTC time zone (without the shift)… Data received from the MetaTrader 5 terminal has UTC time » (🟡 https://www.mql5.com/en/docs/python_metatrader5/mt5copyratesfrom_py).
- Réalité rapportée : l'epoch renvoyé est l'**heure murale du serveur du broker**.
  - 🟠 https://www.mql5.com/en/forum/369602 · https://www.mql5.com/en/forum/375461 · https://www.mql5.com/en/forum/472828
  - Mesure de +3 h sur MetaQuotes-Demo en septembre 2026 : 🟠 https://www.mql5.com/en/forum/515951
- Axi : « MT4/MT5 Server Time is based at GMT+2 when New York observes Eastern Standard Time and moves to GMT+3 when New York observes Daylight Savings Time » (🟡 https://help.axi.com/hc/en-us/articles/37071313315609-What-are-the-trading-hours-for-Forex-pairs-and-CFDs).
- ⇒ **heure serveur = heure de New York + 7 h**, toute l'année (✅ : EST UTC−5 → GMT+2, EDT UTC−4 → GMT+3).
- **Conversion** : `ny = serveur − 7 h`, puis `tz_localize('America/New_York')`, puis `tz_convert('UTC')`. Les heures ambiguës ou inexistantes tombent le dimanche matin à New York, marché fermé (✅ Annexe B).
- **Contrôle** : marché ouvert (tick frais), `round((tick.time − time.time()) / 3600)` doit valoir **2 ou 3** selon l'heure d'été US. Ce contrôle est faux le week-end (tick périmé).
- **Observé sur le terminal démo de l'utilisateur** (capture du 29/09/2026) : le Market Watch affiche 02:35:29 quand il est 01:35 à Paris (UTC+2). Heure serveur = UTC+3 = New York (EDT, UTC−4) + 7 h : la règle est confirmée pour cette date. La mesure automatique par `check_connection.py` reste à faire.

**Pièges**
- `pd.to_datetime(t, unit='s')` produit une date naïve qui *ressemble* à de l'UTC.
- `datetime.fromtimestamp(ts)` sans fuseau applique celui du PC.
- La bougie D1 du serveur va de 17:00 à 17:00 heure de New York, soit 5 bougies par semaine (🟠 https://www.mql5.com/en/blogs/post/774934). Ce n'est pas une D1 UTC.
- Horloge du PC Windows désynchronisée : activer la synchronisation NTP (⚪).
- Veille législative : la Chambre des représentants aurait adopté en juillet 2026 un projet d'heure d'été permanente aux US (🟠 https://www.timeanddate.com/news/time/us-permanent-dst-2026.html ; 🟡 https://www.congress.gov/bill/119th-congress/house-bill/139). La règle serveur d'Axi pourrait alors changer. → Règle en config et contrôle au démarrage.

### 1.13 Calendrier économique MQL5

**Ce qu'on sait**
- 10 fonctions, dont `CalendarCountries`, `CalendarEventByCurrency`, `CalendarValueHistory`, `CalendarValueLast` (🟡 https://www.mql5.com/en/docs/calendar). Elles existent **en MQL5 seulement** : rien dans le package Python (✅ Annexe A.1).
- « Values of datetime type used by all functions and structures that work with the economic calendar are equal to the trade server time (TimeTradeServer) including its time zone and DST (Daylight Saving Time) settings » (🟡 citation, https://www.mql5.com/en/docs/calendar).
- **L'historique est converti avec le décalage serveur courant** (🟡 https://www.mql5.com/en/book/advanced/calendar).
  - Exemple : dans un export fait en été, 1 897 événements sur 5 645 sont décalés d'1 h (🟠 https://www.mql5.com/en/code/77617).
- Fonctions interdites dans le Strategy Tester, erreur 4014 FUNCTION_NOT_ALLOWED (🟡 Book ; 🟠 https://www.mql5.com/en/forum/312462).
- Valeurs `actual`, `forecast`, `prev` : entiers × 1 000 000, `LONG_MIN` si absentes (🟡 https://www.mql5.com/en/docs/constants/structures/mqlcalendar).
- `importance` : NONE, LOW, MODERATE, HIGH (🟡 noms). Ne pas coder les valeurs numériques.
- Timeouts signalés sur les longues plages (🟠 https://www.mql5.com/en/forum/496980). → Requêtes mois par mois.
- **Export vers Python** :
  - `FileOpen(..., FILE_COMMON|FILE_CSV|FILE_ANSI)` écrit dans `Terminal\Common\Files` (🟡 https://www.mql5.com/en/docs/files/fileopen) ;
  - Python trouve ce dossier via `terminal_info().commondata_path` (🟡) ;
  - écrire dans un `.tmp`, puis `FileMove`, pour un remplacement atomique ;
  - sans `FILE_ANSI`, le CSV est en UTF-16 (⚪).
  - Exemples existants : 🟠 https://www.mql5.com/en/code/76951 · https://www.mql5.com/en/code/52977.
- **Contrôle de cohérence proposé** : après conversion en UTC, tous les NFP et CPI doivent tomber à **08:30 America/New_York**, et les communiqués FOMC à **14:00**. Sinon, le décalage appliqué est faux.
- Alternatives :
  - calendriers officiels BLS, BEA et Fed pour le contrôle croisé (domaine public ⚪) ;
  - ForexFactory et Investing.com : CGU et fiabilité douteuses (⚪). → Pas de scraping.
- Le shutdown budgétaire US de fin 2025 a déplacé des publications (🟡 https://www.bea.gov/news/blog/2026-01-07/economic-release-schedule-updates-gdp-personal-income-and-outlays). → Ne jamais backtester avec une règle du type « 1er vendredi du mois » : utiliser le calendrier réel.

### 1.14 Robustesse : IPC, threads, reconnexion, flux figé

- Un processus Python pilote **un seul** terminal (🟠, lien au §1.2). La sûreté entre threads n'est pas documentée (🟠 https://github.com/m1xar/MT5_Connector). → **Passerelle mono-thread** : tous les appels MT5 passent par un seul thread, sous un seul verrou.
- **Reconnexion** (⚪) :
  - si `terminal_info() is None` ou code -10001 à -10005 → `shutdown()`, puis `initialize(path, …)` avec un backoff exponentiel ;
  - si seul `connected` est `False` → attendre : le terminal se reconnecte seul.
- **Flux figé** :
  - âge du tick = `(time.time() + décalage_serveur) − tick.time_msc / 1000` ;
  - seuil de 30 à 60 s (⚪), **appliqué seulement aux heures de cotation** (hors pause 23:59–01:01 serveur, week-end et jours fériés) ;
  - croiser avec `connected` et `ping_last`.
- Le terminal et Python doivent tourner dans la même session Windows, au même niveau de privilèges (⚪ déduit des named pipes) 🧪.
- Le Virtual Hosting MQL5 n'exécute pas de Python (⚪). → VPS Windows classique (Phase 6).

### 1.15 Linux : Wine et `mt5linux` (documenté, déconseillé pour le live)

- `mt5linux` 1.1.1 (04/08/2026) : Wine + RPyC + un binaire **`mt5server.exe` téléchargé depuis GitHub** (✅ README PyPI). Exécuter un binaire tiers qui manipule les identifiants pose un problème de sécurité (règle 10).
- `pymt5linux` 1.0 (04/2025, Python ≥ 3.13) : un Python Windows sous Wine, relié par RPyC (✅ PyPI).
- Guide officiel d'installation Linux via Wine, cité par le README de pymt5linux : https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux (🟠).
- Fragilités (⚪) :
  - versions de Python et de numpy qui divergent entre les deux côtés ;
  - lenteur de RPyC ;
  - casse à chaque mise à jour de Wine ou du terminal ;
  - pas d'écran dans Codespaces.
- **Recommandation** : pas de MT5 dans Codespaces. Recherche et backtests sur des fichiers Parquet exportés depuis Windows.

---

## 2. Axi

⚠️ Les conditions **dépendent de l'entité** : il existe plusieurs centres d'aide aux chiffres parfois contradictoires (help.axi.com, support.axi.com, help-eu.axi.com…). Ne jamais mélanger leurs chiffres.

### 2.1 Entités et onboarding

- **Cinq régulateurs** (🟡 https://help.axi.com/hc/en-us/articles/35968166425625-Is-Axi-licensed-or-authorized-or-regulated) :
  - ASIC : AFSL 318232 ;
  - FCA : FRN 509746 ;
  - DFSA : Dubaï ;
  - FSA de Saint-Vincent-et-les-Grenadines : AxiTrader Limited, n° 25417 BC 2019, statut « registered » ;
  - CySEC : Solaris EMEA Ltd, licence 433/23 du 10/07/2023 (🟡 https://www.cysec.gov.cy/en-GB/entities/investment-firms/cypriot/95468/).
- L'entité dépend du pays de résidence. Un pays absent du formulaire = pas de compte possible (🟡 https://help.axi.com/hc/en-us/articles/38787603455897-Can-I-open-an-Axi-account-in-any-country).
- **France** : le passeport européen de Solaris EMEA couvrirait l'Allemagne, l'Espagne, l'Italie et la Pologne, **mais pas la France**. Un résident français passerait donc par **AxiTrader Ltd (SVG)** (🟠 https://www.broker-forex.fr/Axi.php). → À confirmer avec ton contrat (Q1).
- **Observé le 29/09/2026** : le compte **démo** MT5 de l'utilisateur est au nom d'**AxiCorp Financial Services Pty Ltd** (ASIC), d'après la barre de titre et la messagerie du terminal. Cela contredit l'hypothèse SVG ci-dessus, au moins pour la démo. Les règles retail de l'ASIC (⚪ levier 20:1 sur l'or, clôture à 50 % de marge) s'appliqueraient ; `check_connection.py` affichera le levier et le stop-out réels.
- Côté SVG (⚪) :
  - « registered » ne veut pas dire « regulated » ;
  - la protection contre le solde négatif n'est pas documentée pour cette entité ;
  - un compte ouvert à l'étranger entraîne des obligations déclaratives pour un résident fiscal français (formulaire 3916). Hors périmètre technique : à voir avec un conseiller.
- Attention aux clones : « ax-trading.com » figure sur la liste noire AMF (usurpation), sans lien établi avec Axi (🟡 https://www.amf-france.org/en/warnings/blacklists/usurpation/ax-tradingcom).

### 2.2 Comptes, MT5, devises

- **« Standard and Pro accounts are eligible for MT5 »**. MT5 **n'est pas disponible dans tous les pays**, et **la démo MT5 n'existe que dans certaines régions** (🟡 https://help.axi.com/hc/en-us/articles/47483936524953-Who-can-apply-for-an-MT5-account).
  - Même mention côté UE (🟡 https://help-eu.axi.com/hc/en-us/articles/44805728520985-Does-Axi-offer-MT5-trading-accounts).
- Comptes MT4 et MT5 séparés, 5 comptes au maximum (🟡 https://help.axi.com/hc/en-us/articles/47481686752921).
- Types de comptes (🟡 https://help.axi.com/hc/en-us/articles/51348912473497-What-is-the-difference-between-Standard-and-Pro-trading-accounts) :
  - Standard : pas de commission ;
  - Pro : spreads bruts + commission ;
  - Elite : programme pour gros volumes.
- Devises de base : AUD, PLN, CAD, CHF, EUR, GBP, HKD, JPY, NZD, SGD, USD. Changement possible **avant le premier dépôt uniquement** (🟡 https://support.axi.com/hc/en-us/articles/44114425158809-What-is-a-base-currency).
- Compte swap-free : existe sur MT4 et MT5, mais n'est **pas gratuit** (frais fixes, et « holding fee » au-delà de 5 jours) (🟡 https://help.axi.com/hc/en-us/articles/49379103541529).
- Piège : sur un compte en EUR, le P&L XAUUSD est converti. Du bruit de change s'ajoute aux métriques ; `order_calc_profit` gère la conversion.

### 2.3 XAUUSD chez Axi

- **1 lot = 100 oz** (🟡 https://help.axi.com/hc/en-us/articles/39871768779801-How-can-I-trade-gold-with-Axi).
- Ordres de 0,01 à 20 lots (🟡 https://www.axi.com/int/trade/cfds/commodities/precious-metals/gold). Pas de volume non trouvé 🧪.
- Spreads « à partir de » (chiffres marketing, pas des moyennes) : Standard 0,18 $, Pro 0,081 $ (🟡).
- **Commission Pro : les sources se contredisent.**
  - Page or Int et aide UE : 3,50 $ par lot et par côté, soit 7 $ l'aller-retour (🟡 https://help-eu.axi.com/hc/en-us/articles/38309578404121).
  - Article d'aide Int le plus récent : **4,50 $ l'aller-retour**, prélevés en partie à l'ouverture et en partie à la clôture (🟡, lien au §2.2).
  - Une revue date la baisse au 29/09/2025 (🟠 https://www.compareforexbrokers.com/reviews/axi-review/).
  - Commission libellée dans la devise du compte ? Non confirmé (🟠).
  - → **Mesure réelle via `--test-order`** (Phase 1).
- Marge des métaux : « 1 % à 1:100 », exemple ancien (🟡 https://support.axi.com/hc/en-us/articles/4403602130457). La marge est dynamique, par paliers (§2.7).
- Articles à lire, dont seul le titre a été vu (🟡) :
  - « Why have your spreads changed recently » : https://help.axi.com/hc/en-us/articles/46175832368793
  - « Are the spreads on your demo account the same as live » : https://help.axi.com/hc/en-us/articles/39869202869401

### 2.4 Swaps

- Prélevés chaque jour **entre 23:59:30 et 23:59:59 heure serveur**, soit 17:00 à New York (🟡 https://help.axi.com/hc/en-us/articles/37070088004121-What-is-Swap-Triple-Swap-and-Swap-Rate).
- Triple swap (🟡 idem) :
  - s'applique aux positions détenues du mercredi au jeudi, à cause du règlement spot à T+2 ;
  - certaines paires font exception ;
  - **jour propre aux métaux : non trouvé** → lire `swap_rollover3days` 🧪.
- Niveaux long/short de XAUUSD non trouvés → `swap_mode`, `swap_long`, `swap_short` 🧪. Suivre `position.swap` sur la démo autour du rollover, mercredi compris.

### 2.5 Horaires de XAUUSD et heure serveur

- **Métaux** : ouverture **lundi 01:01**, fermeture **vendredi 23:58**, **pause quotidienne de 23:59 à 01:01**, en heure serveur (🟡 https://help.axi.com/hc/en-us/articles/37073300884121-What-are-the-trading-hours-for-Metals-and-Commodities-CFDs · https://help-eu.axi.com/hc/en-us/articles/38315687345689-Trading-hours-for-Metals-and-Commodities-CFDs).
- Heure serveur : GMT+2 ou GMT+3, selon l'heure d'été **de New York** (🟡, §1.12).
- En heure de New York (✅ calcul) :
  - ouverture dimanche 18:01 ;
  - pause de 16:59 à 18:01 ;
  - fermeture vendredi 16:58.
  - C'est cohérent avec la pause CME de 17:00 à 18:00 ET (🟡 §3.2).
- En heure de Paris : pause de 22:59 à 00:01, et de 21:59 à 23:01 pendant les semaines où les changements d'heure sont décalés (✅ Annexe B).
- Pièges pendant la pause :
  - aucune cotation ;
  - ordres probablement rejetés en 10018 (⚪) ;
  - SL et TP non exécutés ;
  - **gap et spread large à la réouverture**.

### 2.6 Jours fériés

- Axi publie un « Trading Holidays Calendar » et des annonces horaires (🟡 https://www.axi.com/int/trading-tools/holidays-calendar · https://help.axi.com/hc/en-us/sections/44993063043353-Trading-Calendar). Le contenu type pour l'or n'a pas été trouvé.
- Usages habituels, calés sur le CME (⚪) :
  - clôture anticipée les jours fériés US où le marché ouvre ;
  - fermé le Vendredi saint, le 25/12 et le 01/01.
- → Calendrier tenu dans la config, **plus** détection de l'absence de ticks.

### 2.7 Levier, appel de marge, stop-out

| Entité | Levier sur l'or | Appel de marge | Stop-out | Source |
|---|---|---|---|---|
| Int (probablement AxiTrader Ltd SVG), MT4 et MT5 | Jusqu'à 1000:1 depuis le 24/03/2025, dégressif par paliers | Notification sous 100 % | **20 %** | 🟡 https://support.axi.com/hc/en-us/articles/44105575139865 · https://support.axi.com/hc/en-us/articles/38981462022425 · https://help.axi.com/hc/en-us/articles/37077361100953-What-is-Margin-Call |
| CySEC retail | ⚪ 20:1 sur l'or (ESMA) | Non trouvé | **50 %** | 🟡 https://help-eu.axi.com/hc/en-us/articles/40490280967577-What-are-margin-stop-out-levels |
| CySEC professionnel | Jusqu'à 1000:1 (🟠 fxscouts) | Non trouvé | 20 % | 🟡 idem |
| FCA (Royaume-Uni) | 20:1 | Non trouvé | ⚪ 50 % | 🟡 https://www.axi.com/uk/trade/cfds/commodities/precious-metals/gold |

- Axi a déjà **modifié le levier sur des positions ouvertes** (🟡 changement du 24/03/2025).
- Le risque du bot ne dépend pas du levier : la taille vient de la distance au SL. Mais la marge limite la taille possible. → `order_calc_margin` + `order_check` avant chaque envoi (déjà exigé par le `CLAUDE.md`).

### 2.8 Non trouvé, faute de quota : à lire dans les documents contractuels

- Règles sur les EA, le scalping, le hedging, le trading de news, l'arbitrage de latence, une éventuelle durée minimale de détention : **non trouvées**.
  - ⚪ Axi a toujours autorisé EA, scalping et hedging. Les clauses « abusive trading » et « price error » sont classiques dans ce type de contrat.
  - → **Lire le Client Agreement et l'Order Execution Policy de ton entité** (Q9).
- Durée de validité d'une démo : non trouvée 🧪. Le bot doit gérer une démo expirée.
- Localisation des serveurs MT5 et offre VPS : non trouvées. → Mesurer `ping_last` depuis des VPS à Londres et à New York.
- Noms des serveurs MT5 : dans l'e-mail d'ouverture du compte, et dans `account_info().server`.
- Modèle d'exécution (Market ou Instant, requotes, slippage) : non trouvé → `trade_exemode` 🧪.
- Le trailing stop natif de MT5 est exécuté **par le terminal** et s'arrête si le terminal est fermé (⚪ ; 🟡 titre de https://help.axi.com/hc/en-us/articles/48637189284889). → Notre trailing sera géré par le bot via `TRADE_ACTION_SLTP`, le SL serveur restant toujours en place.

---

## 3. Le marché de l'or

### 3.1 Contexte 2025-2026 (indicatif, jamais codé en dur)

- 2025 : environ +65 %, franchissement de 3 000 $ puis de 4 000 $, record au-dessus de 4 100 $ mi-octobre 2025 (🟠 https://www.goldrepublic.com/en-us/gold-price/record).
- Plus haut historique intraday : **5 589,38 $ le 28/01/2026** (🟠 idem · https://preciousmetalscharts.com/gold-price-2026).
- Moyenne LBMA PM au T2-2026 : **4 506,29 $**, soit −8 % sur le T1 et +37 % sur un an (🟡 https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q2-2026).
- Août 2026 : +13 % à 4 563 $ (🟡 https://www.gold.org/goldhub/research/gold-market-commentary-august-2026).
- Fin septembre : environ 4 150 à 4 300 $ (🟠). **Les sources se contredisent** : 4 563 $ fin août selon le WGC, 4 331 $ le 01/09 selon Fortune. → Se fier aux données du broker.
- Politique monétaire : présidence de la Fed par Warsh depuis mai 2026 (🟠) et hausse de taux le 16/09/2026 (🟠). L'or est très sensible aux anticipations de hausse (🟡 https://www.gold.org/goldhub/gold-focus/2026/09/weekly-markets-monitor-treasury-tribalism).
- Banques centrales : 863 t en 2025 (🟡 https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2025/central-banks) ; 289 t au T2-2026, record pour un 2e trimestre (🟡).
- ETF : +121 t en août 2026, encours record de 4 189 t (🟡 https://www.gold.org/goldhub/research/gold-etfs-holdings-and-flows/2026/09).
- ➡️ Régime de **forte volatilité**, avec un prix environ deux fois plus élevé qu'en 2023. Des paramètres en dollars ne sont pas stationnaires : **tout exprimer en ATR ou en %**.

### 3.2 Sessions, définies dans leur fuseau IANA

| Session | Définition proposée | Source |
|---|---|---|
| Asie | 18:00 New York → 08:00 Londres ; sous-fenêtre SGE jour : 09:00–15:30 Asia/Shanghai | 🟡 SGE ; ⚪ convention |
| Londres | 08:00–16:30 Europe/London | ⚪ convention |
| New York « cœur » | 08:20–13:30 America/New_York, de l'ouverture de l'ancien pit COMEX au settlement | 🟠/🟡 (§3.4) |
| Chevauchement | 08:20 New York → 16:30 Londres | ⚪ |

- **CME Globex GC** : du dimanche au vendredi, 18:00–17:00 ET, avec une **pause de 17:00 à 18:00 ET** (🟡 https://www.cmegroup.com/trading/metals/files/fact-card-gold-futures-options.pdf).
- **SGE** (Asia/Shanghai, sans heure d'été) : 09:00–11:30, 13:30–15:30, puis séance de nuit 20:00–02:30. Pas de séance de nuit la veille d'un jour férié chinois (🟡 https://en.sge.com.cn/eng_trading_ProductsIntroduce).
- Le chevauchement Londres–New York concentre le volume maximal. Son range vaut environ 1,8 à 2,2 fois la moyenne sur 24 h (🟠 https://marketstalkers.co.uk/blog/posts/market-profile-on-gold-futures-gc, méthode non publiée).
- Les « heures Asie » au sens du CME représentent environ un tiers du volume GC (🟡 https://www.cmegroup.com/openmarkets/metals/2025/Asias-Growing-Gold-Demand-Fuels-Surging-Derivatives-Market.html).
- La découverte du prix est dominée par les futures de New York (🟠 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2606587).
- ➡️ Mesurer nous-mêmes le spread et le range heure par heure sur les ticks Axi (Phase 2).

### 3.3 LBMA Gold Price

- Enchères à **10:30 et 15:00 Europe/London**, sur la plateforme d'ICE Benchmark Administration (🟡 https://www.lbma.org.uk/prices-and-data/lbma-gold-price), depuis le 20/03/2015.
- Tours de 30 s ; seuil de déséquilibre normalement de 10 000 oz (🟡 https://www.ice.com/publicdocs/futures/IBA_Gold_Auction_Specification.pdf).
- Ancien fixing : volume et volatilité bondissaient dès le début de l'enchère de l'après-midi, et les premières minutes prédisaient le sens du prix (🟠 https://onlinelibrary.wiley.com/doi/10.1002/fut.21636). Pour le mécanisme actuel (après 2015) : non trouvé.
- 15:00 à Londres = **10:00 à New York**, en même temps que les statistiques US de 10:00. Pendant les semaines décalées, cela devient 11:00 à New York (✅ Annexe B).
- ➡️ Marquer `FIX_AM` et `FIX_PM` comme des fenêtres à part (features ou filtres), sans conclusion a priori.

### 3.4 COMEX et CME

- Settlement : fenêtre 13:29:00–13:30:00 ET, au VWAP (🟡 https://www.cmegroup.com/confluence/display/EPICSANDBOX/Gold).
- Mois actifs G, J, M, Q, V, Z. Le premier jour de notification (FND) est le dernier jour ouvré du mois qui précède la livraison. Effet du roll sur le spot faible (⚪).
- Jours fériés : https://www.cmegroup.com/trading-hours.html (🟡). Usages courants (⚪) :
  - clôture anticipée vers 13:30 ET les jours fériés US où le marché ouvre ;
  - fermé le Vendredi saint, le 25/12 et le 01/01.
- Il existe trois « clôtures » différentes : settlement à 13:30 ET, fin de Globex à 17:00 ET, minuit serveur. → **Jour de trading défini de 17:00 à 17:00 heure de New York**, ce qui coïncide avec le jour serveur Axi.

### 3.5 Rollover quotidien

- Pause CME de 17:00 à 18:00 ET (🟡) ; pause Axi de 23:59 à 01:01 heure serveur (🟡 §2.5).
- Le spread s'élargit fortement autour de la pause (⚪ ; aucune statistique trouvée).
- ➡️ **Blackout proposé de 16:45 à 18:30 heure de New York** (aucune entrée, ordres en attente annulés), plus un filtre de spread.

### 3.6 Week-end et jours fériés

- Fermeture le vendredi à 17:00 ET, réouverture le dimanche à 18:00 ET, soit environ 49 h (🟡 CME).
- Taille et fréquence des gaps du dimanche : **non trouvées** → à mesurer sur l'historique Axi, en rapport |gap| / ATR(D1).
- Jours fériés au Royaume-Uni en 2026, sans enchère LBMA (⚪) : 01/01, 03/04, 06/04, 04/05, 25/05, 31/08, 25/12, 28/12.
- Golden Week chinoise début octobre : SGE fermé (⚪).
- Risque géopolitique du week-end en 2026 : escalade entre les États-Unis et l'Iran (🟡 WGC, §3.1).
- ➡️ Aucune entrée le dimanche entre 18:00 et 19:00 ET (règle « Lundi : attendre X min » du `CLAUDE.md`). Positions intraday soldées avant le vendredi vers 16:30 ET.

### 3.7 News USD à fort impact (heures de New York)

| Événement | Heure ET | Fréquence | Source |
|---|---|---|---|
| NFP (Employment Situation) | 08:30 | Mensuel, souvent le 1er vendredi | 🟡 https://www.bls.gov/schedule/news_release/empsit.htm |
| CPI | 08:30 | Mensuel | 🟡 https://www.bls.gov/schedule/news_release/cpi.htm |
| PPI | 08:30 ⚪ | Mensuel | 🟡 https://www.bls.gov/schedule/news_release/ppi.htm |
| PCE (Personal Income and Outlays), PIB (estimation avancée) | 08:30 | Mensuel / trimestriel | 🟡 BEA (§1.13) |
| Ventes au détail | 08:30 | Mensuel | 🟡 https://www.census.gov/retail/release_schedule.html |
| Inscriptions hebdomadaires au chômage | 08:30, jeudi | Hebdomadaire | 🟡 https://www.dol.gov/ui/data.pdf |
| ISM Manufacturing | 10:00, 1er jour ouvré | Mensuel | 🟡 https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/ |
| ISM Services | 10:00, 3e jour ouvré ⚪ | Mensuel | ⚪ |
| JOLTS | 10:00 | Mensuel | 🟡 https://www.bls.gov/schedule/news_release/jolts.htm |
| Université du Michigan | 10:00 | 2 fois par mois | 🟠 |
| Communiqué FOMC | 14:00 | 8 fois par an | 🟡 https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm |
| Conférence de presse FOMC | 14:30 | 8 fois par an | 🟠 |
| Minutes du FOMC | 14:00, 3 semaines après la réunion | 8 fois par an | ⚪ |
| Discours du président de la Fed, Jackson Hole | Variable | — | 🟡 https://www.federalreserve.gov/newsevents/speech/warsh20260828a.htm |

- Réunions FOMC 2026 : 27–28/01, 17–18/03, 28–29/04, 16–17/06, 28–29/07, 15–16/09, **27–28/10**, **08–09/12** (🟡).
- Réaction au NFP 15 min après la publication (étude de 2025, à des prix bien plus bas : raisonner en ATR) : +7,2 $ en moyenne si le chiffre est sous le consensus, −6,5 $ s'il est au-dessus (🟠 https://www.fxstreet.com/analysis/us-july-nonfarm-payrolls-preview-analyzing-gold-price-reaction-to-nfp-surprises-202507311000).
- Le **sens** de la réaction dépend du régime. En 2026, des données fortes alimentent les anticipations de hausse des taux, et l'or baisse (🟡/🟠).
- Fenêtres « sans trading » standard : **non trouvées**. Les praticiens utilisent ±15 à 30 min (⚪).
- ➡️ Proposition (paramètres à valider en walk-forward, dans le budget d'essais) :
  - Tier-1 (NFP, CPI, FOMC, conférence, discours du président) : aucune entrée de T−30 à T+30 min. Pour le FOMC : de 13:45 à 15:30 ET.
  - Tier-2 : aucune entrée de T−10 à T+15 min.

### 3.8 Drivers

- Dollar (DXY), taux réels US, anticipations sur la Fed, géopolitique, achats des banques centrales, flux ETF, demande asiatique (🟡 WGC pour les chiffres 2025-2026).
- La corrélation négative avec les taux réels est **rompue depuis 2022** (⚪).
- ➡️ Pas de filtre macro statique, car les corrélations sont instables. Éventuellement DXY ou US10Y comme features optionnelles, validées hors échantillon (option « contexte dollar » de la Phase 4).

### 3.9 Niveaux de liquidité

- Plus haut et plus bas de la veille ou de la semaine, range asiatique, chiffres ronds : **aucune preuve empirique propre à l'or n'a été trouvée**.
- Sur le FX, Osler (2003, 2005) documente la concentration des ordres stop et TP autour des chiffres ronds (⚪).
- Les concepts ICT/SMC relèvent du folklore, non validé (⚪).
- ➡️ Des hypothèses à tester (S1, S4), avec des définitions **sans look-ahead** :
  - jour de 17:00 à 17:00 heure de New York ;
  - range asiatique connu seulement à sa clôture ;
  - distances exprimées en ATR.

### 3.10 Volatilité, taille de position et capital minimal

- ATR par session ou par jour de semaine : non trouvé → à mesurer en Phase 2.
- Ordre de grandeur (⚪ calcul) : avec une volatilité annualisée d'environ 25 % et un prix de 4 300 $, l'écart-type quotidien vaut environ 68 $, et le range quotidien environ 100 $.
- 0,01 lot = 1 oz, donc **1 $ de mouvement = 1 $ de P&L par 0,01 lot** (✅ calcul, avec le contrat de 100 oz 🟡).
- **Equity minimale** pour pouvoir prendre 0,01 lot (✅ calcul, hors spread et commission) : `equity_min = distance_SL($) / risque_%`.

| Distance du SL | Perte pour 0,01 lot | Equity min. à 0,5 % | Equity min. à 1 % (plafond dur) |
|---|---|---|---|
| 5 $ | 5 $ | 1 000 $ | 500 $ |
| 10 $ | 10 $ | 2 000 $ | 1 000 $ |
| 20 $ | 20 $ | 4 000 $ | 2 000 $ |
| 50 $ | 50 $ | 10 000 $ | 5 000 $ |
| 100 $ | 100 $ | 20 000 $ | 10 000 $ |

- Exemple avec 5 000 $ et un SL de 20 $ :
  - 0,5 % = 25 $, soit 0,0125 lot, arrondi **vers le bas** à 0,01 lot (risque réel 0,4 %) ;
  - tout trade dont le SL dépasse 25 $ est **ignoré**, car le risque n'est jamais arrondi vers le haut (`CLAUDE.md`).

**Application au compte démo de l'utilisateur : 5 000 €, compte Standard (réponse du 2026-09-29)**

Hypothèse indicative : EUR/USD ≈ 1,15. Le bot utilisera le vrai taux via `order_calc_profit`. Le check du 29/09/2026 a mesuré 1,137 (1 $ de hausse = 0,88 € pour 0,01 lot), soit 25 € ≈ 28,4 $ : les valeurs ci-dessous changent à peine. Distance maximale du SL (en $ par once) selon le volume :

| Risque par trade | Montant | 0,01 lot (1 oz) | 0,02 lot | 0,03 lot | 0,05 lot |
|---|---|---|---|---|---|
| 0,5 % (défaut) | 25 € ≈ 29 $ | SL ≤ 29 $ | SL ≤ 14 $ | SL ≤ 9,6 $ | SL ≤ 5,7 $ |
| 1 % (plafond dur) | 50 € ≈ 57 $ | SL ≤ 57 $ | SL ≤ 29 $ | SL ≤ 19 $ | SL ≤ 11 $ |

- Perte journalière maximale (2 %) : 100 €. Drawdown maximal (10 %) : 500 €.
- Le risque réel est **quantifié** par pas de 0,01 lot et presque toujours inférieur à la cible. Exemple : SL de 20 $ → 0,0144 lot → 0,01 lot, soit environ 17 € (0,35 %). Le backtest devra reproduire exactement cet arrondi et ces trades ignorés.
- Conséquence pour les stratégies : les SL intraday de 10 à 29 $ sont tradables à 0,5 %. **S6 (swing H1-H4, SL typique de 50 à 100 $) ne l'est quasiment pas** avec ce capital ; il faudrait 1 % de risque et un SL ≤ 57 $.
- Compte Standard : pas de commission, coût intégré au spread (« à partir de 0,18 $ » selon Axi, à mesurer). Devise EUR probable, à confirmer par le check : le P&L de XAUUSD sera converti.

### 3.11 Spot, futures et prix du CFD

- Futures GC = spot + coût de portage. Le spread EFP s'est disloqué en 2020 puis en 2024-2025 (⚪). → Ne jamais backtester un P&L sur le GC ; le GC ne sert qu'aux profils de volume.
- Le prix du CFD vient des cotations spot OTC des fournisseurs de liquidité, plus la marge du broker. Seul un volume de ticks est disponible (⚪). → **Validation finale sur les ticks bid/ask d'Axi**, comme le prévoit déjà le `CLAUDE.md`.

---

## 4. Méthodologie quant

### 4.1 Pièges et règles d'implémentation

| Piège | Forme concrète sur des barres M1 | Règle pour le projet |
|---|---|---|
| Look-ahead | Exécuter au close de la barre-signal ; utiliser le high/low du jour ou de la session avant sa fin (`groupby(date).transform('max')`) ; agrégats datés à l'ouverture (pandas `resample` : `closed='left'`, `label='left'` par défaut 🟡 https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.resample.html) ; z-score ou percentiles calculés sur tout l'historique ; `rolling(center=True)`, `bfill` | Signal au close de t, exécution au premier tick de t+1. Agrégats datés à leur fin, puis `merge_asof(direction='backward')`. **Test de troncature** automatique (§9 du `CLAUDE.md`) |
| Warm-up | EMA/ATR récursives dépendantes du point de départ ; `ewm(adjust=True)` (défaut pandas) n'est pas une EMA récursive (⚪) | Période de chauffe explicite, indicateurs maison testés |
| Data snooping | Réutiliser les mêmes données pour sélectionner et évaluer (🟡 White 2000, https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152) | Registre d'essais (§4.3), hold-out scellé |
| Coûts sous-estimés | Spread variable (rollover, Asie, news), commission, slippage, swaps et triple swap, stops sautés par un gap | Spread = f(heure, jour, news), calibré sur les ticks (médiane et p90) ; commission mesurée ; swaps lus sur le symbole |
| Ambiguïté intrabar | SL et TP dans la même barre ; entrée et SL dans la barre d'entrée | Pire cas par défaut (SL d'abord) ; résolution par ticks dès qu'ils existent ; publier le % de trades ambigus, et exiger les ticks au-delà d'environ 5 % |
| Prix bid/ask | Les barres MT5 sont construites sur le Bid (⚪) | Achat à l'Ask, vente au Bid ; SL d'un short déclenché à l'Ask |
| Fills | Stop traversé par un gap | Fill au pire de (stop, open de la barre), plus un slippage paramétrable. TP au prix limite, sans amélioration |
| Non-stationnarité | Le prix de l'or a doublé | Paramètres en ATR ou en % |
| Biais rétrospectif | Stratégies choisies en connaissant le marché haussier récent | Découpes par régime, long et short séparés, benchmarks (§4.2) |

Référence : Chan, « Backtesting and its Pitfalls » (🟡 https://epchan.com/img/links/Backtesting-and-its-Pitfalls.pdf).

### 4.2 Validation

- **Découpage** :
  - développement sur environ 75 à 80 % des données ;
  - **hold-out** sur les dernières années (au moins 2 ans), **scellé et utilisé une seule fois**.
  - Un hold-out consulté puis suivi d'une modification de règle est « consommé » : il devient de l'in-sample (⚪).
- **Walk-forward** (Pardo, 2e éd. 2008, chap. 11 🟡 https://www.oreilly.com/library/view/the-evaluation-and/9781118045053/pard_9781118045053_oeb_c11_r1.html) :
  - glissant (rolling), avec l'ancré (anchored) en contrôle ;
  - ratio IS:OOS de 3:1 à 6:1 (🟠, à confirmer dans le livre) ;
  - au moins 8 à 10 fenêtres OOS ;
  - statistiques calculées sur la courbe OOS **mise bout à bout** ;
  - paramètres du walk-forward **figés à l'avance**, puisque les ajuster serait encore du data snooping.
- **Walk-forward efficiency (WFE)** = performance OOS annualisée / performance IS annualisée ; seuil usuel ≥ 50 % (🟠).
- **Analyse en grappe** (cluster, 🟡 https://help.tradestation.com/10_00/eng/tswfo/topics/perform_cluster_analysis.htm) : 3 longueurs d'IS × 3 ratios OOS ; validée à la majorité.
- **Plateaux** :
  - heatmaps par paire de paramètres ;
  - choix sur la moyenne du voisinage (±1 pas) plutôt que sur le maximum ;
  - seuils du projet : au moins 70 % de voisins profitables, et métrique du voisinage ≥ 70 à 80 % du pic.
- **Monte Carlo** (au moins 5 000 à 10 000 tirages ; p50, p95 et p99 du drawdown max) :
  - rebrassage de l'ordre des trades, pour le chemin et le drawdown ;
  - bootstrap avec remise, pour le P&L et le PF ;
  - 10 à 20 % de trades supprimés au hasard ;
  - **bootstrap stationnaire** du P&L journalier (Politis et Romano 1994, 🟡 https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476870), car un bootstrap i.i.d. sous-estime le regroupement des pertes.
  - Un Monte Carlo ne crée pas de régime absent des données.
- **Stress des coûts** : spread × 1,5 puis × 2, slippage des stops augmenté, swaps × 1,5.
- **Régimes** :
  - découpes par année, par tercile de volatilité, par tendance (prix contre moyenne mobile 200 jours), par session, en long et en short séparément, et sur le jour du triple swap ;
  - **benchmarks** : buy & hold, et **entrées aléatoires avec les mêmes sorties**. L'or a dérivé à la hausse : une stratégie acheteuse « gagne » par simple bêta.

### 4.3 Statistiques

- **Sharpe** : calculé sur le **P&L journalier mark-to-market** (jours sans position = 0), annualisé par √252.
  - Jamais par trade × √(trades par an), ni sur des rendements M1.
  - L'autocorrélation peut surestimer un Sharpe annualisé de jusqu'à 65 % (🟡 Lo 2002, https://rpc.cfainstitute.org/research/financial-analysts-journal/2002/the-statistics-of-sharpe-ratios).
- **Sortino** : l'écart à la baisse se calcule sur **toutes** les observations (min(0, r)²). **Calmar** : ne comparer que des périodes de même longueur.
- **Erreur-type du Sharpe** et **seuil t > 3** (Harvey, Liu et Zhu 2016 🟡 https://www.nber.org/papers/w20592), ✅ calcul, i.i.d. :

| Historique | Erreur-type du Sharpe annuel | Sharpe annuel requis pour t > 3 |
|---|---|---|
| 5 ans | ≈ 0,45 | ≥ 1,34 |
| 10 ans | ≈ 0,32 | ≥ 0,95 |
| 15 ans | ≈ 0,26 | ≥ 0,77 |

- **Nombre de trades nécessaire** : n ≥ (t × σ_R / E_R)², avec E_R l'espérance en R et σ_R son écart-type (✅ calcul).

| E_R / σ_R | n pour t = 2 | n pour t = 3 |
|---|---|---|
| 0,05 | 1 600 | 3 600 |
| 0,10 | 400 | 900 |
| 0,15 | 178 | 400 |
| 0,20 | 100 | 225 |

→ 200 trades ne suffisent que si l'edge est déjà net (E_R/σ_R ≥ 0,14 pour t = 2).

- **Budget d'essais, MinBTL** (Bailey, Borwein, López de Prado, Zhu 2014, 🟡 https://www.ams.org/notices/201405/rnoti-p458.pdf) :
  - avec 5 ans de données, pas plus de 45 configurations indépendantes. Au-delà, un Sharpe in-sample de 1 est presque garanti alors que le Sharpe OOS attendu est nul ;
  - table ✅ calculée avec la formule du papier :

| Configurations indépendantes N | E[max Sharpe] pour σ = 1 | Historique minimal pour que le hasard n'atteigne pas un Sharpe de 1 | … de 0,7 |
|---|---|---|---|
| 10 | 1,58 | 2,5 ans | 5,1 ans |
| 45 | 2,24 | 5,0 ans | 10,2 ans |
| 100 | 2,53 | 6,4 ans | 13,1 ans |
| 200 | 2,77 | 7,6 ans | 15,6 ans |
| 1 000 | 3,26 | 10,6 ans | 21,6 ans |

- **PSR et Deflated Sharpe Ratio** :
  - le DSR corrige le biais de sélection (N essais) et la non-normalité (🟡 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) ;
  - PSR et MinTRL : 🟡 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643.
- **PBO par CSCV** : probabilité que la sélection faite en IS soit sur-ajustée (🟡 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253). Méthode (⚪) :
  1. matrice T × N des P&L, coupée en S = 16 blocs (12 870 combinaisons) ;
  2. pour chaque combinaison, on prend la meilleure configuration en IS et on mesure son rang en OOS ;
  3. PBO = part des cas où ce rang est sous la médiane.
- **Autres tests** :
  - haircut du Sharpe par Bonferroni, Holm ou BHY (Harvey et Liu 2015, 🟡 https://papers.ssrn.com/abstract=2345489). Le haircut n'est pas linéaire : un haircut forfaitaire de 50 % est une erreur ;
  - White's Reality Check (2000) et test SPA de Hansen (2005, 🟡 https://www.tandfonline.com/doi/abs/10.1198/073500105000000063) ;
  - test de permutation de Masters, **en relançant toute l'optimisation** à chaque permutation (⚪).
- **Registre d'essais** (append-only, SQLite ou Parquet) :
  - une ligne par essai : id, date, commit git, stratégie, paramètres, période, version du modèle de coûts, métriques, et **série de P&L journalier** (indispensable pour DSR, PBO et SPA) ;
  - ré-exécuter après une correction de bug n'est pas un nouvel essai ; **tout changement de règle en est un** ;
  - le N déclaré couvre les 6 stratégies.

### 4.4 Références clés (usage dans le projet)

- **Pardo** (2008) : walk-forward, WFE, robustesse (paramètres au centre d'un plateau, peu de degrés de liberté).
- **Chan** (2008/2021, 2013, 2017) :
  - pièges du backtest ;
  - tests de mean reversion (ADF, Hurst, variance ratio, demi-vie) et de momentum (corrélation entre période d'observation et période de détention) ;
  - demi-Kelly au plus, en plafond seulement (🟡 http://epchan.blogspot.com/2006/10/how-much-leverage-should-you-use.html).
  - → Pour chaque stratégie, déclarer **a priori** sa logique (momentum ou mean reversion) et la vérifier à son horizon avant toute optimisation.
- **López de Prado** (AFML, Wiley 2018 🟡 https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086) :
  - triple barrière et méta-labeling (Phase 7) ;
  - purge et embargo aux frontières IS/OOS : un trade est un intervalle [entrée, sortie], et l'embargo vaut au moins la durée maximale d'un trade ;
  - CPCV : donne une **distribution** du Sharpe OOS et non un seul chemin ;
  - « The 10 Reasons Most Machine Learning Funds Fail » (🟡 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816).
- **False Strategy Theorem** : avec assez d'essais, aucun Sharpe ne suffit à rejeter l'hypothèse « stratégie fausse » (🟡 https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3177057).

### 4.5 Évaluation des critères de passage en démo du `CLAUDE.md`

| Critère actuel | Verdict | Renforcement proposé (à valider par toi, Q7) |
|---|---|---|
| OOS après coûts : PF ≥ 1,3 et espérance > 0 | Bon plancher économique, mais ce n'est pas un test statistique | Borne basse de l'IC 90 % (bootstrap) > 1,0 ; PF ≥ 1,15 avec des coûts × 2 |
| ≥ 200 trades | Minimum | n ≥ (t × σ_R / E_R)² (§4.3) ; trades présents dans au moins 70 % des années |
| Max DD ≤ 15 % | Nécessaire | Drawdown mark-to-market ; p95 Monte Carlo ≤ 15 % ; juger aussi Sharpe et Calmar |
| Majorité des fenêtres walk-forward positives | **Faible** : 7/12 arrive avec p ≈ 0,39 à pile ou face (✅) | ≥ 70 % de fenêtres positives (≈ 76 % attendu pour un vrai Sharpe de 1 sur des fenêtres de 6 mois ✅) ; WFE ≥ 50 % ; aucune fenêtre > 50 % du profit |
| 5 meilleurs trades < 30 % du profit net | Bon contrôle des outliers | Profit hors top 5 > 0 ; aucun mois > 20 % du profit |
| DD Monte Carlo 95 % acceptable | Bon, mais méthode et horizon non définis | ≥ 10 000 tirages, rebrassage + bootstrap stationnaire, horizon d'1 an |
| **À ajouter** | — | Registre d'essais + **pré-enregistrement** (règles, grilles, critères, avant tout backtest) ; **DSR ≥ 0,95** ; **PBO ≤ 0,2** (seuil de projet) ; t-stat OOS ≥ 2 (≥ 3 si N est élevé) ; hold-out de 2 ans ou plus, utilisé une fois ; benchmarks buy & hold et entrée aléatoire |

### 4.6 Protocole proposé (Phases 3-4)

0. **Pré-enregistrement** versionné de la logique, des règles, des grilles (3 à 5 valeurs par paramètre) et des critères.
1. **Données** :
   - barres M1 bid avec spread, ticks réels sur la période récente, tout en UTC ;
   - contrôle qualité ;
   - **snapshot figé** identifié par un hash.
2. **Moteur** :
   - test de troncature ;
   - parité avec MT5 en mode « Every tick based on real ticks » sur un échantillon (🟠 https://www.metatrader5.com/en/terminal/help/algotrading/testing).
3. **Découpage** entre développement et hold-out scellé.
4. **Screening** sur la période de développement, chaque essai étant inscrit au registre.
   - Budget : au plus environ 200 configurations par stratégie et 1 000 au total ; N effectif estimé.
5. **Validation** : walk-forward rolling + anchored + grappe 3×3, heatmaps, Monte Carlo, stress des coûts, régimes (CPCV en option).
6. **Statistiques** : PSR/DSR, PBO, t-stat, SPA, permutation.
7. **Hold-out, une seule fois**, pour au plus 1 ou 2 stratégies. En cas d'échec, on ne « répare » pas la stratégie sur le hold-out.
8. **Démo** : au moins 4 à 8 semaines **et** 50 trades (`CLAUDE.md`), à allonger si la fréquence de trades est faible. Comparer au backtest rejoué sur les mêmes ticks.

---

## 5. Conséquences pour la conception (propositions, rien n'est codé)

1. **Versions** : Python 3.13 (Q5) ; `MetaTrader5==5.0.6231` épinglé, en extra Windows, avec import paresseux dans `MT5Broker`.
2. **Constantes MT5** : le package n'est pas importable sous Linux. → Module miroir `goldbot/broker/mt5_constants.py`, copié de l'Annexe A et daté de la version 5.0.6231, plus un test Windows qui compare chaque valeur au vrai package. On y ajoute les masques absents (`SYMBOL_FILLING_*`, `SYMBOL_EXPIRATION_*`, `SYMBOL_ORDER_*`), à confirmer sur démo.
3. **Passerelle MT5 mono-thread** :
   - reconnexion avec backoff ;
   - détection des ticks figés, seulement pendant les heures de cotation ;
   - `version()` et `__version__` journalisées.
4. **Temps** :
   - stockage en UTC ;
   - règle « serveur = New York + 7 h » paramétrable, avec contrôle du décalage mesuré au démarrage (arrêt en cas d'écart) ;
   - sessions et news dans leur fuseau IANA ; affichage en `Europe/Paris` ;
   - dépendance `tzdata` obligatoire sous Windows (🟡 https://docs.python.org/3/library/zoneinfo.html) ;
   - tests unitaires sur les semaines de décalage des changements d'heure (Annexe B).
5. **Symbole** : procédure du §1.4, dump JSON de `symbol_info`, refus de démarrer si le choix est ambigu.
6. **Envoi d'ordres** :
   - remplissage choisi par `order_check` (§1.10) ;
   - prix arrondis à `trade_tick_size`, volume arrondi vers le bas ;
   - distances de stops et zone de gel contrôlées sur un tick frais ;
   - politique de retcodes du §1.9 ;
   - `order_check` OK ⇔ `retcode == 0`.
7. **Idempotence et réconciliation** : id du signal dans `comment` (longueur à tester 🧪) et dans SQLite ; réconciliation obligatoire après tout état incertain et au démarrage.
8. **Calendrier** :
   - service MQL5 qui écrit un CSV atomique dans `Common\Files` : historique mois par mois et `CalendarValueLast` en live ;
   - conversion en UTC avec le décalage **au moment de l'export**, et contrôle NFP/CPI à 08:30 ET ;
   - le même fichier sert au backtest et au live.
9. **Config de marché** : horaires Axi des métaux (pause de 23:59 à 01:01 serveur), jours fériés, blackouts (rollover, news Tier-1 et Tier-2, vendredi soir, dimanche soir), seuils de spread par heure.
10. **Risque** :
    - taille = risque / `order_calc_profit(1 lot jusqu'au SL)` ;
    - volume arrondi vers le bas, et trade ignoré s'il est sous `volume_min` ;
    - `order_calc_margin` + `order_check` avant chaque `order_send` ;
    - contrôle de l'equity minimale (§3.10).
11. **Dépendances envisagées**, à justifier une par une en Phase 1 :
    - `numpy` : requis par MetaTrader5, et calculs ;
    - `pandas` : séries temporelles ;
    - `pyarrow` : Parquet ;
    - `pydantic` : validation de la config, exigée par le `CLAUDE.md` ;
    - `PyYAML` : `settings.yaml` ;
    - `python-dotenv` : `.env` ;
    - `tzdata` : `zoneinfo` sous Windows ;
    - `matplotlib` : courbe d'equity en PNG ;
    - `pytest` : tests.
    - **Écartés** : TA-Lib (interdit), `mt5linux` (binaire tiers), paquets Dukascopy (à évaluer en Phase 2 : `dukascopy-python` 4.0.1 d'avril 2025, un seul mainteneur ✅ PyPI ; `duka` abandonné depuis 2016).
    - Telegram : via `urllib` de la stdlib ou `requests`, à décider en Phase 5.
12. **Recherche** : registre d'essais, pré-enregistrement, DSR/PBO dans le rapport standard.

**Couverture de la check-list §15 du `CLAUDE.md`**

| Item | Section |
|---|---|
| Marché fermé, week-end, fériés | §2.5, §2.6, §3.4, §3.6 |
| Changements d'heure décalés | §1.12, Annexe B |
| Gap du lundi | §3.6, §1.10 |
| Spread au rollover et sur les news | §3.5, §3.7 |
| Déconnexion, VPS | §1.14 |
| Algo Trading désactivé | §1.3 |
| Max bars | §1.5 |
| Suffixe du symbole | §1.4, §2.3 |
| Hedging ou netting | §1.11 |
| Fills partiels, requotes, slippage | §1.9, §1.10 |
| Margin call et stop-out | §2.7 |
| Triple swap | §2.4 |
| Ticks figés | §1.14 |
| Horloge système | §1.12 |
| Arrondis au tick | §1.10 |
| Double signal | §5.7 |
| Redémarrage avec positions ouvertes | §5.7 |
| Trades manuels | §1.6, §1.11, Annexe A.5 |
| Devise du compte ≠ USD | §1.7, §2.2 |
| Commission et swaps réels | §2.3, §2.4, §1.11 |

---

## 6. Questions ouvertes

**Réponses reçues le 2026-09-29**
- Q1 (partiel) : **compte démo MT5 Axi actif**, en mode **Hedge**, terminal en français. D'après la capture d'écran de l'utilisateur, l'entité affichée est **AxiCorp Financial Services Pty Ltd**, l'entité australienne (ASIC), et non AxiTrader Ltd (SVG) comme le laissait penser §2.1 (🟠). L'entité d'un futur compte réel reste à vérifier dans le contrat.
- Q2 : compte **Standard**, capital **5 000 €**. Conséquences sur la taille des positions : §3.10.
- Q3 : le bot tournera sur le **PC** de l'utilisateur (un portable d'après la capture), pas sur un VPS.
  - Plage de cotation à couvrir : du lundi 00:01 au vendredi 22:58, heure de Paris ; du dimanche 23:01 au vendredi 21:58 pendant les semaines de décalage des changements d'heure (✅ calcul avec la règle serveur).
  - À prévoir en Phase 6 : PC branché sur secteur, veille et fermeture du capot désactivées, redémarrages Windows planifiés hors marché, connexion internet stable.
- Q4 à Q10 : toujours ouvertes.

1. **(Bloquant)** Quel est ton pays de résidence, et quelle entité Axi figure dans ton contrat ou ton portail (AxiTrader Ltd SVG ? Solaris EMEA ?) ? MT5, **réel et démo**, t'est-il proposé ? As-tu déjà un compte démo MT5 Axi, et quel est le **nom exact du serveur** ?
2. Quel **type de compte** vises-tu (Standard ou Pro) et quelle **devise** (USD évite le bruit de change) ? Quel **ordre de grandeur de capital**, en démo puis en réel ? D'après le tableau du §3.10, c'est ce qui fixe les distances de SL tradables.
3. Le bot tournera-t-il sur ton **PC Windows** (allumé 24 h/24, 5 jours sur 7 ?) ou sur un **VPS** ? Où ?
4. **Réseau de l'environnement cloud** : peux-tu autoriser au moins les domaines suivants, pour que je relise les pages officielles (§7) ?
   - `mql5.com`, `metatrader5.com` ;
   - `axi.com`, `help.axi.com`, `help-eu.axi.com`, `support.axi.com` ;
   - `lbma.org.uk`, `cmegroup.com`, `gold.org` ;
   - `federalreserve.gov`, `bls.gov`, `dukascopy.com`.
   Sinon, la vérification se fera empiriquement en Phase 1.
5. **Python 3.13** sur Windows et Codespaces : OK ?
6. **Horizon** : S6 (swing H1-H4) implique de garder des positions plusieurs jours, y compris le week-end (swaps, triple swap, gaps). Est-ce autorisé, ou bien intraday seulement avec clôture systématique avant le week-end ?
7. **Critères de validation** : OK pour les renforcer comme au §4.5 (≥ 70 % de fenêtres WF positives, DSR, PBO, registre d'essais, pré-enregistrement, hold-out ≥ 2 ans) ?
8. **Données longues** : OK pour utiliser Dukascopy (gratuit, en UTC, CGU à vérifier) pour la recherche, avec une validation finale sur les données Axi ?
9. **Documents** : peux-tu me fournir le Client Agreement, le PDS ou Product Schedule MT5, et l'Order Execution Policy de ton entité ? Colle le texte, ou dépose-les dans un dossier non versionné. Ils contiennent les règles EA, scalping, news et exécution.
10. **Arborescence** : la racine du repo `Trading` tient lieu de `gold-bot/` du plan, sans sous-dossier. OK ?

---

## 7. À relire quand l'accès réseau sera ouvert

Par ordre de priorité :

1. Doc Python MT5 : https://www.mql5.com/en/docs/python_metatrader5 et la page de chaque fonction (`mt5initialize_py`, `mt5ordersend_py`, `mt5ordercheck_py`, `mt5copyratesfrom_py`, `mt5historydealsget_py`, etc.).
2. Codes retour : https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes
3. Propriétés des ordres (remplissage, expiration) : https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties
4. Propriétés des symboles (masques, stops et freeze level, calc modes) : https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants
5. Propriétés du compte : https://www.mql5.com/en/docs/constants/environment_state/accountinformation
6. Calendrier : https://www.mql5.com/en/docs/calendar et https://www.mql5.com/en/book/advanced/calendar
7. Axi :
   - horaires des métaux, swaps, stop-out, MT5 par pays, Standard ou Pro, calendrier des fériés (liens aux §2.1 à §2.7) ;
   - **Client Agreement, PDS et Order Execution Policy** de ton entité.
8. LBMA, CME (horaires et fériés 2026-2027), BLS, BEA et Fed (calendriers 2026-2027).
9. Articles cités seulement par leur titre : Pardo chap. 11 (ratios IS/OOS), « Did the New Fix, Fix the Fix? » (https://www.ssrn.com/abstract=2657767).

---
## Annexe A — Référence API vérifiée sur le package officiel (✅)

Source : wheel officiel `metatrader5-5.0.6231-cp311-cp311-win_amd64.whl` téléchargé depuis PyPI le 2026-09-28, empreinte SHA-256 `1e0fb5e43437f426be3e5aef614f6798a918377db4cdb380593a66d1908f918d` (identique à celle publiée par PyPI). Analyse de `MetaTrader5/__init__.py` (source Python lisible) et des chaînes du module natif `_core.cp311-win_amd64.pyd` (docstrings, noms de champs, messages d'erreur, imports Windows).
URL : https://pypi.org/project/MetaTrader5/ — JSON : https://pypi.org/pypi/MetaTrader5/json

### A.1 Fonctions exportées et signatures (docstrings du binaire, texte exact)

| Fonction | Docstring officielle (signature) | Description officielle (binaire) |
|---|---|---|
| `initialize` | `initialize([path],[server="SERVER"],[login=LOGIN],[password="PASSWORD"])` — mots-clés acceptés en plus : `timeout`, `portable` | Establish connection to MetaTrader 5 terminal |
| `login` | `login(login,[server="SERVER"],[password="PASSWORD"])` — accepte aussi `timeout` | Log in to specific account |
| `shutdown` | `shutdown()` | Disconnect from MetaTrader 5 terminal |
| `version` | `version()` | Get MetaTrader 5 terminal version |
| `last_error` | `last_error()` | Return last error code and description |
| `terminal_info` | `terminal_info()` | Get parameters of MetaTrader 5 terminal |
| `account_info` | `account_info()` | Return information on the current account |
| `symbols_total` | `symbols_total()` | Return the number of all available symbols |
| `symbols_get` | `symbols_get([group="GROUP"])` | Return all available symbols or filtred by group |
| `symbol_info` | `symbol_info(symbol)` | Return full information for specified symbol |
| `symbol_info_tick` | `symbol_info_tick(symbol)` | Return current prices of specified symbol |
| `symbol_select` | `symbol_select(symbol,[enable])` | Select symbol in Market Watch window or remove symbol from the window |
| `market_book_add` / `_get` / `_release` | `market_book_add(symbol)` etc. | Depth of Market (inutile pour ce projet) |
| `copy_rates_from` | `copy_rates_from(symbol, timeframe, date_from, count)` | Get bars starting from specific date |
| `copy_rates_from_pos` | `copy_rates_from_pos(symbol, timeframe, start_pos, count)` | Get bars starting from specified position |
| `copy_rates_range` | `copy_rates_range(symbol, timeframe, date_from, date_to)` | Get bars from specified period |
| `copy_ticks_from` | `copy_ticks_from(symbol, date_from, count, flags)` | Get ticks starting from specific date |
| `copy_ticks_range` | `copy_ticks_range(symbol, date_from, date_to, flags)` | Get ticks from specified period |
| `positions_total` | `positions_total()` | Return the number of open positions |
| `positions_get` | `positions_get([symbol="SYMBOL"],[ticket=TICKET],[group="GROUP"])` | Return all open positions, can be filtered by symbol, group or ticket |
| `orders_total` | `orders_total()` | Return the number of orders |
| `orders_get` | `orders_get([symbol="SYMBOL"],[ticket=TICKET],[group="GROUP"])` | Return all orders, can be filtered by symbol, group or ticket |
| `history_orders_total` | `history_orders_total(date_from, date_to)` | Return the number of orders in selected range from history |
| `history_orders_get` | `history_orders_get([date_from, date_to, [group="GROUP"]],[position=POSITION],[ticket=TICKET])` | Return orders in selected range from history or filtered by position id, ticket |
| `history_deals_total` | `history_deals_total(date_from, date_to)` | Return the number of deals in selected range from history |
| `history_deals_get` | `history_deals_get([date_from, date_to, [group="GROUP"]],[position=POSITION],[ticket=TICKET])` | Return deals in selected range from history or filtered by position id, ticket |
| `order_calc_margin` | `order_calc_margin(action, symbol, volume, price,)` | Calculate margin required for specified order |
| `order_calc_profit` | `order_calc_profit(action, symbol, volume, price_open, price_close)` | Calculate profit for the current account in the current market conditions based on parameters passed |
| `order_check` | `order_check(request)` | Check if there are enough funds to execute required trading operation |
| `order_send` | `order_send(request)` | Send trading requests to server |

Observations tirées du binaire :
- `date_from` / `date_to` n'existent pas comme mots-clés (absents de la table des noms d'arguments) → **arguments positionnels uniquement**. Le binaire contient « Unnamed arguments not allowed » : pour `positions_get` / `orders_get`, les filtres se passent **uniquement par mot-clé** (`symbol=`, `ticket=`, `group=`).
- Messages d'erreur présents : `Terminal: Invalid params`, `Terminal: Out of memory`, `Terminal: Not found`, `Terminal: Incompatible versions, please install the latest version of Terminal and Python module`, `Terminal: Authorization failed`, `Terminal: Unsupported function`, `IPC call failed`, `IPC send failed`, `IPC recv failed`, `IPC timeout`, `IPC initialize failed, %S`, `No IPC connection`, `Terminal: Call failed`, `Unsupported authorization mode, OTP or certificate password needed`, `Auto-Trading using Python API disabled in Terminal`.
- Imports Windows : `CreateProcessW`, `CreateToolhelp32Snapshot`/`Process32FirstW`/`Process32NextW`, `QueryFullProcessImageNameW`, `WaitNamedPipeW`, `CreateFileW`, `ReadFile`/`WriteFile`. Le module cherche ou lance le processus du terminal, puis dialogue avec lui par **named pipe local**. Il faut donc la même machine Windows et, en pratique, la même session utilisateur (⚪ déduction, 🧪 à confirmer).
- Le binaire référence `numpy._core._multiarray_umath` (NumPy 2) et `numpy.core._multiarray_umath` (NumPy 1). Il est conçu pour fonctionner avec NumPy 1.x et 2.x (🧪 à confirmer à l'installation).

### A.2 Codes de `last_error()` (✅ `__init__.py`)

| Constante | Valeur | Commentaire officiel |
|---|---|---|
| `RES_S_OK` | 1 | generic success |
| `RES_E_FAIL` | -1 | generic fail |
| `RES_E_INVALID_PARAMS` | -2 | invalid arguments/parameters |
| `RES_E_NO_MEMORY` | -3 | no memory condition |
| `RES_E_NOT_FOUND` | -4 | no history |
| `RES_E_INVALID_VERSION` | -5 | invalid version |
| `RES_E_AUTH_FAILED` | -6 | authorization failed |
| `RES_E_UNSUPPORTED` | -7 | unsupported method |
| `RES_E_AUTO_TRADING_DISABLED` | -8 | auto-trading disabled |
| `RES_E_INTERNAL_FAIL` | -10000 | internal IPC general error |
| `RES_E_INTERNAL_FAIL_SEND` | -10001 | internal IPC send failed |
| `RES_E_INTERNAL_FAIL_RECEIVE` | -10002 | internal IPC recv failed |
| `RES_E_INTERNAL_FAIL_INIT` | -10003 | internal IPC initialization fail |
| `RES_E_INTERNAL_FAIL_CONNECT` | -10004 | internal IPC no ipc |
| `RES_E_INTERNAL_FAIL_TIMEOUT` | -10005 | internal timeout |

### A.3 Champs des structures (noms ✅ présents dans le binaire ; rattachement structure ↔ champ selon la doc MQL5, 🧪 à confirmer par `._asdict()` en Phase 1)

182 noms de champs vérifiés, aucun absent.

- **TerminalInfo** : community_account, community_connection, connected, dlls_allowed, trade_allowed, tradeapi_disabled, email_enabled, ftp_enabled, notifications_enabled, mqid, build, maxbars, codepage, ping_last, community_balance, retransmission, company, name, language, path, data_path, commondata_path
- **AccountInfo** : login, trade_mode, leverage, limit_orders, margin_so_mode, trade_allowed, trade_expert, margin_mode, currency_digits, fifo_close, balance, credit, profit, equity, margin, margin_free, margin_level, margin_so_call, margin_so_so, margin_initial, margin_maintenance, assets, liabilities, commission_blocked, name, server, currency, company
- **SymbolInfo** (sélection utile) : select, visible, digits, spread, spread_float, trade_calc_mode, trade_mode, trade_stops_level, trade_freeze_level, trade_exemode, swap_mode, swap_rollover3days, expiration_mode, filling_mode, order_mode, order_gtc_mode, bid, ask, point, trade_tick_value, trade_tick_value_profit, trade_tick_value_loss, trade_tick_size, trade_contract_size, volume_min, volume_max, volume_step, volume_limit, swap_long, swap_short, margin_initial, margin_maintenance, margin_hedged, currency_base, currency_profit, currency_margin, description, name, path
- **Tick** (et dtype de `copy_ticks_*`) : time, bid, ask, last, volume, time_msc, flags, volume_real
- **Barres** (dtype de `copy_rates_*`) : time, open, high, low, close, tick_volume, spread, real_volume
- **TradePosition** : ticket, time, time_msc, time_update, time_update_msc, type, magic, identifier, reason, volume, price_open, sl, tp, price_current, swap, profit, symbol, comment, external_id
- **TradeOrder** : ticket, time_setup, time_setup_msc, time_done, time_done_msc, time_expiration, type, type_time, type_filling, state, magic, position_id, position_by_id, reason, volume_initial, volume_current, price_open, sl, tp, price_current, price_stoplimit, symbol, comment, external_id
- **TradeDeal** : ticket, order, time, time_msc, type, entry, magic, position_id, reason, volume, price, commission, swap, profit, fee, symbol, comment, external_id
- **TradeRequest** (clés du dict passé à `order_send` / `order_check`, messages « Invalid "x" argument » du binaire) : action, magic, order, symbol, volume, price, stoplimit, sl, tp, deviation, type, type_filling, type_time, expiration, comment, position, position_by
- **OrderSendResult** : retcode, deal, order, volume, price, bid, ask, comment, request_id, retcode_external, request
- **OrderCheckResult** : retcode, balance, equity, profit, margin, margin_free, margin_level, comment, request
- Toutes les structures exposent `._asdict()` (« Convert named tuple object to dictionary »).

### A.4 Constantes (valeurs ✅ `__init__.py`)

- **Timeframes** : `TIMEFRAME_M1`=1, M2=2, M3=3, M4=4, M5=5, M6=6, M10=10, M12=12, M15=15, M20=20, M30=30, `TIMEFRAME_H1`=16385, H2=16386, H3=16387, H4=16388, H6=16390, H8=16392, H12=16396, `TIMEFRAME_D1`=16408, `TIMEFRAME_W1`=32769, `TIMEFRAME_MN1`=49153. ⚠️ H1 ≠ 60 : ne jamais faire d'arithmétique sur ces constantes.
- **Copie de ticks** : `COPY_TICKS_ALL`=-1, `COPY_TICKS_INFO`=1, `COPY_TICKS_TRADE`=2. **Flags de tick** : BID=0x02, ASK=0x04, LAST=0x08, VOLUME=0x10, BUY=0x20, SELL=0x40.
- **Actions** : `TRADE_ACTION_DEAL`=1, `PENDING`=5, `SLTP`=6, `MODIFY`=7, `REMOVE`=8, `CLOSE_BY`=10.
- **Types d'ordre** : `ORDER_TYPE_BUY`=0, SELL=1, BUY_LIMIT=2, SELL_LIMIT=3, BUY_STOP=4, SELL_STOP=5, BUY_STOP_LIMIT=6, SELL_STOP_LIMIT=7, CLOSE_BY=8. **Positions** : `POSITION_TYPE_BUY`=0, `POSITION_TYPE_SELL`=1.
- **Remplissage** : `ORDER_FILLING_FOK`=0, `IOC`=1, `RETURN`=2, `BOC`=3. **Durée** : `ORDER_TIME_GTC`=0, `DAY`=1, `SPECIFIED`=2, `SPECIFIED_DAY`=3.
- **États d'ordre** : STARTED=0, PLACED=1, CANCELED=2, PARTIAL=3, FILLED=4, REJECTED=5, EXPIRED=6, REQUEST_ADD=7, REQUEST_MODIFY=8, REQUEST_CANCEL=9.
- **Raisons** : `ORDER_REASON_*` / `DEAL_REASON_*` : CLIENT=0, MOBILE=1, WEB=2, EXPERT=3, SL=4, TP=5, SO=6 (+ `DEAL_REASON_ROLLOVER`=7, VMARGIN=8, SPLIT=9). `POSITION_REASON_*` : CLIENT=0, MOBILE=1, WEB=2, EXPERT=3.
- **Deals** : `DEAL_TYPE_BUY`=0, SELL=1, BALANCE=2, CREDIT=3, CHARGE=4, CORRECTION=5, BONUS=6, COMMISSION=7, COMMISSION_DAILY=8, COMMISSION_MONTHLY=9, COMMISSION_AGENT_DAILY=10, COMMISSION_AGENT_MONTHLY=11, INTEREST=12, BUY_CANCELED=13, SELL_CANCELED=14, `DEAL_DIVIDEND`=15, `DEAL_DIVIDEND_FRANKED`=16, `DEAL_TAX`=17. **Entrées** : `DEAL_ENTRY_IN`=0, OUT=1, INOUT=2, OUT_BY=3.
- **Symbole** : `SYMBOL_CALC_MODE_FOREX`=0, FUTURES=1, CFD=2, CFDINDEX=3, CFDLEVERAGE=4, FOREX_NO_LEVERAGE=5, EXCH_*=32…39, SERV_COLLATERAL=64 ; `SYMBOL_TRADE_MODE_DISABLED`=0, LONGONLY=1, SHORTONLY=2, CLOSEONLY=3, FULL=4 ; `SYMBOL_TRADE_EXECUTION_REQUEST`=0, INSTANT=1, MARKET=2, EXCHANGE=3 ; `SYMBOL_SWAP_MODE_DISABLED`=0, POINTS=1, CURRENCY_SYMBOL=2, CURRENCY_MARGIN=3, CURRENCY_DEPOSIT=4, INTEREST_CURRENT=5, INTEREST_OPEN=6, REOPEN_CURRENT=7, REOPEN_BID=8 ; `DAY_OF_WEEK_SUNDAY`=0 … SATURDAY=6 (pour `swap_rollover3days`) ; `SYMBOL_ORDERS_GTC`=0, DAILY=1, DAILY_NO_STOPS=2 ; `SYMBOL_CHART_MODE_BID`=0, LAST=1.
- **Compte** : `ACCOUNT_TRADE_MODE_DEMO`=0, CONTEST=1, REAL=2 ; `ACCOUNT_STOPOUT_MODE_PERCENT`=0, MONEY=1 ; `ACCOUNT_MARGIN_MODE_RETAIL_NETTING`=0, EXCHANGE=1, RETAIL_HEDGING=2.
- **Codes retour trading** : REQUOTE=10004, REJECT=10006, CANCEL=10007, PLACED=10008, DONE=10009, DONE_PARTIAL=10010, ERROR=10011, TIMEOUT=10012, INVALID=10013, INVALID_VOLUME=10014, INVALID_PRICE=10015, INVALID_STOPS=10016, TRADE_DISABLED=10017, MARKET_CLOSED=10018, NO_MONEY=10019, PRICE_CHANGED=10020, PRICE_OFF=10021, INVALID_EXPIRATION=10022, ORDER_CHANGED=10023, TOO_MANY_REQUESTS=10024, NO_CHANGES=10025, SERVER_DISABLES_AT=10026, CLIENT_DISABLES_AT=10027, LOCKED=10028, FROZEN=10029, INVALID_FILL=10030, CONNECTION=10031, ONLY_REAL=10032, LIMIT_ORDERS=10033, LIMIT_VOLUME=10034, INVALID_ORDER=10035, POSITION_CLOSED=10036, INVALID_CLOSE_VOLUME=10038, CLOSE_ORDER_EXIST=10039, LIMIT_POSITIONS=10040, REJECT_CANCEL=10041, LONG_ONLY=10042, SHORT_ONLY=10043, CLOSE_ONLY=10044, FIFO_CLOSE=10045.
- **Absents du package Python** (valeurs MQL5 à redéfinir nous-mêmes, 🟡/⚪ selon §1) : les masques `SYMBOL_FILLING_*`, `SYMBOL_EXPIRATION_*`, `SYMBOL_ORDER_*` et le code 10046 (`TRADE_RETCODE_HEDGE_PROHIBITED` côté MQL5). Toute fonction MQL5 non listée en A.1, dont le **calendrier économique**, est inaccessible depuis Python.

### A.5 Helpers Python dangereux présents dans le package (✅ lus dans `__init__.py`)

`MetaTrader5.Buy(symbol, volume, price=None, *, comment=None, ticket=None)`, `Sell(...)` et `Close(symbol, *, comment=None, ticket=None)` construisent un ordre au marché avec `deviation=10`, **sans `sl`, sans `tp`, sans `magic`, sans `type_filling`**, et bouclent jusqu'à 10 fois sur REQUOTE / PRICE_OFF. `Close()` traite toutes les positions du symbole, y compris les trades manuels. **Interdits dans ce projet** : ils violent la règle 3 (SL serveur dans la requête d'ouverture) et la règle 6 (magic / ne pas toucher aux trades manuels).

---

## Annexe B — Changements d'heure et heures clés (✅ calculés avec la base IANA via `zoneinfo`)

Transitions (heure UTC de bascule) :

| Année | New York (`America/New_York`) | Londres (`Europe/London`), = Paris pour les dates |
|---|---|---|
| 2024 | 10 mars 07:00 UTC → EDT ; 3 nov 06:00 UTC → EST | 31 mars 01:00 UTC → BST ; 27 oct 01:00 UTC → GMT |
| 2025 | 9 mars → EDT ; 2 nov → EST | 30 mars → BST ; 26 oct → GMT |
| 2026 | 8 mars → EDT ; 1er nov → EST | 29 mars → BST ; 25 oct → GMT |
| 2027 | 14 mars → EDT ; 7 nov → EST | 28 mars → BST ; 31 oct → GMT |

« Semaines décalées » où l'écart Londres–New York vaut **4 h au lieu de 5 h** :
- 2025 : 9 → 29 mars et 26 oct → 1er nov
- 2026 : 8 → 28 mars et 25 → 31 oct
- 2027 : 14 → 27 mars et 31 oct → 6 nov

Heures clés converties (exemples 2026) :

| Événement (fuseau de référence) | 15 janv. (hiver) | 20 mars (décalé) | 15 juil. (été) | 28 oct. (décalé) |
|---|---|---|---|---|
| LBMA Gold Price AM 10:30 Londres | 10:30 UTC · 11:30 Paris | 10:30 UTC · 11:30 Paris | 09:30 UTC · 11:30 Paris | 10:30 UTC · 11:30 Paris |
| LBMA Gold Price PM 15:00 Londres | 15:00 UTC · 16:00 Paris | 15:00 UTC · 16:00 Paris | 14:00 UTC · 16:00 Paris | 15:00 UTC · 16:00 Paris |
| Stats US 08:30 New York | 13:30 UTC · 14:30 Paris | 12:30 UTC · **13:30** Paris | 12:30 UTC · 14:30 Paris | 12:30 UTC · **13:30** Paris |
| « Ouverture COMEX » 08:20 New York | 13:20 UTC · 14:20 Paris | 12:20 UTC · **13:20** Paris | 12:20 UTC · 14:20 Paris | 12:20 UTC · **13:20** Paris |
| FOMC 14:00 New York | 19:00 UTC · 20:00 Paris | 18:00 UTC · **19:00** Paris | 18:00 UTC · 20:00 Paris | 18:00 UTC · **19:00** Paris |
| Rollover / pause Globex 17:00 New York | 22:00 UTC · 23:00 Paris | 21:00 UTC · **22:00** Paris | 21:00 UTC · 23:00 Paris | 21:00 UTC · **22:00** Paris |

Conséquence : un filtre codé « 14:30 Paris » ou « 13:30 UTC » pour les chiffres US se trompe plusieurs semaines par an. Il faut définir chaque événement ou session dans **son** fuseau (`America/New_York`, `Europe/London`) et convertir en UTC jour par jour.

---

## Annexe C — Bibliographie et sources

Les URL citées dans le texte y figurent point par point. Voici la liste consolidée des sources principales. Le niveau de confiance est indiqué dans le texte ; aucune de ces pages n'a pu être ouverte depuis cet environnement.

### C.1 MetaTrader 5, MQL5 et package Python
- Package PyPI (✅ analysé) : https://pypi.org/project/MetaTrader5/
- Doc Python MT5 : https://www.mql5.com/en/docs/python_metatrader5
  - Pages de fonctions citées : `mt5initialize_py`, `mt5terminalinfo_py`, `mt5accountinfo_py`, `mt5symbolsget_py`, `mt5symbolinfo_py`, `mt5symbolinfotick_py`, `mt5copyratesfrom_py`, `mt5copyratesfrompos_py`, `mt5positionsget_py`, `mt5ordersget_py`, `mt5historydealsget_py`, `mt5historyordersget_py`, `mt5ordercheck_py`, `mt5ordersend_py`.
  - Préfixe de ces pages : https://www.mql5.com/en/docs/python_metatrader5/
- Structures de trading :
  - https://www.mql5.com/en/docs/constants/structures/mqltraderequest
  - https://www.mql5.com/en/docs/constants/structures/mqltraderesult
  - https://www.mql5.com/en/docs/constants/structures/mqltradecheckresult
- Codes retour : https://www.mql5.com/en/docs/constants/errorswarnings/enum_trade_return_codes
- Propriétés des ordres : https://www.mql5.com/en/docs/constants/tradingconstants/orderproperties
- Propriétés des symboles : https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants
- Propriétés du compte : https://www.mql5.com/en/docs/constants/environment_state/accountinformation
- État du terminal : https://www.mql5.com/en/docs/constants/environment_state/terminalstatus
- Permissions de trading : https://www.mql5.com/en/docs/runtime/tradepermission
- OrderSend et OrderCheck :
  - https://www.mql5.com/en/docs/trading/ordersend
  - https://www.mql5.com/en/docs/trading/ordercheck
- Calendrier économique :
  - https://www.mql5.com/en/docs/calendar
  - https://www.mql5.com/en/docs/calendar/calendarvaluehistory
  - https://www.mql5.com/en/docs/constants/structures/mqlcalendar
- Fichiers : https://www.mql5.com/en/docs/files/fileopen
- MQL5 Book (MetaQuotes) :
  - Python : https://www.mql5.com/en/book/advanced/python/python_init, `python_terminal_info`, `python_account_info`, `python_symbols`, `python_copyrates`, `python_ordercheck_ordersend`
  - Symboles : https://www.mql5.com/en/book/automation/symbols/symbols_execution_filling, `symbols_expiration`, `symbols_spreads_levels`
  - Ordres : https://www.mql5.com/en/book/automation/experts/experts_mqltraderequest, `experts_market_buy_sell`, `experts_execution_filling`, `experts_ordercheck`, `experts_mqltradecheckresult`, `experts_modify_position`, `experts_pending`, `experts_pending_expiration`, `experts_closeby`, `experts_ordercalcmargin`
  - Compte : https://www.mql5.com/en/book/automation/account/account_netting_hedge, `account_limits_and_restrictions`, `account_real_demo_contest`
  - Environnement : https://www.mql5.com/en/book/common/environment/env_connectivity
  - Calendrier : https://www.mql5.com/en/book/advanced/calendar
- Article sur les contrôles de niveaux de stops et de gel : https://www.mql5.com/en/articles/2555
- Strategy Tester : https://www.metatrader5.com/en/terminal/help/algotrading/testing
- Installation sous Linux via Wine : https://www.metatrader5.com/en/terminal/help/start_advanced/install_linux
- Forum et CodeBase (🟠), fils les plus utiles :
  - 369602, 375461, 472828, 515951 : heure serveur
  - 447937, 443248, 428075 : IPC timeout
  - 368425, 463767 : filling
  - 376255 : Algo Trading
  - 351590 : plusieurs terminaux
  - CodeBase 77617 : export du calendrier, correction DST
  - Préfixe : https://www.mql5.com/en/forum/<id>

### C.2 Ponts Linux (✅ métadonnées PyPI)
- https://pypi.org/project/mt5linux/ (source : https://github.com/lucas-campagna/mt5linux)
- https://pypi.org/project/pymt5linux/ (source : https://github.com/hpdeandrade/pymt5linux)
- https://pypi.org/project/rpyc/

### C.3 Axi
- Régulation :
  - https://help.axi.com/hc/en-us/articles/35968166425625-Is-Axi-licensed-or-authorized-or-regulated
  - https://help-eu.axi.com/hc/en-us/articles/38144388847001-Is-Axi-licensed-and-regulated
  - https://www.cysec.gov.cy/en-GB/entities/investment-firms/cypriot/95468/
- MT5 :
  - https://help.axi.com/hc/en-us/articles/47483936524953-Who-can-apply-for-an-MT5-account
  - https://help-eu.axi.com/hc/en-us/articles/44805728520985-Does-Axi-offer-MT5-trading-accounts
  - https://help.axi.com/hc/en-us/articles/47480882755225-Which-instruments-can-I-trade-on-MT5
  - https://www.axi.com/int/trading-platforms/metatrader-5
- Comptes :
  - https://help.axi.com/hc/en-us/articles/51348912473497-What-is-the-difference-between-Standard-and-Pro-trading-accounts
  - https://help-eu.axi.com/hc/en-us/articles/38309578404121-What-is-the-difference-between-Standard-and-Pro-trading-accounts
- Or :
  - https://help.axi.com/hc/en-us/articles/39871768779801-How-can-I-trade-gold-with-Axi
  - https://www.axi.com/int/trade/cfds/commodities/precious-metals/gold
  - https://support.axi.com/hc/en-us/articles/4403602104601-What-are-the-trading-symbols-for-Gold-Silver-Palladium-and-Platinum-
- Horaires :
  - https://help.axi.com/hc/en-us/articles/37073300884121-What-are-the-trading-hours-for-Metals-and-Commodities-CFDs
  - https://help.axi.com/hc/en-us/articles/37071313315609-What-are-the-trading-hours-for-Forex-pairs-and-CFDs
  - https://help.axi.com/hc/en-us/articles/38455728757145-Daylight-savings-in-MT4-and-market-holidays
- Swaps : https://help.axi.com/hc/en-us/articles/37070088004121-What-is-Swap-Triple-Swap-and-Swap-Rate
- Marge et stop-out :
  - https://support.axi.com/hc/en-us/articles/44105575139865-Important-Changes-to-Leverage-and-Margin-Structure
  - https://support.axi.com/hc/en-us/articles/38981462022425-What-are-margin-stop-out-levels
  - https://help-eu.axi.com/hc/en-us/articles/40490280967577-What-are-margin-stop-out-levels
- Jours fériés :
  - https://www.axi.com/int/trading-tools/holidays-calendar
  - https://help.axi.com/hc/en-us/sections/44993063043353-Trading-Calendar
- Tiers (🟠) :
  - https://www.broker-forex.fr/Axi.php
  - https://www.compareforexbrokers.com/reviews/axi-review/

### C.4 Marché de l'or
- LBMA et IBA :
  - https://www.lbma.org.uk/prices-and-data/lbma-gold-price
  - https://www.ice.com/publicdocs/futures/IBA_Gold_Auction_Specification.pdf
- CME :
  - https://www.cmegroup.com/trading/metals/files/fact-card-gold-futures-options.pdf
  - https://www.cmegroup.com/confluence/display/EPICSANDBOX/Gold
  - https://www.cmegroup.com/trading-hours.html
  - https://www.cmegroup.com/markets/metals/precious/gold.calendar.html
- SGE : https://en.sge.com.cn/eng_trading_ProductsIntroduce
- World Gold Council :
  - https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-q2-2026
  - https://www.gold.org/goldhub/research/gold-etfs-holdings-and-flows/2026/09
  - https://www.gold.org/goldhub/research/gold-market-commentary-august-2026
- Calendriers macro :
  - https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  - https://www.bls.gov/schedule/news_release/empsit.htm
  - https://www.bls.gov/schedule/news_release/cpi.htm
  - https://www.bea.gov/news/blog/2026-01-07/economic-release-schedule-updates-gdp-personal-income-and-outlays
  - https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/
- Études (🟠) :
  - https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2606587 (découverte du prix spot/futures)
  - https://onlinelibrary.wiley.com/doi/10.1002/fut.21636 (fixing)
  - https://ideas.repec.org/a/wly/jfutmk/v21y2001i3p257-278.html (saisonnalité intrajournalière, annonces)
- Heure et DST :
  - https://docs.python.org/3/library/zoneinfo.html
  - https://www.gov.uk/when-do-the-clocks-change
  - https://www.congress.gov/bill/119th-congress/house-bill/139

### C.5 Méthodologie quant
- Aronson, D. (2006). *Evidence-Based Technical Analysis*. Wiley.
- Bailey, D., Borwein, J., López de Prado, M., Zhu, Q. (2014). Pseudo-Mathematics and Financial Charlatanism. *Notices of the AMS* 61(5). https://www.ams.org/notices/201405/rnoti-p458.pdf
- Bailey, Borwein, López de Prado, Zhu. The Probability of Backtest Overfitting. *Journal of Computational Finance* (2017). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253
- Bailey, D., López de Prado, M. (2012). The Sharpe Ratio Efficient Frontier. *Journal of Risk* 15(2). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1821643
- Bailey, D., López de Prado, M. (2014). The Deflated Sharpe Ratio. *Journal of Portfolio Management* 40(5). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551
- Chan, E. (2008 ; 2e éd. 2021). *Quantitative Trading*. Wiley.
- Chan, E. (2013). *Algorithmic Trading: Winning Strategies and Their Rationale*. Wiley.
- Chan, E. (2017). *Machine Trading*. Wiley.
- Chan, E. *Backtesting and its Pitfalls* (slides). https://epchan.com/img/links/Backtesting-and-its-Pitfalls.pdf
- Davey, K. (2014). *Building Winning Algorithmic Trading Systems*. Wiley.
- Hansen, P. R. (2005). A Test for Superior Predictive Ability. *JBES* 23(4). https://www.tandfonline.com/doi/abs/10.1198/073500105000000063
- Harvey, C., Liu, Y. (2015). Backtesting. *JPM* 42(1). https://papers.ssrn.com/abstract=2345489
- Harvey, C., Liu, Y., Zhu, H. (2016). …and the Cross-Section of Expected Returns. *RFS* 29(1). https://www.nber.org/papers/w20592
- Lo, A. (2002). The Statistics of Sharpe Ratios. *FAJ* 58(4). https://rpc.cfainstitute.org/research/financial-analysts-journal/2002/the-statistics-of-sharpe-ratios
- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley. https://www.wiley.com/en-us/Advances+in+Financial+Machine+Learning-p-9781119482086
- López de Prado, M. (2018). The 10 Reasons Most Machine Learning Funds Fail. *JPM* 44(6). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3104816
- López de Prado, M. (2019). A Data Science Solution to the Multiple-Testing Crisis in Financial Research. *JFDS* 1(1). https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3177057
- Masters, T. (2018). *Testing and Tuning Market Trading Systems*. Apress.
- Pardo, R. (2008). *The Evaluation and Optimization of Trading Strategies*, 2e éd. Wiley (chap. 11, « Walk-Forward Analysis »).
- Politis, D., Romano, J. (1994). The Stationary Bootstrap. *JASA* 89(428). https://www.tandfonline.com/doi/abs/10.1080/01621459.1994.10476870
- White, H. (2000). A Reality Check for Data Snooping. *Econometrica* 68(5). https://onlinelibrary.wiley.com/doi/abs/10.1111/1468-0262.00152
- pandas `resample` (défauts `closed` et `label`) : https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.resample.html
- pandas 3.0, notes de version : https://pandas.pydata.org/docs/whatsnew/v3.0.0.html

---

## Annexe D — Constats sur le compte démo Axi (check du 29/09/2026)

Source : sortie de `scripts/check_connection.py` sur le PC de l'utilisateur, le 29/09/2026 à 02:04, heure de Paris (✅ mesuré). Aucun identifiant n'est reproduit ici.

| Élément | Hypothèse de la recherche | Constaté |
|---|---|---|
| Package / terminal | package 5.0.6231 | package 5.0.6231, terminal build 6231 du 27/09/2026 : les numéros coïncident |
| Python | 3.13, 64 bits | 3.13.15, 64 bits |
| Connexion | `initialize(path, login, password, server)` | -10005 IPC timeout répété ; OK en s'attachant d'abord sans identifiants (§1.2) |
| « Max bars in chart » sur Unlimited | 100 000 000 (🟠) | 100 000 000 ✅ |
| `ping_last` | en microsecondes (🟡/🟠) | 89 900, soit 89,9 ms, cohérent (serveur démo probablement loin de la France) |
| Heure serveur | New York + 7 h | GMT+3 mesuré, écart de 0 s avec la règle ✅ ; horloge du PC juste |
| Entité | AxiTrader Ltd, SVG (🟠) | AxiCorp Financial Services Pty Ltd |
| Levier | 1000:1 « Int » (🟡) ou 20:1 ASIC retail (⚪) | 1:1000 |
| Appel de marge / stop-out | < 100 % / 20 % « Int » (🟡) | 99 % / 20 %, en pourcentage ✅ |
| Mode de marge | inconnu | hedging ✅ |
| Devise | EUR probable | EUR ✅ |
| Symboles or | XAUUSD, .pro ? | XAUUSD (sans suffixe, compte Standard), XAUAUD, XAUEUR, XAUGBP |
| Contrat, volumes | 100 oz ; 0,01 à 20 lots (🟡) | 100 ; min 0,01, max 20, pas 0,01 ✅ |
| Prix | inconnu | 2 décimales, point = pas de prix = 0,01 |
| Stops level / freeze level | inconnus | 1 point / 0 |
| Remplissage | masque FOK = 1, IOC = 2 (🟡/⚪) | 3 = FOK + IOC ✅ |
| Exécution | Market ? | MARKET ✅ : `deviation` sans effet |
| Expiration / types d'ordres | masques MQL5 (⚪) | 15 et 127 : toutes les options, cohérent avec les valeurs des masques ✅ |
| Durée des ordres GTC | inconnue | GTC (pas de suppression quotidienne des SL/TP) |
| Triple swap | mercredi pour le FX, métaux non trouvé | **mercredi** ✅ |
| Swaps | non trouvés | mode POINTS : long −61,6, short +40,5 points par lot et par nuit |
| Spread | « à partir de 0,18 $ » (Standard) | 16 points = 0,16 $ à 03:04 heure serveur (session asiatique), flottant |
| Mode de calcul | CFD ? | FOREX (sans effet pour nous : marge et profit viennent du terminal) |
| Marge pour 0,01 lot | — | 3,63 € (or à 4 124 $) |
| 1 $ de mouvement | 1 $ par 0,01 lot | 0,88 € par 0,01 lot (EUR/USD ≈ 1,137) |

Conséquences chiffrées (✅ calcul à partir de ces valeurs) :
- **Swap sur 0,01 lot** : −0,54 € par nuit en achat, +0,36 € par nuit en vente, triplé le mercredi (−1,63 € / +1,07 €). Pour 1 lot : −54 € / +36 € par nuit. Une position acheteuse gardée plusieurs jours paie donc un coût réel, à intégrer au backtest (Phase 3).
- **Coût du spread** : 0,16 $ par once, soit 0,14 € par aller-retour sur 0,01 lot. C'est environ 0,8 % du risque d'un trade de 0,01 lot avec un SL de 20 $ (17,6 €) : faible, sauf au rollover et pendant les news, où il reste à mesurer (Phase 2).
- **Commission** : aucune attendue sur un compte Standard. À confirmer avec `--test-order`.
