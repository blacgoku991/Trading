# goldbot : bot de trading XAUUSD (MetaTrader 5, Axi)

Bot algorithmique sur l'or, connecté à MetaTrader 5 via le package officiel `MetaTrader5`.
Le plan complet, les règles non négociables et le journal d'avancement sont dans [`CLAUDE.md`](CLAUDE.md).
La recherche préalable est dans [`docs/RESEARCH.md`](docs/RESEARCH.md).

**État : Phase 1 (squelette + connexion MT5).** Le bot se connecte et vérifie tout ; il ne trade pas encore.
Seul l'ordre de test (0,01 lot, compte démo uniquement) envoie un ordre.

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
  data/                   heure serveur <-> UTC, heures de cotation
  execution/              arrondi au tick, mode de remplissage, ordre de test
  risk/guards.py          garde-fous sur le type de compte
  monitoring/             journalisation (fichiers rotatifs UTC + console)
  diagnostics.py          logique de check_connection.py
scripts/check_connection.py
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
| `pytest` (dev) | Tests |

Python **3.13** recommandé des deux côtés (Windows et Codespaces) ; 3.12 à 3.14 acceptés.

## Codespaces (Linux) : développement et tests

```bash
pip install -e ".[dev]"
pytest
```

`import MetaTrader5` est impossible sous Linux : les tests utilisent `FakeBroker`.

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
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

- Si `Activate.ps1` est bloqué : `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, puis relance l'activation.
- Si le dépôt est privé, Git ouvre une fenêtre de connexion GitHub.

### 3. Régler le terminal MT5 d'Axi

1. Ouvre le terminal MT5 d'Axi et connecte-toi à ton compte **démo** (Fichier > Connexion à un compte de trading).
2. Outils > Options > onglet **Expert Advisors** :
   - coche « Autoriser le trading algorithmique » ;
   - si elle existe, **décoche** l'option « Disable algorithmic trading via external Python API ».
3. Dans la barre d'outils, le bouton **Algo Trading** doit être activé (vert).
4. Outils > Options > onglet **Graphiques** : « Max bars in chart » sur **Unlimited**, puis redémarre le terminal.

### 4. Renseigner les secrets

```powershell
Copy-Item .env.example .env
notepad .env
```

- `MT5_LOGIN` : numéro du compte.
- `MT5_PASSWORD` : mot de passe du compte. S'il contient un espace ou un `#`, entoure-le de guillemets **simples**.
- `MT5_SERVER` : nom **exact** du serveur, tel qu'affiché dans MT5.
- `MT5_PATH` : chemin complet de `terminal64.exe`, **sans guillemets**. Pour le trouver : clic droit sur le raccourci
  MT5 > « Ouvrir l'emplacement du fichier ». Tu peux le laisser vide au premier essai : le script affichera le
  chemin du terminal trouvé.

Ne colle jamais ces valeurs dans un chat.

### 5. Lancer la vérification (terminal MT5 ouvert)

```powershell
python scripts/check_connection.py
```

Le script affiche l'état du terminal, du compte, du symbole de l'or, de l'heure serveur et du flux de prix.
Il enregistre aussi un rapport dans `reports\`. Login, serveur et nom sont masqués : tu peux recoller la sortie
telle quelle.

### 6. Ordre de test (compte démo, pendant les heures de cotation)

```powershell
python scripts/check_connection.py --test-order
```

Déroulé :
1. achète 0,01 lot (1 once) avec SL et TP côté serveur, à 5 USD du prix ;
2. resserre le SL ;
3. ferme la position ;
4. affiche la commission réelle par lot et le slippage.

Refusé sur un compte réel, le week-end et pendant la pause quotidienne (de 23:59 à 01:01 heure serveur).

### 7. (Optionnel) Tests sous Windows

```powershell
pytest
```

Sous Windows, un test supplémentaire compare les constantes du bot à celles du vrai package `MetaTrader5`.

### Codes de sortie de `check_connection.py`

| Code | Signification |
|---|---|
| 0 | Tout est bon |
| 1 | Au moins un problème détecté (voir « Bilan ») |
| 2 | Connexion au terminal impossible |
| 3 | Ordre de test refusé (compte non démo, marché fermé, erreurs à corriger) |
| 4 | Configuration ou `.env` invalide |
