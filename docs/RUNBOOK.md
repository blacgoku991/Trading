# RUNBOOK — faire tourner le bot 24 h/24 (compte démo)

Phase 6 du `CLAUDE.md`. Deux cas : **VPS Ubuntu** (partie 1) ou **VPS Windows** (partie 2, le plus fiable).

## 0. À savoir avant de commencer

- Le package `MetaTrader5` n'existe que pour Windows, avec le terminal MT5 sur la même machine.
  - Sur un VPS Windows, tout tourne normalement.
  - Sur un VPS Ubuntu, le terminal MT5 **et** le Python du bot tournent sous **Wine**, qui fait tourner des programmes
    Windows sous Linux. C'est la méthode officielle de MetaQuotes pour MT5 sous Linux. Côté Python, elle a été testée
    le 29/09/2026 : Wine 9.0, Python Windows 3.12.10, les 455 tests du dépôt passent, ainsi que le gardien avec de
    vrais processus. Le terminal MT5 sous Wine n'a pas pu être testé (téléchargements MetaQuotes bloqués dans
    l'environnement de développement) : la commande `verifier` le contrôle sur ton VPS.
  - Pour un compte réel plus tard, préférer un VPS Windows (`CLAUDE.md` §4 : Wine est déconseillé pour le réel).
- Le « VPS » intégré à MT5 (Virtual Hosting) et le « Sponsored VPS » proposé par les brokers, Axi compris, ne font
  tourner que des robots MQL5, **pas de Python** : inutiles ici.
- **Un seul bot par compte.** Arrête celui du PC (Ctrl+C : il ferme ses positions) avant de lancer celui du VPS, et
  vérifie dans MT5 qu'il ne reste aucune position du bot. Le verrou « déjà en cours » ne protège qu'une machine ; deux
  bots sur le même compte fermeraient leurs positions l'un l'autre (positions inconnues de leur état).
- **Le gardien** (`scripts/run_forever.py`) garde le bot en marche.
  - Il relance le bot après un plantage (code 1) ou une connexion impossible au terminal (code 2). L'attente est
    de 15 s, puis double à chaque arrêt rapproché, jusqu'à 5 minutes.
  - Il **ne relance jamais** après :
    - un arrêt voulu (Ctrl+C, code 0) ;
    - un refus (code 3) : arrêt total à −10 %, réglages modifiés, Algo Trading coupé, compte non démo ;
    - une configuration invalide (code 4) ;
    - un bot déjà en cours (code 5).
  - Il ne passe jamais `--nouvelle-experience` de lui-même : la reprise après l'arrêt total reste ta décision.
  - Journal : `logs/gardien_scalp.log`.
- **Taille du VPS** : 2 vCPU, 4 Go de mémoire (2 Go au minimum sous Ubuntu), 30 Go de disque.
- **Emplacement** : je n'ai pas trouvé où sont les serveurs MT5 d'Axi. Londres est un choix raisonnable. Après
  installation, la vérification affiche le ping vers le serveur : moins de 20 ms est très bien.
- **Heures sans marché** (pour les mises à jour et redémarrages) : du vendredi vers 22 h-23 h au lundi vers 0 h,
  heure de Paris (heure serveur : vendredi 23:58 → lundi 01:00).

## 1. VPS Ubuntu (22.04 ou 24.04)

### 1.1 Installation (une fois, environ 15 minutes)

