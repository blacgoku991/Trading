#!/usr/bin/env bash
# Outils du bot sur un VPS Ubuntu, où MT5 et le Python du bot tournent sous Wine (docs/RUNBOOK.md, partie Ubuntu).
#
#   bash scripts/linux/goldbot.sh mt5                  écran virtuel + terminal MT5, pour le régler (VNC par SSH)
#   bash scripts/linux/goldbot.sh installer-mt5        installateur MT5 à l'écran, à suivre par VNC (si l'auto échoue)
#   bash scripts/linux/goldbot.sh ecran                écran virtuel :99 + VNC seulement
#   bash scripts/linux/goldbot.sh verifier             vérification de la connexion (check_connection.py)
#   bash scripts/linux/goldbot.sh test                 tests du dépôt avec le Python Windows (sans MT5)
#   bash scripts/linux/goldbot.sh bilan                bilans de l'expérience de scalping
#   bash scripts/linux/goldbot.sh nouvelle-experience  au prochain démarrage du service : --nouvelle-experience
#   bash scripts/linux/goldbot.sh bot                  gardien + bot (lancé par le service systemd goldbot)
#
# Les relances automatiques (gardien, systemd) ne passent jamais --nouvelle-experience d'elles-mêmes : la relance
# après l'arrêt total à -10 % reste une décision manuelle (commande nouvelle-experience, puis redémarrage).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GB_HOME="${GOLDBOT_HOME:-$HOME/.goldbot}"
export WINEPREFIX="${WINEPREFIX:-$HOME/.wine-goldbot}"
export WINEDEBUG="${WINEDEBUG:--all}"
export DISPLAY="${GOLDBOT_DISPLAY:-:99}"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
PYWIN="$GB_HOME/python/tools/python.exe"
FLAG="$REPO/data/nouvelle_experience_demandee"

say() { printf '%s goldbot.sh : %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

need_python() {
    if [ ! -f "$PYWIN" ]; then
        say "Python Windows introuvable ($PYWIN) : lance d'abord sudo bash scripts/linux/install_ubuntu.sh"
        exit 4
    fi
}

win() { winepath -w "$1" 2>/dev/null; }

terminal_exe() {
    # terminal64.exe du préfixe Wine (MetaTrader 5 générique ou version d'un broker).
    find "$WINEPREFIX/drive_c/Program Files" -maxdepth 2 -name terminal64.exe 2>/dev/null | head -n 1
}

ecran() {
    # Écran virtuel (Xvfb) + petit gestionnaire de fenêtres + VNC limité à la machine (accès par tunnel SSH).
    local number="${DISPLAY#:}"
    mkdir -p "$GB_HOME"
    if [ ! -S "/tmp/.X11-unix/X$number" ]; then
        setsid nohup Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >"$GB_HOME/xvfb.log" 2>&1 &
        for _ in $(seq 1 50); do [ -S "/tmp/.X11-unix/X$number" ] && break; sleep 0.2; done
        [ -S "/tmp/.X11-unix/X$number" ] || { say "écran virtuel $DISPLAY impossible à démarrer ($GB_HOME/xvfb.log)"; exit 2; }
    fi
    pgrep -u "$(id -u)" -x openbox >/dev/null || setsid nohup openbox >"$GB_HOME/openbox.log" 2>&1 &
    pgrep -u "$(id -u)" -f "x11vnc -display $DISPLAY" >/dev/null ||
        setsid nohup x11vnc -display "$DISPLAY" -localhost -forever -shared -nopw -quiet >"$GB_HOME/x11vnc.log" 2>&1 &
}

python_win() {
    need_python
    PYTHONPATH="$(win "$REPO/src");$(win "$REPO")" wine "$PYWIN" "$@"
}

use_vps_terminal() {
    # Le terminal du VPS remplace le MT5_PATH du .env copié depuis le PC (les variables d'environnement passent
    # avant le fichier .env) : pas besoin de modifier .env sur le VPS.
    local exe
    exe="$(terminal_exe)"
    if [ -n "$exe" ]; then
        MT5_PATH="$(win "$exe")"
        export MT5_PATH
    fi
}

case "${1:-}" in
    ecran)
        ecran
        say "écran virtuel $DISPLAY prêt ; VNC sur localhost:5900 (depuis ton PC : ssh -L 5900:localhost:5900 $(id -un)@<IP du VPS>)."
        ;;
    installer-mt5)
        ecran
        setup="$GB_HOME/mt5setup.exe"
        url="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
        [ -f "$setup" ] || curl -fsSL --max-time 600 --retry 2 -o "$setup" "$url"
        wineserver -k 2>/dev/null || true
        (setsid nohup wine "$setup" >"$GB_HOME/mt5setup.log" 2>&1 &)
        say "installateur MT5 lancé à l'écran : regarde-le par VNC (localhost:5900 par le tunnel SSH) et suis-le."
        say "Ensuite : sudo bash $REPO/scripts/linux/install_ubuntu.sh (affiche MT5_PATH et finit l'installation)."
        ;;
    mt5)
        ecran
        exe="$(terminal_exe)"
        [ -n "$exe" ] || { say "terminal64.exe introuvable dans $WINEPREFIX : relance install_ubuntu.sh"; exit 4; }
        pgrep -f "terminal64.exe" >/dev/null || (cd "$(dirname "$exe")" && setsid nohup wine "$exe" >"$GB_HOME/mt5.log" 2>&1 &)
        say "terminal MT5 lancé sur l'écran virtuel $DISPLAY."
        say "Pour le voir : sur ton PC, ssh -L 5900:localhost:5900 $(id -un)@<IP du VPS>, puis un visualiseur VNC sur localhost:5900."
        say "terminal utilisé par le bot : $(win "$exe") (le MT5_PATH du .env est remplacé sur le VPS)."
        ;;
    verifier)
        ecran
        use_vps_terminal
        cd "$REPO"
        python_win "$(win "$REPO/scripts/check_connection.py")"
        ;;
    test)
        cd "$REPO"
        python_win -m pytest -q -p no:cacheprovider
        ;;
    bilan)
        ecran
        use_vps_terminal
        cd "$REPO"
        python_win "$(win "$REPO/scripts/run_scalp.py")" --bilan
        ;;
    nouvelle-experience)
        mkdir -p "$REPO/data"
        touch "$FLAG"
        say "demandé : la prochaine fois que le bot démarre, l'expérience en cours est archivée (--nouvelle-experience)."
        say "Pour le faire maintenant : sudo systemctl restart goldbot"
        ;;
    bot)
        need_python
        ecran
        use_vps_terminal
        cd "$REPO"
        extra=()
        if [ -f "$FLAG" ]; then
            rm -f "$FLAG"
            extra=(--nouvelle-experience)
            say "nouvelle expérience demandée : archivage au démarrage"
        fi
        PYTHONPATH="$(win "$REPO/src");$(win "$REPO")"
        export PYTHONPATH
        exec wine "$PYWIN" "$(win "$REPO/scripts/run_forever.py")" "${extra[@]}"
        ;;
    *)
        sed -n '2,14p' "${BASH_SOURCE[0]}"
        exit 1
        ;;
esac
