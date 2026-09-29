"""Backtest d'une stratégie sur l'historique exporté (Codespaces ou Windows).

    python scripts/run_backtest.py                      # S1 sur la période d'étude (hors échantillon exclu)
    python scripts/run_backtest.py --spread-x 1.5       # stress des coûts
    python scripts/run_backtest.py --hors-echantillon   # validation finale, une seule fois

Rapport markdown et courbe d'equity dans reports/ ; chaque essai est ajouté à reports/essais.csv.
"""

import sys
from pathlib import Path

from goldbot.backtest.cli import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(root=Path(__file__).resolve().parents[1]))
