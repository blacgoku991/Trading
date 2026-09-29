"""Expérience de scalping sur compte DÉMO (séparée du bot principal). Windows, terminal MT5 ouvert.

Depuis la racine du repo :
    .\\.venv\\Scripts\\python.exe scripts\\run_scalp.py --verification   # test technique (marché ouvert)
    .\\.venv\\Scripts\\python.exe scripts\\run_scalp.py                  # collecte des résultats
    .\\.venv\\Scripts\\python.exe scripts\\run_scalp.py --simulation     # aucun ordre envoyé
    .\\.venv\\Scripts\\python.exe scripts\\run_scalp.py --bilan          # les deux bilans

Codes de sortie : 0 OK, 1 vérification en échec, 2 connexion impossible, 3 refus (compte non démo…),
4 configuration invalide, 5 déjà en cours.
"""

import sys
from pathlib import Path

from goldbot.scalping.cli import live_main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(live_main(root=Path(__file__).resolve().parents[1]))
    except KeyboardInterrupt:
        print("\nArrêt demandé : les positions ouvertes gardent leur stop sur le serveur.")
        sys.exit(0)
