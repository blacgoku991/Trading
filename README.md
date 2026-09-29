# goldbot : bot de trading XAUUSD (MetaTrader 5, Axi)

Bot algorithmique sur l'or, connecté à MetaTrader 5 via le package officiel `MetaTrader5`.
Le plan complet, les règles non négociables et le journal d'avancement sont dans [`CLAUDE.md`](CLAUDE.md).
La recherche préalable est dans [`docs/RESEARCH.md`](docs/RESEARCH.md).

**État : Phase 5 (démo).** Le bot est prêt à trader seul sur le compte démo la stratégie validée par backtest
(`docs/STRATEGIES.md`). Le passage en réel reste une décision humaine, après plusieurs semaines de démo.
À part : une **expérience de scalping** (bougies de 5 s, deux stratégies versionnées), compte démo uniquement,
pour observer de vraies exécutions (section 9). Elle perd sur le rejeu des ticks Axi : ce n'est pas une stratégie
rentable.

## Principes de sécurité

- **Démo d'abord.** L'ordre de test refuse tout compte qui n'est pas un compte démo. Plus tard, le bot refusera
  un compte réel sauf si `LIVE_TRADING=true` dans `.env` **et** l'argument `--i-understand-real-money`.
- **Stop loss côté serveur** dans chaque requête d'ouverture. Le SL n'est jamais éloigné.
- **Magic number** : le bot ne touche jamais tes trades manuels (en compte *hedging*).
- **Secrets** uniquement dans `.env` (jamais commité). Login, serveur et mot de passe sont masqués dans
  l'affichage, les rapports et les journaux.

## Structure

```
config/settings.yaml      configuration validée (aucun secret)
src/goldbot/
  config.py               chargement et validation (pydantic), secrets depuis .env
  broker/                 interface commune, MT5Broker (Windows), FakeBroker (tests),
                          constantes MT5, reconnexion, détection du symbole, vérifications
  data/                   heure serveur <-> UTC, heures de cotation, export et relecture de
                          l'historique (history.py), contrôle qualité (quality.py)
  execution/              arrondi au tick, mode de remplissage, ordre de test
  risk/guards.py          garde-fous sur le type de compte
  monitoring/             journalisation (fichiers rotatifs UTC + console)
  indicators/, strategies/, backtest/   stratégies et moteur de backtest (docs/STRATEGIES.md)
  live/                   bot principal (run_live.py)
  scalping/               expérience de scalping : moteur 5 s, rejeu des ticks, bot démo, état SQLite
  diagnostics.py          logique de check_connection.py
  export.py               logique de export_history.py
  data_report.py          logique de data_report.py
scripts/check_connection.py
scripts/export_history.py  export de l'historique (Windows)
scripts/data_report.py     contrôle qualité des données exportées (partout)
scripts/run_backtest.py    backtest des stratégies (partout)
scripts/run_live.py        bot principal (Windows)
scripts/run_scalp.py       expérience de scalping sur compte démo (Windows)
scripts/backtest_scalp.py  rejeu de l'expérience sur les ticks exportés (partout)
tests/                    tests unitaires (sans MT5)
docs/RESEARCH.md
```

## Dépendances (règle 10 du CLAUDE.md)

| Paquet | Pourquoi |
|---|---|
| `MetaTrader5==5.0.6231` | Connecteur officiel MetaQuotes. Installé **sous Windows uniquement** (seules des wheels Windows existent) |
| `pydantic` | Validation de la configuration |
| `PyYAML` | Lecture de `config/settings.yaml` |
| `python-dotenv` | Lecture des secrets depuis `.env` |
| `tzdata` | Base des fuseaux horaires pour `zoneinfo` (absente sous Windows) |
| `numpy` | Calcul vectorisé sur les barres et les ticks (tableaux renvoyés par MT5) |
| `pandas` | Séries temporelles de l'historique, conversion heure serveur -> UTC |
| `pyarrow` | Fichiers parquet compressés (historique) |
| `matplotlib` | Courbe d'equity des backtests (PNG) |
| `pytest` (dev) | Tests |

Python **3.13** recommandé des deux côtés (Windows et Codespaces) ; 3.12 à 3.14 acceptés.

## Codespaces (Linux) : développement et tests

```bash
pip install -e ".[dev]"
pytest
```

`import MetaTrader5` est impossible sous Linux : les tests utilisent `FakeBroker`.

Contrôle qualité des données exportées depuis Windows (fichiers copiés dans `data/export/`) :

```bash
python scripts/data_report.py
```

