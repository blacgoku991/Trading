"""Démarrage du candidat impulsion/repli/reprise sur MT5 démo. Voir docs/LEARNING_DEMO.md."""

import sys
from pathlib import Path

from goldbot.scalping.cli import live_main

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    root = Path(__file__).resolve().parents[1]
    raise SystemExit(live_main(root=root, default_config=root / "config" / "learning_demo.yaml"))
