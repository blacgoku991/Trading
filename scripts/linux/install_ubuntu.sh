#!/usr/bin/env bash
# Installation du bot sur un VPS Ubuntu (22.04 ou 24.04) : Wine, Python Windows, dépendances du bot, terminal MT5
# et service systemd « goldbot ». À lancer une fois depuis le dossier du dépôt : sudo bash scripts/linux/install_ubuntu.sh
#
# Le package MetaTrader5 n'existe que pour Windows : le terminal MT5 ET le Python du bot tournent sous Wine, dans le
# même préfixe (~/.wine-goldbot). Méthode moins solide qu'un VPS Windows (docs/RUNBOOK.md) ; suffisante pour la démo.
# Variables facultatives : MT5_INSTALLER_URL (installateur MT5 de ton broker), GOLDBOT_PIP_ARGS (options pip).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Lance avec sudo : sudo bash scripts/linux/install_ubuntu.sh"
    exit 1
fi
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_USER="${SUDO_USER:-root}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
GB_HOME="$RUN_HOME/.goldbot"
PREFIX="$RUN_HOME/.wine-goldbot"
PYTHON_VERSION="3.12.10"  # testé avec requirements-wine.txt (voir ce fichier)
MT5_URL="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
. /etc/os-release
CODENAME="${VERSION_CODENAME:-noble}"

as_user() { sudo -u "$RUN_USER" -H env WINEPREFIX="$PREFIX" WINEDEBUG=-all "$@"; }
step() { printf '\n=== %s ===\n' "$*"; }

step "1/6 Paquets Ubuntu (écran virtuel, VNC, outils)"
dpkg --add-architecture i386
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ca-certificates curl unzip git xvfb openbox x11vnc

step "2/6 Wine"
if ! command -v wine >/dev/null 2>&1; then
    install -d -m 755 /etc/apt/keyrings
    if curl -fsSL -o /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key &&
        curl -fsSL -o "/etc/apt/sources.list.d/winehq-$CODENAME.sources" \
            "https://dl.winehq.org/wine-builds/ubuntu/dists/$CODENAME/winehq-$CODENAME.sources" &&
        apt-get update -qq &&
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --install-recommends winehq-stable; then
        echo "WineHQ stable installé."
    else
        echo "WineHQ indisponible : Wine d'Ubuntu."
        rm -f "/etc/apt/sources.list.d/winehq-$CODENAME.sources"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wine64 wine32:i386 ||
            DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wine64
    fi
fi
wine --version

step "3/6 Python Windows $PYTHON_VERSION (paquet NuGet officiel « python »)"
as_user mkdir -p "$GB_HOME"
if [ ! -f "$GB_HOME/python/tools/python.exe" ]; then
    as_user curl -fsSL -o "$GB_HOME/python.nupkg" \
        "https://api.nuget.org/v3-flatcontainer/python/$PYTHON_VERSION/python.$PYTHON_VERSION.nupkg"
    as_user unzip -q -o "$GB_HOME/python.nupkg" 'tools/*' -d "$GB_HOME/python"
    rm -f "$GB_HOME/python.nupkg"
fi

step "4/6 Préfixe Wine et dépendances du bot"
# mscoree / mshtml vides : pas de fenêtre d'installation de Mono et Gecko (inutiles au bot).
as_user WINEDLLOVERRIDES="mscoree=;mshtml=" wineboot --init >/dev/null 2>&1 || true
REQUIREMENTS="$(as_user winepath -w "$REPO/scripts/linux/requirements-wine.txt")"
# shellcheck disable=SC2086
as_user wine "$GB_HOME/python/tools/python.exe" -m pip install --disable-pip-version-check --no-warn-script-location \
    ${GOLDBOT_PIP_ARGS:-} -q -r "$REQUIREMENTS"
as_user wine "$GB_HOME/python/tools/python.exe" -c \
    "import MetaTrader5, numpy, pandas; print('Python Windows OK : MetaTrader5', MetaTrader5.__version__)"

step "5/6 Terminal MetaTrader 5"
find_terminal() { find "$PREFIX/drive_c/Program Files" -maxdepth 2 -name terminal64.exe 2>/dev/null | head -n 1; }
if [ -z "$(find_terminal)" ]; then
    echo "Téléchargement de l'installateur MT5..."
    if as_user curl -fsSL --max-time 600 --retry 2 -o "$GB_HOME/mt5setup.exe" "$MT5_URL"; then
        as_user wineserver -k 2>/dev/null || true  # un installateur d'un essai précédent ne doit pas gêner
        # Écran virtuel :99 avec VNC (celui de goldbot.sh) : l'installation peut se regarder par le tunnel SSH.
        as_user bash "$REPO/scripts/linux/goldbot.sh" ecran
        echo "Installation silencieuse de MT5 : 15 minutes au plus, le script continue ensuite tout seul."
        echo "Pour la regarder : tunnel SSH puis VNC sur localhost:5900 (docs/RUNBOOK.md, partie 1.3)."
        as_user DISPLAY=:99 timeout 900 wine "$GB_HOME/mt5setup.exe" /auto || true
        for _ in $(seq 1 30); do [ -n "$(find_terminal)" ] && break; sleep 10; done  # fichiers en place
        as_user wineserver -k 2>/dev/null || true  # le terminal lancé par l'installateur est relancé plus tard
    else
        echo "Téléchargement de MT5 impossible ($MT5_URL) : relance ce script plus tard, ou MT5_INSTALLER_URL=..."
    fi
fi
TERMINAL="$(find_terminal)"
if [ -n "$TERMINAL" ]; then
    rm -f "$GB_HOME/mt5setup.exe"
    echo "Terminal MT5 : $TERMINAL (goldbot.sh le donne au bot : rien à changer dans .env)"
else
    echo "ATTENTION : terminal MT5 non installé. Installe-le à la main en le voyant :"
    echo "  bash $REPO/scripts/linux/goldbot.sh installer-mt5   puis VNC (localhost:5900 par le tunnel SSH)"
    echo "puis relance ce script pour finir (il saute ce qui est déjà fait)."
fi

step "6/6 Service systemd goldbot (démarre avec le VPS, relance le bot)"
cat >/etc/systemd/system/goldbot.service <<EOF
[Unit]
Description=goldbot : terminal MT5 (Wine) et bot de trading, relancés automatiquement
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Environment=HOME=$RUN_HOME
WorkingDirectory=$REPO
ExecStart=/bin/bash $REPO/scripts/linux/goldbot.sh bot
# Relance après un plantage ; jamais après un refus (3 : arrêt total, réglages modifiés...), une configuration
# invalide (4) ou un bot déjà en cours (5) : décision humaine.
Restart=on-failure
RestartSec=60
RestartPreventExitStatus=3 4 5
KillSignal=SIGINT
TimeoutStopSec=90

[Install]
WantedBy=multi-user.target
EOF
if systemctl daemon-reload 2>/dev/null; then
    echo "Service installé (pas encore démarré)."
else
    echo "systemd indisponible ici : service écrit dans /etc/systemd/system/goldbot.service."
fi

cat <<EOF

Installation terminée. Suite (docs/RUNBOOK.md, partie Ubuntu) :
  1. copie ton .env du PC dans $REPO/.env (depuis le PC : scp; MT5_PATH est réglé tout seul sur le VPS)
  2. bash $REPO/scripts/linux/goldbot.sh mt5         puis connexion au compte démo et Algo Trading (VNC)
  3. bash $REPO/scripts/linux/goldbot.sh verifier
  4. sudo systemctl enable --now goldbot             (démarre maintenant et à chaque redémarrage du VPS)
EOF