Le rapport (trous, doublons, pics, spreads, validation de l'heure serveur) est écrit dans `reports/`.

Backtest d'une stratégie sur ces données (la dernière année est réservée à la validation finale) :

```bash
python scripts/run_backtest.py                  # S1 sur la période d'étude
python scripts/run_backtest.py --spread-x 1.5   # stress des coûts
```

Rapport, courbe d'equity (PNG) et journal de tous les essais (`essais.csv`) dans `reports/`.
Les stratégies et leurs résultats sont décrits dans [`docs/STRATEGIES.md`](docs/STRATEGIES.md).

## Windows : connexion au compte Axi

Toutes les commandes sont à taper dans **PowerShell**.

### 1. Installer Python 3.13 (64 bits) et Git

```powershell
winget install -e --id Python.Python.3.13
winget install -e --id Git.Git
```

Ferme puis rouvre PowerShell, puis vérifie : `py -3.13 --version`.

### 2. Récupérer le code et installer le bot

```powershell
cd $HOME
git clone https://github.com/blacgoku991/Trading.git
cd Trading
git checkout claude/new-session-863awc
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

- Toutes les commandes appellent directement le Python de l'environnement virtuel (`.venv\Scripts\python.exe`).
  Pas besoin de `Activate.ps1`, que Windows bloque par défaut (« l'exécution de scripts est désactivée »).
  Si tu préfères l'activer : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, puis `.\.venv\Scripts\Activate.ps1`.
- Si le dépôt est privé, Git ouvre une fenêtre de connexion GitHub.

### 3. Régler le terminal MT5 d'Axi

1. Ouvre le terminal MT5 d'Axi et connecte-toi à ton compte **démo** (Fichier > Connexion à un compte de trading).
2. Outils > Options > onglet **Expert Consultants** (« Expert Advisors » en anglais) :
   - **coché** : « Autoriser le trading algorithmique » ;
   - **décoché** : « Désactiver le trading algorithmique via les API Python externes » ;
   - **décoché** : « Désactiver le trading algorithmique lors du changement de compte ». Le script se connecte
     lui-même au compte à chaque lancement : cochée, cette option pourrait couper Algo Trading à chaque fois.
     Le bot vérifie de son côté le type de compte (démo) à chaque démarrage ;
   - laisse décochés « Autoriser les importations DLL » et « Autoriser WebRequest » (inutiles ici).
3. Dans la barre d'outils, le bouton **Trading Algo** doit être vert (icône ▷), pas rouge.
4. Outils > Options > onglet **Graphiques** : « Max bars in chart » (nombre maximal de barres dans le graphique)
   sur **Unlimited**, puis redémarre le terminal.

### 4. Renseigner les secrets

```powershell
Copy-Item .env.example .env
notepad .env
```

- `MT5_LOGIN` : numéro du compte.
- `MT5_PASSWORD` : mot de passe du compte. S'il contient un espace ou un `#`, entoure-le de guillemets **simples**.
- `MT5_SERVER` : nom **exact** du serveur, tel qu'affiché dans MT5.
- `MT5_PATH` : chemin complet de `terminal64.exe` du terminal Axi, **sans guillemets**. Pour l'obtenir, MT5 ouvert,
  tape dans PowerShell : `(Get-Process terminal64).Path`. Vide, le module cherche un terminal lui-même, et peut
  échouer avec l'erreur -10005 (IPC timeout) s'il ne trouve pas celui qui est ouvert.

Ne colle jamais ces valeurs dans un chat.

### 5. Lancer la vérification (terminal MT5 ouvert)

```powershell
.\.venv\Scripts\python.exe scripts\check_connection.py
```

Le script affiche l'état du terminal, du compte, du symbole de l'or, de l'heure serveur et du flux de prix.
Il enregistre aussi un rapport dans `reports\`. Login, serveur et nom sont masqués : tu peux recoller la sortie
telle quelle.

### 6. Ordre de test (compte démo, pendant les heures de cotation)

```powershell
.\.venv\Scripts\python.exe scripts\check_connection.py --test-order
```

Déroulé :
1. achète 0,01 lot (1 once) avec SL et TP côté serveur, à 5 USD du prix ;
2. resserre le SL ;
3. ferme la position ;
4. affiche la commission réelle par lot et le slippage.

Refusé sur un compte réel, le week-end et pendant la pause quotidienne (de 23:59 à 01:01 heure serveur).

### 7. Exporter l'historique de l'or (terminal MT5 ouvert)

```powershell
cd $HOME\Trading
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\export_history.py
```

- Durée : quelques minutes. Le terminal télécharge l'historique chez Axi : barres d'une minute sur toutes les années
  disponibles, et ticks des 30 derniers jours (réglable dans `config/settings.yaml`, section `export`).
- Les fichiers arrivent dans `data\export` : `explorer data\export` ouvre le dossier.
- `manifest.json` décrit l'export (symbole, fichiers, empreintes SHA-256) sans aucun secret.
- Si l'historique est court : ouvre un graphique de l'or en M1, clique dedans, appuie sur la touche Début (Home),
  attends une minute et relance l'export.

### 8. Lancer le bot (compte démo)

Le bot trade la stratégie validée (`docs/STRATEGIES.md`) : momentum intraday à Londres et à New York.
Il lui faut le terminal MT5 ouvert, connecté, et le bouton **Trading Algo** vert.

D'abord en simulation (aucun ordre envoyé, il affiche ce qu'il aurait fait) :

```powershell
cd $HOME\Trading
git pull
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe scripts\run_live.py --simulation
```

Puis pour de vrai, sur le compte démo :

```powershell
.\.venv\Scripts\python.exe scripts\run_live.py
```

- Laisse la fenêtre PowerShell ouverte : le bot vérifie le marché toutes les 2 secondes et décide à chaque minute.
  Toutes les 30 minutes, il affiche une ligne « toujours en marche ».
- Horaires des signaux (heure de Paris) : Londres vers 11:00, New York vers 15:30 ; sorties à 17:00 et 22:00.
  Le PC et MT5 doivent donc rester allumés en semaine, environ de 10:30 à 22:30.
- Ctrl+C arrête le bot. Les positions ouvertes gardent leur stop loss côté serveur ; au redémarrage, le bot les
  reprend (sortie horaire comprise) grâce à son état enregistré dans `data\live.sqlite`.
- Garde-fous : compte démo uniquement (un compte réel exige `LIVE_TRADING=true` dans `.env` **et**
  `--i-understand-real-money`), compte en hedging exigé, un seul bot à la fois, jamais de contact avec tes trades
  manuels (magic number). Il respecte aussi les limites de `config/settings.yaml` (0,5 % par trade, −2 % par jour,
  arrêt total à −10 %).
- Journal détaillé : `logs\goldbot.log`.

### 9. Expérience de scalping (compte démo uniquement)

Version expérimentale, **séparée** de la stratégie Londres / New York, sur des bougies de 5 s construites à partir
des ticks, toutes sessions ouvertes. Deux stratégies tournent ensemble, chacune identifiée par sa version et
l'empreinte de ses réglages (par exemple `cassure v1 · a496a8fc`) :
- **cassure** : cassure du plus haut ou du plus bas des 60 s précédentes, confirmée par deux clôtures, dans le sens
  de la tendance M1 (EMA20 / EMA50) ;
- **impulsion-repli** : impulsion d'au moins 1 ATR M1, repli de 30 à 70 %, puis reprise dans le sens de
  l'impulsion (principe public de GOLD Scalper PRO réécrit en règles, pas son code).

Pour les deux : stop côté serveur derrière la structure du signal, objectif à 1,2 fois le stop, sortie forcée au
bout de 120 s même en perte. Règles détaillées et résultats : `docs/STRATEGIES.md`, « Expérience de scalping ».

**À savoir avant de lancer** : rejouées sur les 4 semaines de ticks Axi, **les deux perdent** (profit factor 0,73
pour la cassure, 0,69 pour l'impulsion-repli ; aucune journée positive sur 21). L'expérience sert à observer de
vraies ouvertures et clôtures, pas à gagner. Quelques trades gagnants ne prouveront pas qu'elle est rentable.

1. Vérification technique, **pendant les heures de cotation** (terminal MT5 ouvert, Algo Trading vert) :

   ```powershell
   cd $HOME\Trading
   git pull
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   .\.venv\Scripts\python.exe scripts\run_scalp.py --verification
   ```

   Le bot ouvre un achat de test au lot minimal (stop à 3 $), vérifie le stop côté serveur, le resserre de
   0,50 $, simule un redémarrage (reprise depuis l'état), ferme la position au bout de 15 s et compare le
   résultat estimé avant clôture au résultat exécuté. Il affiche six lignes `OK` ou `ÉCHEC`.
   Coût : le spread (environ 0,15 € pour 0,01 lot), plus ou moins le mouvement de l'or pendant ces 15 s ;
   perte maximale environ 2,5 $ (stop resserré). Ce trade de test est exclu des bilans.

2. Collecte des résultats, **sans modifier les réglages** pendant les 10 jours de cotation annoncés
   (`experiment_days`) :

   ```powershell
   .\.venv\Scripts\python.exe scripts\run_scalp.py
   ```

   En direct : chaque signal avec sa stratégie et sa version, le motif d'entrée ou de refus, le spread, le lot,
   le stop, l'objectif, la durée maximale ; à chaque sortie, le motif, la durée réelle, le résultat estimé et
   exécuté, les frais, puis le **résultat de l'essai : réalisé, latent (positions ouvertes) et total**. Une ligne
   « en marche » toutes les 5 minutes (tendance, ATR, spread, compteurs, résultat total). Deux bilans toutes les
   15 minutes et à l'arrêt, par stratégie et au total :
   - **Bilan 1 : exécutions démo** : gains et pertes réalisés, frais (commission, swap ; le spread payé est
     indiqué à part, il est déjà dans les prix), positions ouvertes et résultat latent, résultat total, durée des
     trades, motifs de sortie, doublons évités, trades fractionnés, valeur du compte ;
   - **Bilan 2 : simulation** des mêmes signaux avec 10 points de glissement en plus, mêmes rubriques.

   Ctrl+C arrête le bot : il ferme d'abord les positions de l'expérience, puis affiche les deux bilans. Si la
   fermeture échoue (marché fermé, connexion perdue), leur stop reste sur le serveur et le bot les reprend au
   redémarrage. Si les réglages de la section `scalping` changent en cours de collecte, le bot refuse de
   repartir ; pour démarrer une nouvelle expérience (l'ancienne est archivée) : `run_scalp.py --nouvelle-experience`.

3. Bilans à tout moment : `.\.venv\Scripts\python.exe scripts\run_scalp.py --bilan`.
   Sans aucun ordre (tout simulé localement) : `run_scalp.py --simulation`.

Plusieurs entrées par minute : jusqu'à 5 nouvelles entrées sur 60 s glissantes quand des occasions **distinctes**
apparaissent (au plus une par bougie de 5 s), 5 trades ouverts et 0,5 % de risque cumulé au plus. Le bot distingue :
- un **nouveau signal** : une occasion différente (autre niveau cassé, autre impulsion) ; la même occasion n'est
  jamais prise deux fois tant que son trade est ouvert ;
- un **fractionnement** : un trade trop gros pour un seul ordre part en plusieurs ordres `#1`, `#2`… qui partagent
  un seul budget de risque et comptent pour une seule entrée ;
- un **doublon accidentel** : le même signal revu (relance, réponse perdue du broker) n'est jamais renvoyé, et une
  position en double chez le broker est fermée ; ces cas sont comptés dans le bilan.

Garde-fous : compte **démo obligatoire, sans exception** (aucune option ne permet le réel), compte en hedging,
Algo Trading actif, un seul exemplaire à la fois ; 0,1 % de risque par trade (signal ignoré si le lot minimal
dépasse ce budget) ; plus d'entrée pour la journée à −1 % (démo, latent compris) ; marge libre d'au moins 50 % de
l'equity après l'ordre ; pas d'entrée si le marché ferme avant la fin des 120 s ; stop manquant reposé, sinon
position fermée ; si le serveur répond « trop de requêtes », plus d'envoi pendant 60 s. Magic `20260929` : le bot
principal (`20260928`) et tes trades manuels ne sont jamais touchés. D'après le centre d'aide d'Axi, le scalping
est permis sur les comptes Standard et le trading haute fréquence ne l'est pas (sans seuil chiffré) : avant tout
usage en réel, demander à Axi par écrit la fréquence acceptée.

Rejeu et comparaison des deux stratégies sur les ticks exportés (Codespaces ou Windows) :
`python scripts/backtest_scalp.py` (options : `--strategie B`, `P` ou `ensemble`).

### 10. (Optionnel) Tests sous Windows

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Sous Windows, un test supplémentaire compare les constantes du bot à celles du vrai package `MetaTrader5`.

### Codes de sortie

`check_connection.py` :

| Code | Signification |
|---|---|
| 0 | Tout est bon |
| 1 | Au moins un problème détecté (voir « Bilan ») |
| 2 | Connexion au terminal impossible |
| 3 | Ordre de test refusé (compte non démo, marché fermé, erreurs à corriger) |
| 4 | Configuration ou `.env` invalide |

`export_history.py` : 0 export complet, 1 export incomplet (tranches en échec ou aucune barre), 2 connexion
impossible, 4 configuration invalide, 130 interrompu (Ctrl+C).

`run_scalp.py` : 0 OK, 1 vérification en échec, 2 connexion impossible, 3 refus (compte non démo, netting,
Algo Trading coupé, réglages modifiés en cours d'expérience), 4 configuration invalide, 5 déjà en cours.

`run_live.py` : 0 arrêt normal, 2 connexion impossible, 3 refus (compte réel, netting, Algo Trading désactivé),
4 configuration invalide, 5 un autre bot tourne déjà.
