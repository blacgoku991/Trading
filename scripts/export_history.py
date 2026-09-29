"""Exporte l'historique de l'or (barres d'une minute et ticks) depuis MT5. À lancer sous Windows.

Depuis la racine du repo, terminal MT5 ouvert :
    .\\.venv\\Scripts\\python.exe scripts\\export_history.py

Les fichiers arrivent dans data\\export (réglages : section export de config/settings.yaml).
Codes de sortie : 0 OK, 1 export incomplet, 2 connexion impossible, 4 configuration invalide,
130 interrompu (Ctrl+C).
"""

import sys
from pathlib import Path

from goldbot.export import main

if __name__ == "__main__":
    # Sortie en UTF-8 même redirigée vers un fichier (sinon cp1252 sous Windows).
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main(root=Path(__file__).resolve().parents[1]))
    except KeyboardInterrupt:
        print("\nInterrompu (Ctrl+C). Relance la commande pour reprendre l'export depuis le début.")
        sys.exit(130)
