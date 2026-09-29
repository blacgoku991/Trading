"""Vérifications de l'environnement Windows avant la connexion MT5.

Causes connues de l'erreur -10005 (IPC timeout) : mauvais MT5_PATH, terminal lancé
en administrateur (pas Python, ou l'inverse), terminal en mode portable, version
Microsoft Store isolée, Python 32 bits. Tout est vérifiable sans MT5.
"""

from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PureWindowsPath

from goldbot.broker.checks import Finding, Level

# Liste les terminaux MT5 64 bits ouverts : PID|chemin|ligne de commande (UTF-8).
_QUERY = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
    "Get-CimInstance Win32_Process -Filter \"Name='terminal64.exe'\" | "
    'ForEach-Object { "$($_.ProcessId)|$($_.ExecutablePath)|$($_.CommandLine)" }'
)


@dataclass(frozen=True)
class TerminalProcess:
    pid: int
    path: str  # vide si Windows refuse de le donner (terminal lancé avec d'autres droits)
    command_line: str

    @property
    def portable(self) -> bool:
        return "/portable" in self.command_line.lower()


def running_terminals(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[TerminalProcess] | None:
    """Terminaux MT5 64 bits ouverts, ou None si la liste n'a pas pu être obtenue."""
    try:
        completed = run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    processes = []
    for line in completed.stdout.splitlines():
        pid, _, rest = line.partition("|")
        path, _, command_line = rest.partition("|")
        if pid.strip().isdigit():
            processes.append(TerminalProcess(int(pid), path.strip(), command_line.strip()))
    return processes


def is_admin() -> bool | None:
    """Vrai si ce processus Python tourne en administrateur (None hors Windows)."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return None


def check_windows_environment(
    terminal_path: str | None,
    *,
    python_version: str,
    python_bits: int,
    admin: bool | None,
    processes: list[TerminalProcess] | None,
    file_exists: Callable[[str], bool],
) -> list[Finding]:
    findings = [
        Finding(Level.OK, f"Python {python_version} {python_bits} bits")
        if python_bits == 64
        else Finding(Level.ERROR, f"Python {python_version} en {python_bits} bits : installe Python 64 bits"),
    ]
    if admin:
        findings.append(Finding(Level.INFO, "Python tourne en administrateur : MT5 doit tourner avec les mêmes droits"))

    if terminal_path:
        if "\\windowsapps\\" in terminal_path.lower():
            findings.append(
                Finding(
                    Level.ERROR,
                    "MT5_PATH pointe vers une version Microsoft Store (WindowsApps), isolée du reste de Windows : "
                    "installe le terminal MT5 depuis le site d'Axi",
                )
            )
        if not file_exists(terminal_path):
            findings.append(Finding(Level.ERROR, f"MT5_PATH ne pointe vers aucun fichier : {terminal_path}"))
        elif not terminal_path.lower().endswith("terminal64.exe"):
            findings.append(Finding(Level.ERROR, f"MT5_PATH doit se terminer par terminal64.exe : {terminal_path}"))

    if processes is None:
        findings.append(Finding(Level.INFO, "Liste des terminaux MT5 ouverts indisponible"))
        return findings
    if not processes:
        findings.append(
            Finding(Level.ERROR, "Aucun terminal MT5 (terminal64.exe) ouvert : lance le terminal Axi et connecte-toi")
        )
        return findings

    for process in processes:
        if not process.path:
            findings.append(
                Finding(
                    Level.ERROR,
                    f"Terminal ouvert (PID {process.pid}) dont Windows cache le chemin : il tourne probablement en "
                    "administrateur. Relance MT5 normalement, ou lance PowerShell en administrateur",
                )
            )
            continue
        mode = "portable" if process.portable else "normal"
        findings.append(Finding(Level.INFO, f"Terminal ouvert (PID {process.pid}, mode {mode}) : {process.path}"))

    visible = [p for p in processes if p.path]
    if terminal_path and visible:
        matching = [p for p in visible if PureWindowsPath(p.path) == PureWindowsPath(terminal_path)]
        if not matching:
            findings.append(
                Finding(
                    Level.ERROR,
                    "MT5_PATH ne correspond à aucun terminal ouvert : copie le chemin affiché ci-dessus dans .env",
                )
            )
        elif any(p.portable for p in matching):
            findings.append(
                Finding(
                    Level.ERROR,
                    "Le terminal tourne en mode portable (/portable) : mets « portable: true » dans la section mt5 "
                    "de config/settings.yaml",
                )
            )
        else:
            findings.append(Finding(Level.OK, "MT5_PATH correspond au terminal ouvert"))
    return findings
