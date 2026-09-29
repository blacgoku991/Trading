"""Contrôle qualité des données exportées par export_history.py (Codespaces ou Windows).

    python scripts/data_report.py                 # dossier export.directory de la config
    python scripts/data_report.py --dir chemin    # autre dossier

Rapport en markdown dans reports/. Codes de sortie : 0 OK, 1 alertes, 2 pas de données,
4 configuration invalide.
"""

import sys
from pathlib import Path

from goldbot.data_report import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(root=Path(__file__).resolve().parents[1]))