Sur ton PC, ouvre PowerShell et connecte-toi au VPS (adresse IP et utilisateur donnés par l'hébergeur) :

```powershell
ssh root@<IP du VPS>
```

Puis, sur le VPS :

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/blacgoku991/Trading.git
cd Trading
git checkout claude/new-session-863awc
sudo bash scripts/linux/install_ubuntu.sh
```

Le script installe :
- Wine ;
- un écran virtuel (Xvfb), un petit gestionnaire de fenêtres et VNC ;
- le Python Windows officiel (paquet NuGet « python ») et les dépendances du bot, en versions figées testées
  (`scripts/linux/requirements-wine.txt`) ;
- le terminal MetaTrader 5 (installation silencieuse) ;
- le service `goldbot`.

À la fin, il affiche la ligne `MT5_PATH=...` à mettre dans `.env`.

### 1.2 Secrets (`.env`)

Depuis ton PC (PowerShell, pas sur le VPS), copie ton `.env` :

```powershell
scp $HOME\Trading\.env root@<IP du VPS>:Trading/.env
```

Puis, sur le VPS, remplace la ligne `MT5_PATH` par celle affichée à la fin de l'installation
(`nano ~/Trading/.env`, Ctrl+O pour enregistrer, Ctrl+X pour quitter). Ne colle jamais ces valeurs dans un chat.

### 1.3 Régler le terminal MT5 (une fois, en voyant l'écran du VPS)

1. Sur le VPS : `bash scripts/linux/goldbot.sh mt5` (lance le terminal sur l'écran virtuel).
2. Sur ton PC, installe un visualiseur VNC gratuit : TigerVNC Viewer ou RealVNC Viewer.
3. Sur ton PC, dans un nouveau PowerShell, ouvre le tunnel et laisse cette fenêtre ouverte :
   `ssh -L 5900:localhost:5900 root@<IP du VPS>`.
   VNC n'écoute que sur le VPS lui-même ; le tunnel SSH le protège.
4. Dans le visualiseur VNC, connecte-toi à `localhost:5900` : la fenêtre de MT5 apparaît.
5. Dans MT5, connecte-toi à ton compte **démo** : Fichier > Se connecter à un compte de trading, avec le même
   numéro, mot de passe et nom de serveur que sur ton PC. Tape le nom du serveur s'il n'est pas dans la liste.
6. Fais les réglages du README, section Windows, étape 3 :
   - Outils > Options > Expert Advisors : trading algorithmique autorisé, API Python non désactivée ;
   - bouton **Algo Trading** vert.
7. Ferme le visualiseur et le tunnel. Le terminal continue de tourner.

### 1.4 Vérifier

```bash
bash scripts/linux/goldbot.sh verifier   # connexion, compte démo, symbole, ping : « 0 erreur » attendu
bash scripts/linux/goldbot.sh test       # facultatif : « 455 passed » attendu
```

### 1.5 Lancer 24 h/24

```bash
sudo systemctl enable --now goldbot
```

Le bot démarre maintenant, puis à chaque redémarrage du VPS. Au premier lancement sur le VPS, une nouvelle
expérience commence (celle du PC reste sur le PC).

| Pour… | Commande |
|---|---|
| voir le bot en direct | `journalctl -u goldbot -f` (Ctrl+C arrête l'affichage, pas le bot) |
| l'état du service | `systemctl status goldbot` |
| les bilans | `bash scripts/linux/goldbot.sh bilan` |
| le journal du gardien | `tail -f logs/gardien_scalp.log` |
| voir les positions sur ton téléphone | appli MetaTrader 5, connectée au même compte démo |

### 1.6 Arrêter, mettre à jour, reprendre après l'arrêt total

- **Arrêter** : `sudo systemctl stop goldbot`. Sous Wine, le bot n'a peut-être pas le temps de fermer ses positions :
  leur stop reste sur le serveur, et le bot les reprend au démarrage suivant.
- **Mettre à jour le bot** :

  ```bash
  sudo systemctl stop goldbot
  cd ~/Trading && git pull
  sudo bash scripts/linux/install_ubuntu.sh           # seulement si requirements-wine.txt a changé
  bash scripts/linux/goldbot.sh nouvelle-experience   # seulement si les réglages ont changé
  sudo systemctl start goldbot
  ```

  Si les réglages ont changé et que tu oublies `nouvelle-experience`, le bot refuse de démarrer (code 3) et
  `systemctl status goldbot` le montre.
- **Arrêt total à −10 %** : le service s'arrête et ne repart pas tout seul. Lis les bilans. Si tu décides de repartir :
  `bash scripts/linux/goldbot.sh nouvelle-experience && sudo systemctl restart goldbot`.

### 1.7 Si ça ne marche pas

| Symptôme | Que faire |
|---|---|
| `verifier` : connexion au terminal impossible | vérifie `MT5_PATH` dans `.env` ; relance `goldbot.sh mt5` et regarde l'écran par VNC |
| refus « Algo Trading désactivé » | VNC : clique sur Algo Trading (vert), puis `sudo systemctl restart goldbot` |
| le service redémarre en boucle | `journalctl -u goldbot -n 100` ; systemd attend 60 s entre deux essais |
| MT5 ne s'installe pas | relance `sudo bash scripts/linux/install_ubuntu.sh` ; avec l'installateur d'Axi : `sudo MT5_INSTALLER_URL=<lien> bash scripts/linux/install_ubuntu.sh` |
| rien ne va | passe sur un VPS Windows (partie 2) : beaucoup d'hébergeurs proposent Windows en option |

## 2. VPS Windows (le plus fiable)

### 2.1 Installer

1. VPS **Windows Server 2022** (ou 2019, ou Windows 10/11) avec accès « Bureau à distance ».
2. Sur ton PC, connecte-toi au VPS avec « Connexion Bureau à distance » (`mstsc`), avec l'adresse, l'utilisateur et
   le mot de passe donnés par l'hébergeur.
3. Sur le VPS, suis le README, section Windows, étapes 1 à 5 :
   - Python 3.13 et Git (`winget` manque souvent sur Windows Server : sinon, installateurs de python.org et de
     git-scm.com) ;
   - `git clone`, puis l'environnement du bot ;
   - terminal MT5 d'Axi (depuis ton espace client Axi), connecté au compte démo, réglé comme à l'étape 3 ;
   - `.env` (copie celui du PC) ;
   - `check_connection.py` : « 0 erreur », et regarde la ligne du ping.

### 2.2 Démarrage automatique

Dans PowerShell, depuis le dossier du bot :

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
Start-ScheduledTask -TaskName goldbot-scalp
```

- La tâche lance le gardien une minute après chaque ouverture de session, dans une fenêtre visible. Tu vois le bot
  chaque fois que tu te connectes.
- Pour que la session s'ouvre toute seule après un redémarrage du VPS (mises à jour, maintenance de l'hébergeur) :
  1. installe **Autologon** de Microsoft Sysinternals (https://learn.microsoft.com/sysinternals/downloads/autologon) ;
  2. lance `Autologon64.exe`, entre ton utilisateur et ton mot de passe, puis clique sur Enable. Le mot de passe est
     stocké chiffré ;
  3. teste : Démarrer > Redémarrer, attends 3 minutes, reconnecte-toi : la fenêtre du bot doit être là.
- Retirer la tâche : `... install_autostart.ps1 -Remove`.
- En cas de refus « Accès refusé », ouvre PowerShell en administrateur.

### 2.3 Quitter le VPS sans arrêter le bot

- Ferme la fenêtre du Bureau à distance avec la **croix**.
- **Ne clique jamais sur « Se déconnecter »** (Sign out) : cela ferme la session, donc MT5 et le bot.
- Si le bot s'arrête quand tu fermes la fenêtre, une limite de temps s'applique aux sessions déconnectées. Dans
  `gpedit.msc` : Configuration ordinateur > Modèles d'administration > Composants Windows > Services Bureau à distance
  > Hôte de la session Bureau à distance > Limites de temps de session > « Définir une limite de temps pour les
  sessions déconnectées » : Désactivé.

### 2.4 Mises à jour de Windows

Pour qu'elles ne redémarrent pas le VPS en pleine séance :
- **Windows Server** : `sconfig`, option 5 (paramètres de Windows Update), puis Manuel. Installe les mises à jour le
  samedi (option 6), puis redémarre.
- **Windows 10/11** : Paramètres > Windows Update > Suspendre, et « Heures d'activité ».

### 2.5 Mettre à jour le bot

1. Dans la fenêtre du bot : Ctrl+C. Le bot ferme ses positions et le gardien s'arrête.
2. Puis :

   ```powershell
   git pull
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   .\.venv\Scripts\python.exe scripts\run_forever.py --nouvelle-experience   # si les réglages ont changé
   Start-ScheduledTask -TaskName goldbot-scalp                               # sinon
   ```

- `--nouvelle-experience` ne vaut que pour ce lancement-là : les relances automatiques ne l'utilisent jamais.
- Arrêt total à −10 % : même commande avec `--nouvelle-experience`, après avoir lu les bilans.

## 3. Incidents (les deux cas)

| Incident | Ce qui se passe | Ce que tu fais |
|---|---|---|
| coupure internet ou du terminal | le bot se reconnecte ; s'il s'arrête (code 2), le gardien le relance | rien |
| plantage du bot | le gardien le relance (15 s, puis jusqu'à 5 min) | regarder `logs/` s'il se répète |
| redémarrage du VPS | Ubuntu : le service repart ; Windows : Autologon + tâche | rien |
| perte du jour à −2 % | plus d'entrée jusqu'au lendemain, reprise automatique | rien |
| arrêt total à −10 % | positions fermées, bot arrêté, pas de relance | lire les bilans, puis décider (`nouvelle-experience`) |
| stop manquant sur une position | le bot le repose, sinon ferme la position | rien |
| position avec le magic du bot mais inconnue de son état | le bot la ferme ; tes trades manuels (autre magic) ne sont jamais touchés | ne jamais lancer deux bots sur le même compte : ils fermeraient leurs positions l'un l'autre |
| Algo Trading coupé | refus au démarrage (code 3), pas de relance | le réactiver, puis relancer |

## 4. Passer de la démo au réel

Décision humaine uniquement (`CLAUDE.md` §14) : au moins 4 à 8 semaines de démo et 50 trades, avec des résultats dans
l'intervalle attendu du backtest ; puis risque réduit.

- L'expérience de scalping refuse tout compte réel, sans option.
- Le bot principal exige `LIVE_TRADING=true` dans `.env` **et** l'option `--i-understand-real-money`. Le gardien ne
  la passe jamais : un compte réel ne peut pas être lancé par le démarrage automatique.
