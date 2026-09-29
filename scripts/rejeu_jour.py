"""Rejoue une journée (par défaut la dernière) : la version démo du scalper et des variantes d'un seul réglage.

Windows, terminal MT5 ouvert (lecture de l'historique seulement, aucun ordre envoyé) :
    .\\.venv\\Scripts\\python.exe scripts\\rejeu_jour.py
    .\\.venv\\Scripts\\python.exe scripts\\rejeu_jour.py --jour 2026-09-29 --de 21:55 --a 23:00 --capital 4940
"""

import sys
from pathlib import Path

from goldbot.scalping.day_replay import day_main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(day_main(root=Path(__file__).resolve().parents[1]))
