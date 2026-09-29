"""Lance le bot de trading de l'or en direct. Windows, terminal MT5 ouvert et connecté.

Depuis la racine du repo :
    .\\.venv\\Scripts\\python.exe scripts\\run_live.py --simulation   # d'abord : aucun ordre envoyé
    .\\.venv\\Scripts\\python.exe scripts\\run_live.py                # compte DÉMO

Codes de sortie : 0 arrêt normal, 2 connexion impossible, 3 refus (compte réel, netting,
Algo Trading désactivé), 4 configuration invalide, 5 un autre bot tourne déjà.
"""

import sys
from pathlib import Path

from goldbot.live.cli import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main(root=Path(__file__).resolve().parents[1]))
    except KeyboardInterrupt:
        print("\nArrêt demandé : les positions ouvertes gardent leur SL sur le serveur.")
        sys.exit(0)
