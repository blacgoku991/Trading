"""Rejoue l'expérience de scalping sur les ticks exportés (Codespaces ou Windows).

python scripts/backtest_scalp.py
"""

import sys
from pathlib import Path

from goldbot.scalping.cli import backtest_main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(backtest_main(root=Path(__file__).resolve().parents[1]))
