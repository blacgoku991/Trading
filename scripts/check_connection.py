"""Vérifie la connexion au terminal MT5 et l'état du compte. À lancer sous Windows.

Depuis la racine du repo :
    .\\.venv\\Scripts\\python.exe scripts\\check_connection.py
    .\\.venv\\Scripts\\python.exe scripts\\check_connection.py --test-order   # compte DÉMO uniquement

Codes de sortie : 0 OK, 1 problème détecté, 2 connexion impossible,
3 ordre de test refusé, 4 configuration invalide, 130 interrompu (Ctrl+C).
"""

import sys
from pathlib import Path

from goldbot.diagnostics import main

if __name__ == "__main__":
    # Sortie en UTF-8 même redirigée vers un fichier (sinon cp1252 sous Windows).
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main(root=Path(__file__).resolve().parents[1]))
    except KeyboardInterrupt:
        # Un ordre de test en cours est refermé par ses blocs finally avant d'arriver ici.
        print("\nInterrompu (Ctrl+C).")
        sys.exit(130)
