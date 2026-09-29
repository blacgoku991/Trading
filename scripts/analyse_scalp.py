"""Analyse des erreurs de l'expérience de scalping et apprentissage « en avançant » (Codespaces ou Windows).

python scripts/analyse_scalp.py                 # prix exécutables
python scripts/analyse_scalp.py --glissement 10 # 10 points de glissement en plus
"""

import sys
from pathlib import Path

from goldbot.scalping.research import main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(root=Path(__file__).resolve().parents[1]))
