"""Garde un bot en marche sur le VPS (Phase 6) : relance après un plantage ou une connexion impossible, jamais
après un arrêt voulu ou un refus, qui demandent une décision humaine.

- Codes de sortie de run_scalp.py et run_live.py (README, « Codes de sortie ») : 0 arrêt voulu (Ctrl+C), 1 plantage
  (exception Python), 2 connexion au terminal impossible, 3 refus (arrêt total à -10 %, réglages modifiés, Algo
  Trading coupé, compte non démo…), 4 configuration invalide, 5 déjà en cours.
- Relance pour 1, 2 et tout code inattendu, après une attente qui double à chaque arrêt rapproché (15 s, 30 s…
  5 min au plus) et revient à 15 s après 30 minutes de marche normale.
- Options du premier lancement (first_extra, par exemple --nouvelle-experience demandé à la main) : jamais reprises
  par les relances automatiques ; la relance après l'arrêt total reste manuelle (CLAUDE.md règle 5).
- Ctrl+C : le bot reçoit le même Ctrl+C et ferme ses positions ; le gardien attend qu'il ait fini, puis s'arrête.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPTS = {"scalp": "run_scalp.py", "live": "run_live.py"}
STOP_CODES = {
    0: "arrêt normal (Ctrl+C)",
    3: "refus, lire le message du bot (arrêt total, réglages modifiés, Algo Trading coupé, compte non démo…)",
    4: "configuration ou .env invalide",
    5: "le bot tourne déjà sur ce PC",
}
RESTART_REASONS = {1: "plantage", 2: "connexion au terminal MT5 impossible"}
FIRST_WAIT_S = 15
MAX_WAIT_S = 300
STABLE_RUN_S = 1800  # 30 minutes de marche : l'arrêt suivant repart avec l'attente la plus courte
POLL_S = 1.0  # attente du bot par tranches : Ctrl+C reste pris en compte sous Windows
PARIS = ZoneInfo("Europe/Paris")


def keep_running(
    command: Sequence[str],
    *,
    cwd: Path,
    first_extra: Sequence[str] = (),
    log_path: Path,
    start: Callable[..., subprocess.Popen] = subprocess.Popen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    echo: Callable[[str], None] = print,
    max_runs: int | None = None,
) -> int:
    """Lance `command` et le relance selon son code de sortie ; renvoie le code qui a arrêté le gardien."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def say(text: str) -> None:
        line = f"{now().astimezone(PARIS):%Y-%m-%d %H:%M:%S} gardien : {text}"
        echo(line)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    delay, runs, process = FIRST_WAIT_S, 0, None
    try:
        while True:
            extra = list(first_extra) if runs == 0 else []
            say(" ".join([f"démarrage de {Path(command[-1]).name}", *extra]))
            began = clock()
            process = start([*command, *extra], cwd=str(cwd))
            code = _wait(process)
            process = None
            runs += 1
            if code in STOP_CODES:
                say(f"le bot s'est arrêté (code {code} : {STOP_CODES[code]}). Pas de relance automatique.")
                return code
            ran = clock() - began
            if ran >= STABLE_RUN_S:
                delay = FIRST_WAIT_S
            reason = RESTART_REASONS.get(code, "code inattendu")
            if max_runs is not None and runs >= max_runs:
                say(f"le bot s'est arrêté (code {code} : {reason}) ; nombre de lancements atteint.")
                return code
            say(f"le bot s'est arrêté (code {code} : {reason}) après {ran / 60:.0f} min : relance dans {delay} s")
            sleep(delay)
            delay = min(delay * 2, MAX_WAIT_S)
    except KeyboardInterrupt:
        if process is not None:
            say("Ctrl+C : attente de la fin propre du bot (fermeture de ses positions)...")
            code = _wait(process, patient=True)
            say(f"bot arrêté (code {code}). Pas de relance.")
        else:
            say("Ctrl+C : pas de relance.")
        return 0


def _wait(process: subprocess.Popen, *, patient: bool = False) -> int:
    """Attend la fin du bot par tranches de POLL_S (un Ctrl+C interrompt l'attente, sauf en mode patient)."""
    while True:
        try:
            return process.wait(timeout=POLL_S)
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt:
            if not patient:
                raise
