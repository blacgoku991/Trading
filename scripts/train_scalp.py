"""Entraînement hors ligne ; aucune connexion à MT5 et aucun ordre."""

import sys
from pathlib import Path

from goldbot.scalping.train_cli import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main(root=Path(__file__).resolve().parents[1]))
