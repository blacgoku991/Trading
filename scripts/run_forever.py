"""Garde le bot en marche 24 h/24 sur le VPS Windows (Phase 6, docs/RUNBOOK.md).

Depuis la racine du repo :
    .\\.venv\\Scripts\\python.exe scripts\\run_forever.py              # expérience de scalping (run_scalp.py)
    .\\.venv\\Scripts\\python.exe scripts\\run_forever.py --bot live   # bot principal (run_live.py)

    .\\.venv\\Scripts\\python.exe scripts\\run_forever.py --nouvelle-experience   # après un changement de réglages

Relance après un plantage ou une connexion impossible ; jamais après Ctrl+C, un refus (arrêt total à -10 %,
réglages modifiés, Algo Trading coupé…), une configuration invalide ou un bot déjà en cours.
--nouvelle-experience ne vaut que pour le premier lancement (décision manuelle) : les relances automatiques ne
l'utilisent jamais, la relance après l'arrêt total reste manuelle. Journal : logs\\gardien_<bot>.log.
"""

import argparse
import sys
from pathlib import Path

from goldbot.live.keepalive import SCRIPTS, keep_running

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="run_forever.py", description="Garde le bot en marche (VPS).")
    parser.add_argument("--bot", choices=sorted(SCRIPTS), default="scalp", help="scalp (défaut) ou live")
    parser.add_argument("--nouvelle-experience", action="store_true",
                        help="scalp : archive l'expérience au premier lancement seulement")  # fmt: skip
    args = parser.parse_args()
    if args.nouvelle_experience and args.bot != "scalp":
        parser.error("--nouvelle-experience ne concerne que l'expérience de scalping")
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "scripts" / SCRIPTS[args.bot])]
    first = ["--nouvelle-experience"] if args.nouvelle_experience else []
    sys.exit(keep_running(command, cwd=root, first_extra=first, log_path=root / "logs" / f"gardien_{args.bot}.log"))
